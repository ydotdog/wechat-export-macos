#!/usr/bin/env python3
"""
WeChat Chat History Exporter

Export chat history with a specific contact or group from decrypted WeChat databases.
Outputs TXT (human-readable), CSV (spreadsheet), and JSON (structured data).

Usage:
    python3 export_chat.py --name "联系人昵称或备注" --output ./output_dir
    python3 export_chat.py --name "Chris" --output ~/Downloads/chris
    python3 export_chat.py --name "工作群" --output ~/Downloads/work_group
    python3 export_chat.py --list                    # List all conversations
    python3 export_chat.py --list --top 30           # List top 30 by message count
"""

import sqlite3
import os
import sys
import json
import csv
import hashlib
import argparse
import unicodedata
from datetime import datetime, timezone, timedelta

from config import load_config

try:
    import zstandard as zstd
except ImportError:
    zstd = None

_cfg = load_config()
DECRYPTED_DIR = _cfg["decrypted_dir"]
CONTACT_DB = os.path.join(os.path.dirname(DECRYPTED_DIR), "decrypted", "contact", "contact.db")
# Try alternative path
if not os.path.exists(CONTACT_DB):
    CONTACT_DB = os.path.join(DECRYPTED_DIR, "contact", "contact.db")

MY_WXID = None  # Auto-detected
MY_NAME = "我"
CST = timezone(timedelta(hours=8))

MSG_TYPES = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频",
    47: "表情", 48: "位置", 49: "链接/文件/小程序", 50: "语音/视频通话",
    51: "系统消息", 10000: "系统提示", 10002: "撤回消息",
}

MEDIA_TYPES = {3, 34, 43, 47}
SYSTEM_TYPES = {51, 10000, 10002}
INVALID_FILENAME_CHARS = '<>:"/\\|?*\0'
ZSTD_DECOMPRESSOR = zstd.ZstdDecompressor() if zstd else None
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def get_message_dbs():
    """Find all message database files."""
    msg_dir = os.path.join(DECRYPTED_DIR, "message")
    dbs = []
    if os.path.isdir(msg_dir):
        for f in sorted(os.listdir(msg_dir)):
            if f.startswith("message_") and f.endswith(".db") and "fts" not in f:
                dbs.append(os.path.join(msg_dir, f))
    return dbs


