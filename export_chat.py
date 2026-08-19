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
from datetime import datetime, timezone, timedelta

from config import load_config

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


def get_message_dbs():
    """Find all message database files."""
    msg_dir = os.path.join(DECRYPTED_DIR, "message")
    dbs = []
    if os.path.isdir(msg_dir):
        for f in sorted(os.listdir(msg_dir)):
            if f.startswith("message_") and f.endswith(".db") and "fts" not in f:
                dbs.append(os.path.join(msg_dir, f))
    return dbs


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
    """Auto-detect current user's wxid from config or local databases."""
    db_dir = _cfg.get("db_dir", "")
    account_dir = os.path.basename(os.path.dirname(db_dir)) if db_dir else ""
    if account_dir.startswith("wxid_"):
        # macOS account directories are usually named like wxid_xxx_suffix.
        parts = account_dir.split("_")
        if len(parts) >= 2:
            return "_".join(parts[:2])

    session_db = os.path.join(DECRYPTED_DIR, "session", "session.db")
    if not os.path.exists(session_db):
        return None
    try:
        with sqlite3.connect(session_db) as conn:
            row = conn.execute(
                "SELECT user_name FROM Name2Id WHERE user_name LIKE 'wxid_%' LIMIT 1"
            ).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
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


def export_chat(contact_username, contact_display_name, output_dir):
    """Export all messages for a contact."""
    table_hash = hashlib.md5(contact_username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"

    os.makedirs(output_dir, exist_ok=True)

    global MY_WXID
    if not MY_WXID:
        MY_WXID = detect_my_wxid()

    all_messages = []

    for db_path in get_message_dbs():
        conn = sqlite3.connect(db_path)
        db_name = os.path.basename(db_path)
        try:
            # Per-DB sender lookup
            name2id = {}
            for rowid, uname in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                name2id[rowid] = uname

            rows = conn.execute(f"""
                SELECT local_id, server_id, local_type, create_time,
                       real_sender_id, message_content, source,
                       WCDB_CT_message_content
                FROM {table_name}
                ORDER BY create_time ASC
            """).fetchall()

            for row in rows:
                sender_wxid = name2id.get(row[4], "")
                if sender_wxid == MY_WXID:
                    sender = MY_NAME
                elif row[2] in (10000, 10002):
                    sender = "系统"
                else:
                    sender = contact_display_name

                content = row[5] or ""
                if isinstance(content, bytes):
                    content = "[压缩内容]"

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
                print(f"  {db_name}: {len(rows)} 条消息")
        except Exception as e:
            pass  # Table doesn't exist in this DB
        finally:
            conn.close()

    all_messages.sort(key=lambda x: x["timestamp"] or 0)

    if not all_messages:
        print(f"未找到与 {contact_display_name} 的聊天记录")
        return

    # === TXT ===
    txt_path = os.path.join(output_dir, "chat.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"微信聊天记录: {contact_display_name} ({contact_username})\n")
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

    print(f"  CSV: {csv_path}")

    # === JSON ===
    json_path = os.path.join(output_dir, "chat.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_messages, f, ensure_ascii=False, indent=2)

    print(f"  JSON: {json_path}")
    print(f"\n导出完成: {len(all_messages)} 条消息")


def main():
    parser = argparse.ArgumentParser(description="微信聊天记录导出工具")
    parser.add_argument("--name", "-n", help="联系人昵称、备注或微信号（模糊搜索）")
    parser.add_argument("--username", "-u", help="联系人用户名（精确匹配，跳过搜索）")
    parser.add_argument("--output", "-o", default="./exported", help="导出目录")
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
