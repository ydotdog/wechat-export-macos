#!/usr/bin/env python3
"""
WeChat Chat History Exporter (Fixed)
"""

import sqlite3
import os
import sys
import json
import csv
import hashlib
import argparse
import re
from datetime import datetime, timezone, timedelta

from config import load_config

_cfg = load_config()
DECRYPTED_DIR = _cfg["decrypted_dir"]
CONTACT_DB = os.path.join(os.path.dirname(DECRYPTED_DIR), "decrypted", "contact", "contact.db")
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

# Global cache for contact names
CONTACT_CACHE = {}

def get_contact_name(wxid):
    if wxid in CONTACT_CACHE:
        return CONTACT_CACHE[wxid]
    
    if not wxid:
        return "未知"
        
    try:
        conn = sqlite3.connect(CONTACT_DB)
        row = conn.execute(
            "SELECT nick_name, remark FROM contact WHERE username = ?", (wxid,)
        ).fetchone()
        conn.close()
        if row:
            nick, remark = row
            name = remark if remark else nick
            CONTACT_CACHE[wxid] = name
            return name
    except Exception:
        pass
    
    CONTACT_CACHE[wxid] = wxid
    return wxid

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
    """Search contacts by username, nickname, remark, or alias."""
    conn = sqlite3.connect(CONTACT_DB)
    # First try exact username
    results = conn.execute("""
        SELECT username, nick_name, remark, alias
        FROM contact
        WHERE username = ?
    """, (query,)).fetchall()
    
    if not results:
        # Then try fuzzy search
        results = conn.execute("""
            SELECT username, nick_name, remark, alias
            FROM contact
            WHERE nick_name LIKE ? OR remark LIKE ? OR alias LIKE ?
        """, (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
    conn.close()
    return results


def detect_my_wxid():
    """Auto-detect current user's wxid from session database."""
    session_db = os.path.join(DECRYPTED_DIR, "session", "session.db")
    if not os.path.exists(session_db):
        return None
    try:
        conn = sqlite3.connect(session_db)
        contact_conn = sqlite3.connect(CONTACT_DB)
        self_entry = contact_conn.execute(
            "SELECT username FROM contact WHERE username LIKE 'wxid_%' AND local_type = 0 LIMIT 1"
        ).fetchone()
        contact_conn.close()
        if self_entry:
            return self_entry[0]
    except Exception:
        pass
    return None


def list_conversations(top_n=20):
    """List all conversations with message counts."""
    conversations = {}
    for db_path in get_message_dbs():
        conn = sqlite3.connect(db_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for (table_name,) in tables:
                try:
                    row = conn.execute(f"""
                        SELECT COUNT(*),
                               datetime(MIN(create_time), 'unixepoch', 'localtime'),
                               datetime(MAX(create_time), 'unixepoch', 'localtime')
                        FROM {table_name} WHERE create_time > 0
                    """).fetchone()
                    count, earliest, latest = row
                    if not count: continue
                    if table_name not in conversations:
                        conversations[table_name] = {"count": 0, "earliest": earliest, "latest": latest}
                    conversations[table_name]["count"] += count
                    if earliest and (not conversations[table_name]["earliest"] or earliest < conversations[table_name]["earliest"]):
                        conversations[table_name]["earliest"] = earliest
                    if latest and (not conversations[table_name]["latest"] or latest > conversations[table_name]["latest"]):
                        conversations[table_name]["latest"] = latest
                except Exception: continue
        finally:
            conn.close()

    contact_map = {}
    try:
        conn = sqlite3.connect(CONTACT_DB)
        for username, nick, remark, alias in conn.execute("SELECT username, nick_name, remark, alias FROM contact"):
            h = hashlib.md5(username.encode()).hexdigest()
            table = f"Msg_{h}"
            display = remark if remark else nick
            contact_map[table] = (username, display)
        conn.close()
    except Exception: pass

    sorted_convs = sorted(conversations.items(), key=lambda x: x[1]["count"], reverse=True)
    print(f"\n{'排名':<4} {'消息数':<8} {'时间范围':<45} {'显示名':<20} {'用户名'}")
    print("-" * 120)
    for i, (table, info) in enumerate(sorted_convs[:top_n], 1):
        username, display = (contact_map[table] if table in contact_map else (table, "(?)"))
        print(f"{i:<4} {info['count']:<8} {f'{info['earliest']} ~ {info['latest']}':<45} {str(display):<20} {username}")
    print(f"\n共 {len(conversations)} 个会话")


def export_chat(contact_username, contact_display_name, output_dir):
    """Export all messages for a contact."""
    table_hash = hashlib.md5(contact_username.encode()).hexdigest()
    table_name = f"Msg_{table_hash}"
    is_group = contact_username.endswith("@chatroom")

    os.makedirs(output_dir, exist_ok=True)
    global MY_WXID
    if not MY_WXID: MY_WXID = detect_my_wxid()

    all_messages = []
    for db_path in get_message_dbs():
        conn = sqlite3.connect(db_path)
        try:
            name2id = {row[0]: row[1] for row in conn.execute("SELECT rowid, user_name FROM Name2Id")}
            rows = conn.execute(f"SELECT local_type, create_time, real_sender_id, message_content FROM {table_name}").fetchall()
            for row in rows:
                m_type, m_time, s_id, m_content = row
                sender_wxid = name2id.get(s_id, "")
                
                content = m_content or ""
                if isinstance(content, bytes): content = "[压缩内容]"

                if sender_wxid == MY_WXID:
                    sender = MY_NAME
                elif m_type in (10000, 10002):
                    sender = "系统"
                elif is_group and ":" in content:
                    # Group message: sender_wxid:\ncontent
                    parts = content.split(":\n", 1)
                    if len(parts) > 1 and (parts[0].startswith("wxid_") or "@chatroom" not in parts[0]):
                        sender_wxid = parts[0]
                        content = parts[1]
                    sender = get_contact_name(sender_wxid)
                else:
                    sender = contact_display_name

                all_messages.append({
                    "time": datetime.fromtimestamp(m_time, tz=CST).strftime("%Y-%m-%d %H:%M:%S") if m_time else "",
                    "timestamp": m_time,
                    "sender": sender,
                    "type": m_type,
                    "type_name": MSG_TYPES.get(m_type, f"未知({m_type})"),
                    "content": content,
                })
        except Exception: pass
        finally: conn.close()

    all_messages.sort(key=lambda x: x["timestamp"] or 0)
    if not all_messages:
        print(f"未找到与 {contact_display_name} 的聊天记录")
        return

    txt_path = os.path.join(output_dir, "chat.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"微信聊天记录: {contact_display_name} ({contact_username})\n")
        f.write(f"总消息数: {len(all_messages)}\n\n" + "=" * 60 + "\n\n")
        for m in all_messages:
            c = f"[{m['type_name']}]" if (m["type"] in MEDIA_TYPES or (m["type"] != 1 and not m["content"])) else m["content"]
            f.write(f"[{m['time']}] {m['sender']}: {c}\n")
    print(f"  已导出: {txt_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", "-n")
    parser.add_argument("--username", "-u")
    parser.add_argument("--output", "-o", default="./exported")
    parser.add_argument("--list", "-l", action="store_true")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--my-wxid")
    args = parser.parse_args()

    if args.my_wxid:
        global MY_WXID
        MY_WXID = args.my_wxid

    if args.list:
        list_conversations(args.top)
        return

    if args.username:
        results = find_contact(args.username)
        username = results[0][0] if results else args.username
        display = (results[0][2] or results[0][1]) if results else args.username
        export_chat(username, display, args.output)
    elif args.name:
        results = find_contact(args.name)
        if not results: print("未找到联系人"); return
        if len(results) == 1:
            export_chat(results[0][0], results[0][2] or results[0][1], args.output)
        else:
            for i, r in enumerate(results, 1): print(f"{i}. {r[2] or r[1]} ({r[0]})")
            idx = int(input("请选择: ")) - 1
            export_chat(results[idx][0], results[idx][2] or results[idx][1], args.output)

if __name__ == "__main__":
    main()