def quote_identifier(identifier):
    """Quote a SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def is_chatroom(username):
    return bool(username and username.endswith("@chatroom"))


def sanitize_folder_name(name, default="未命名"):
    safe_chars = []
    for ch in unicodedata.normalize("NFC", name or ""):
        category = unicodedata.category(ch)
        is_noncharacter = (
            0xFDD0 <= ord(ch) <= 0xFDEF
            or ord(ch) & 0xFFFE == 0xFFFE
        )
        if (
            ch in INVALID_FILENAME_CHARS
            or category.startswith("C")
            or is_noncharacter
        ):
            safe_chars.append("_")
        elif ch == "\xa0":
            safe_chars.append(" ")
        else:
            safe_chars.append(ch)
    cleaned = "".join(safe_chars).strip().strip(".")
    return (cleaned or default)[:80]


def short_id(value):
    if not value:
        return "unknown"
    digest = hashlib.md5(value.encode()).hexdigest()[:8]
    return digest


def decode_bytes(data):
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def decode_message_content(content, compression_type):
    if content is None:
        return ""

    if isinstance(content, memoryview):
        content = content.tobytes()

    if isinstance(content, bytes):
        if compression_type == 4 or content.startswith(ZSTD_MAGIC):
            if ZSTD_DECOMPRESSOR is None:
                return "[压缩内容: 请先安装 zstandard]"
            try:
                return decode_bytes(ZSTD_DECOMPRESSOR.decompress(content))
            except zstd.ZstdError:
                return "[压缩内容: 解压失败]"
        return decode_bytes(content)

    return content


def load_name2id(conn):
    name2id = {}
    try:
        for rowid, uname in conn.execute("SELECT rowid, user_name FROM Name2Id"):
            name2id[rowid] = uname
    except sqlite3.Error:
        pass
    return name2id


def load_contact_display_map():
    """Map username to display name for sender labels."""
    contact_map = {}
    conn = None
    try:
        conn = sqlite3.connect(CONTACT_DB)
        for username, nick, remark in conn.execute(
            "SELECT username, nick_name, remark FROM contact"
        ):
            if not username:
                continue
            contact_map[username] = remark if remark else nick or username
    except sqlite3.Error:
        pass
    finally:
        if conn:
            conn.close()
    return contact_map


def build_contact_table_map():
    """Map Msg_<md5(username)> table names back to contact usernames."""
    table_map = {}
    conn = None
    try:
        conn = sqlite3.connect(CONTACT_DB)
        for (username,) in conn.execute("SELECT username FROM contact"):
            if not username:
                continue
            table_map[f"Msg_{hashlib.md5(username.encode()).hexdigest()}"] = username
    except sqlite3.Error:
        pass
    finally:
        if conn:
            conn.close()
    return table_map


def collect_conversations():
    """Collect all Msg_* conversation tables across message databases."""
    conversations = {}
    for db_path in get_message_dbs():
        conn = sqlite3.connect(db_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (table_name,) in tables:
                try:
                    count, earliest, latest = conn.execute(f"""
                        SELECT COUNT(*), MIN(create_time), MAX(create_time)
                        FROM {quote_identifier(table_name)}
                        WHERE create_time > 0
                    """).fetchone()
                    if count == 0:
                        continue
                    if table_name not in conversations:
                        conversations[table_name] = {
                            "table": table_name,
                            "count": 0,
                            "earliest": earliest,
                            "latest": latest,
                        }
                    conversations[table_name]["count"] += count
                    if earliest and (
                        not conversations[table_name]["earliest"]
                        or earliest < conversations[table_name]["earliest"]
                    ):
                        conversations[table_name]["earliest"] = earliest
                    if latest and (
                        not conversations[table_name]["latest"]
                        or latest > conversations[table_name]["latest"]
                    ):
                        conversations[table_name]["latest"] = latest
                except sqlite3.Error:
                    continue
        finally:
            conn.close()

    table_map = build_contact_table_map()
    display_map = load_contact_display_map()
    for table_name, info in conversations.items():
        username = table_map.get(table_name)
        info["username"] = username
        if username:
            info["display"] = display_map.get(username, username)
        else:
            info["display"] = f"未知会话_{table_name[-8:]}"

    return sorted(
        conversations.values(),
        key=lambda item: (item["display"], item["table"]),
    )


def infer_my_wxid_from_direct_chats():
    """
    Infer our wxid from one-to-one conversations.

    In a direct-chat Msg table, real_sender_id resolves either to the contact's
    username or to the current user's username. Counting the non-contact sender
    across direct-chat tables is more reliable than relying on contact.local_type.
    """
    table_map = {
        table: username
        for table, username in build_contact_table_map().items()
        if not is_chatroom(username)
    }
    if not table_map:
        return None

    sender_counts = {}
    for db_path in get_message_dbs():
        conn = sqlite3.connect(db_path)
        try:
            existing_tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'Msg_%'"
                )
            }
            name2id = load_name2id(conn)
            for table_name, contact_username in table_map.items():
                if table_name not in existing_tables:
                    continue
                rows = conn.execute(f"""
                    SELECT real_sender_id, COUNT(*)
                    FROM {quote_identifier(table_name)}
                    GROUP BY real_sender_id
                """).fetchall()
                for sender_id, count in rows:
                    sender_wxid = name2id.get(sender_id, "")
                    if not sender_wxid or sender_wxid == contact_username:
                        continue
                    if is_chatroom(sender_wxid):
                        continue
                    sender_counts[sender_wxid] = (
                        sender_counts.get(sender_wxid, 0) + count
                    )
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    if not sender_counts:
        return None
    return max(sender_counts.items(), key=lambda item: item[1])[0]


def resolve_sender(sender_wxid, message_type, contact_username,
                   contact_display_name, contact_display_map):
    """Resolve a message sender for direct chats and group chats."""
    if message_type in SYSTEM_TYPES:
        return "系统"

    if not contact_username:
        if MY_WXID and sender_wxid == MY_WXID:
            return MY_NAME
        if sender_wxid:
            return contact_display_map.get(sender_wxid, sender_wxid)
        return contact_display_name

    if is_chatroom(contact_username):
        if MY_WXID and sender_wxid == MY_WXID:
            return MY_NAME
        if sender_wxid:
            return contact_display_map.get(sender_wxid, sender_wxid)
        return contact_display_name

    if sender_wxid == contact_username:
        return contact_display_name
    if MY_WXID and sender_wxid == MY_WXID:
        return MY_NAME
    if sender_wxid:
        return MY_NAME
    return contact_display_name


def find_contact(query):
    """Search contacts by nickname, remark, or alias."""
    conn = sqlite3.connect(CONTACT_DB)
    results = conn.execute("""
        SELECT username, nick_name, remark, alias
        FROM contact
        WHERE nick_name LIKE ? OR remark LIKE ? OR alias LIKE ?
    """, (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
    conn.close()
    return results


def detect_my_wxid():
    """Auto-detect current user's wxid."""
    inferred = infer_my_wxid_from_direct_chats()
    if inferred:
        return inferred

    contact_conn = None
    try:
        # Older database layouts sometimes include a self contact entry.
        contact_conn = sqlite3.connect(CONTACT_DB)
        self_entry = contact_conn.execute(
            "SELECT username FROM contact WHERE username LIKE 'wxid_%' AND local_type = 0 LIMIT 1"
        ).fetchone()
        if self_entry:
            return self_entry[0]
    except sqlite3.Error:
        pass
    finally:
        if contact_conn:
            contact_conn.close()
    return None


def list_conversations(top_n=20):
    """List all conversations with message counts."""
    # Collect all Msg_ tables across databases
    conversations = {}
    for db_path in get_message_dbs():
        conn = sqlite3.connect(db_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            name2id = {}
            try:
                for rowid, uname in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    name2id[rowid] = uname
            except Exception:
                pass

            for (table_name,) in tables:
                try:
                    row = conn.execute(f"""
                        SELECT COUNT(*),
                               datetime(MIN(create_time), 'unixepoch', 'localtime'),
                               datetime(MAX(create_time), 'unixepoch', 'localtime')
                        FROM {table_name} WHERE create_time > 0
                    """).fetchone()
                    count, earliest, latest = row
                    if count == 0:
                        continue
                    if table_name not in conversations:
                        conversations[table_name] = {
                            "count": 0, "earliest": earliest, "latest": latest
                        }
                    conversations[table_name]["count"] += count
                    # Update time range
                    if earliest and (not conversations[table_name]["earliest"]
                                    or earliest < conversations[table_name]["earliest"]):
                        conversations[table_name]["earliest"] = earliest
                    if latest and (not conversations[table_name]["latest"]
                                  or latest > conversations[table_name]["latest"]):
                        conversations[table_name]["latest"] = latest
                except Exception:
                    continue
        finally:
            conn.close()

    # Resolve table hashes to contact names
    contact_map = {}
    try:
        conn = sqlite3.connect(CONTACT_DB)
        for username, nick, remark, alias in conn.execute(
            "SELECT username, nick_name, remark, alias FROM contact"
        ):
            h = hashlib.md5(username.encode()).hexdigest()
            table = f"Msg_{h}"
            display = remark if remark else nick
            contact_map[table] = (username, display, nick, remark)
        conn.close()
    except Exception:
        pass

    # Sort by message count
    sorted_convs = sorted(conversations.items(), key=lambda x: x[1]["count"], reverse=True)

    print(f"\n{'排名':<4} {'消息数':<8} {'时间范围':<45} {'显示名':<20} {'用户名'}")
    print("-" * 120)
    for i, (table, info) in enumerate(sorted_convs[:top_n], 1):
        if table in contact_map:
            username, display, nick, remark = contact_map[table]
        else:
            username = table
            display = "(?)"
        time_range = f"{info['earliest']} ~ {info['latest']}"
        print(f"{i:<4} {info['count']:<8} {time_range:<45} {display:<20} {username}")

    print(f"\n共 {len(conversations)} 个会话")


def export_chat(contact_username, contact_display_name, output_dir,
                table_name=None, verbose=True):
    """Export all messages for a contact."""
    if table_name is None:
        table_hash = hashlib.md5(contact_username.encode()).hexdigest()
        table_name = f"Msg_{table_hash}"

    os.makedirs(output_dir, exist_ok=True)

    global MY_WXID
    if not MY_WXID:
        MY_WXID = detect_my_wxid()

    all_messages = []
    contact_display_map = load_contact_display_map()

    for db_path in get_message_dbs():
        conn = sqlite3.connect(db_path)
        db_name = os.path.basename(db_path)
        try:
            # Per-DB sender lookup
            name2id = load_name2id(conn)

            rows = conn.execute(f"""
                SELECT local_id, server_id, local_type, create_time,
                       real_sender_id, message_content, source,
                       WCDB_CT_message_content
                FROM {quote_identifier(table_name)}
                ORDER BY create_time ASC
            """).fetchall()

            for row in rows:
                sender_wxid = name2id.get(row[4], "")
                sender = resolve_sender(
                    sender_wxid,
                    row[2],
                    contact_username,
                    contact_display_name,
                    contact_display_map,
                )

                content = decode_message_content(row[5], row[7])

                all_messages.append({
                    "time": datetime.fromtimestamp(row[3], tz=CST).strftime(
                        "%Y-%m-%d %H:%M:%S") if row[3] else "",
                    "timestamp": row[3],
                    "sender": sender,
                    "type": row[2],
                    "type_name": MSG_TYPES.get(row[2], f"未知({row[2]})"),
                    "content": content,
                    "server_id": row[1],
                    "db": db_name,
                })

            if rows:
                if verbose:
                    print(f"  {db_name}: {len(rows)} 条消息")
        except Exception as e:
            pass  # Table doesn't exist in this DB
        finally:
            conn.close()

    all_messages.sort(key=lambda x: x["timestamp"] or 0)

    if not all_messages:
        if verbose:
            print(f"未找到与 {contact_display_name} 的聊天记录")
        return 0

    # === TXT ===
    txt_path = os.path.join(output_dir, "chat.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        identity = contact_username or table_name
        f.write(f"微信聊天记录: {contact_display_name} ({identity})\n")
        f.write(f"总消息数: {len(all_messages)}\n")
        f.write(f"时间范围: {all_messages[0]['time']} ~ {all_messages[-1]['time']}\n")
        f.write("=" * 60 + "\n\n")

        for m in all_messages:
            content = m["content"]
            if m["type"] in MEDIA_TYPES:
                content = f"[{m['type_name']}]"
            elif m["type"] != 1 and not content:
                content = f"[{m['type_name']}]"
            f.write(f"[{m['time']}] {m['sender']}: {content}\n")

    if verbose:
        print(f"  TXT: {txt_path}")

    # === CSV ===
    csv_path = os.path.join(output_dir, "chat.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["时间", "发送者", "类型", "内容"])
        for m in all_messages:
            content = m["content"]
            if m["type"] in MEDIA_TYPES:
                content = f"[{m['type_name']}]"
            elif m["type"] != 1 and not content:
                content = f"[{m['type_name']}]"
            writer.writerow([m["time"], m["sender"], m["type_name"], content])

    if verbose:
        print(f"  CSV: {csv_path}")

    # === JSON ===
    json_path = os.path.join(output_dir, "chat.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_messages, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"  JSON: {json_path}")
        print(f"\n导出完成: {len(all_messages)} 条消息")
    return len(all_messages)


def make_conversation_folder(output_dir, display, unique_key, used_names):
    base_name = sanitize_folder_name(display)
    folder_name = base_name
    if folder_name in used_names:
        folder_name = f"{base_name}_{short_id(unique_key)}"
    counter = 2
    while folder_name in used_names:
        folder_name = f"{base_name}_{short_id(unique_key)}_{counter}"
        counter += 1
    used_names.add(folder_name)
    return os.path.join(output_dir, folder_name), folder_name


def format_timestamp(timestamp):
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, tz=CST).strftime("%Y-%m-%d %H:%M:%S")


def export_all_chats(output_dir):
    """Export every conversation into a display-name folder."""
    os.makedirs(output_dir, exist_ok=True)

    global MY_WXID
    if not MY_WXID:
        MY_WXID = detect_my_wxid()

    conversations = collect_conversations()
    manifest_rows = []
    used_names = set()
    exported = 0
    skipped = 0
    total_messages = 0

    print(f"\n准备导出 {len(conversations)} 个会话到: {output_dir}")
    for idx, conv in enumerate(conversations, 1):
        unique_key = conv["username"] or conv["table"]
        chat_dir, folder_name = make_conversation_folder(
            output_dir,
            conv["display"],
            unique_key,
            used_names,
        )
        count = export_chat(
            conv["username"],
            conv["display"],
            chat_dir,
            table_name=conv["table"],
            verbose=False,
        )
        if count:
            exported += 1
            total_messages += count
        else:
            skipped += 1

        manifest_rows.append({
            "folder": folder_name,
            "display": conv["display"],
            "username": conv["username"] or "",
            "table": conv["table"],
            "messages": count,
            "earliest": format_timestamp(conv["earliest"]),
            "latest": format_timestamp(conv["latest"]),
        })

        if idx % 50 == 0 or idx == len(conversations):
            print(f"  已处理 {idx}/{len(conversations)}")

    manifest_path = os.path.join(output_dir, "manifest.csv")
    with open(manifest_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "folder", "display", "username", "table",
                "messages", "earliest", "latest",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    print(f"\n全部导出完成: {exported} 个会话, {total_messages} 条消息")
    if skipped:
        print(f"跳过空会话: {skipped} 个")
    print(f"索引文件: {manifest_path}")


def main():
    parser = argparse.ArgumentParser(description="微信聊天记录导出工具")
    parser.add_argument("--name", "-n", help="联系人昵称、备注或微信号（模糊搜索）")
    parser.add_argument("--username", "-u", help="联系人用户名（精确匹配，跳过搜索）")
    parser.add_argument("--output", "-o", default="./exported", help="导出目录")
    parser.add_argument("--all", "-a", action="store_true", help="导出所有会话")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有会话")
    parser.add_argument("--top", type=int, default=20, help="列出前N个会话（默认20）")
    parser.add_argument("--my-wxid", help="你自己的微信ID（可选，自动检测）")
    args = parser.parse_args()

    global MY_WXID
    if args.my_wxid:
        MY_WXID = args.my_wxid

    if args.list:
        list_conversations(args.top)
        return

    if args.all:
        export_all_chats(args.output)
        return

    if not args.name and not args.username:
        parser.print_help()
        return

    if args.username:
        # Direct username, look up display name
        results = find_contact(args.username)
        if results:
            username, nick, remark, alias = results[0]
            display = remark if remark else nick
        else:
            username = args.username
            display = args.username
        print(f"\n导出: {display} ({username})")
        export_chat(username, display, args.output)
        return

    # Search by name
    results = find_contact(args.name)

    if not results:
        print(f"未找到匹配 \"{args.name}\" 的联系人")
        return

    if len(results) == 1:
        username, nick, remark, alias = results[0]
        display = remark if remark else nick
        print(f"\n找到联系人: {display} ({username})")
        export_chat(username, display, args.output)
    else:
        print(f"\n找到 {len(results)} 个匹配的联系人:")
        for i, (username, nick, remark, alias) in enumerate(results, 1):
            display = remark if remark else nick
            print(f"  {i}. {display} (昵称: {nick}, 备注: {remark}, 微信号: {alias}, ID: {username})")

        try:
            choice = input("\n请选择 [1-{}]: ".format(len(results))).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(results):
                idx = int(choice) - 1
                username, nick, remark, alias = results[idx]
                display = remark if remark else nick
                print(f"\n导出: {display} ({username})")
                export_chat(username, display, args.output)
            else:
                print("已取消")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")


if __name__ == "__main__":
    main()
