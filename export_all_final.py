#!/usr/bin/env python3
import sqlite3, os, hashlib, argparse, re, shutil, sys
from datetime import datetime, timezone, timedelta

# ================= 配置区 =================
# 您的身份信息
MY_WXID_PREFIX = "wxid_nmcyz2pvrtdr22"
MY_DISPLAY_NAME = "黄俊"
# 导出总目录
EXPORT_ROOT = os.path.expanduser("~/Downloads/WeChat_All_Exports_Final")
# ==========================================

CST = timezone(timedelta(hours=8))
MSG_TYPES = {1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情", 48: "位置", 49: "链接/文件/小程序", 50: "语音/视频通话", 51: "系统消息", 10000: "系统提示", 10002: "撤回消息"}

# 自动从当前目录加载配置
try:
    from config import load_config
    _cfg = load_config()
    DECRYPTED_DIR = _cfg["decrypted_dir"]
    CONTACT_DB = os.path.join(DECRYPTED_DIR, "contact", "contact.db")
except:
    print("错误: 找不到 config.py 或解密目录配置")
    sys.exit(1)

CONTACT_CACHE = {}

def get_contact_name(wxid, db_conn=None):
    if not wxid: return "未知"
    if MY_WXID_PREFIX in wxid: return MY_DISPLAY_NAME
    if wxid in CONTACT_CACHE: return CONTACT_CACHE[wxid]
    try:
        conn = db_conn if db_conn else sqlite3.connect(CONTACT_DB)
        row = conn.execute("SELECT nick_name, remark FROM contact WHERE username = ?", (wxid,)).fetchone()
        if not db_conn: conn.close()
        if row:
            name = row[1] if row[1] else row[0]
            CONTACT_CACHE[wxid] = name
            return name
    except: pass
    return wxid

def export_single_chat(username, display_name):
    h = hashlib.md5(username.encode()).hexdigest()
    table_name = f"Msg_{h}"
    is_group = username.endswith("@chatroom")
    
    all_messages = []
    msg_dir = os.path.join(DECRYPTED_DIR, "message")
    contact_conn = sqlite3.connect(CONTACT_DB)

    for f in sorted(os.listdir(msg_dir)):
        if not f.startswith("message_") or not f.endswith(".db") or "fts" in f: continue
        db_path = os.path.join(msg_dir, f)
        conn = sqlite3.connect(db_path)
        try:
            name2id = {row[0]: row[1] for row in conn.execute("SELECT rowid, user_name FROM Name2Id")}
            rows = conn.execute(f"SELECT local_type, create_time, real_sender_id, message_content, status FROM {table_name}").fetchall()
            for row in rows:
                m_type, m_time, s_id, m_content, m_status = row
                sender_wxid = name2id.get(s_id, "")
                
                # 发送者判定
                sender = ""
                if m_type in (10000, 10002):
                    sender = "系统"
                elif m_status == 2 or (sender_wxid and MY_WXID_PREFIX in sender_wxid):
                    sender = MY_DISPLAY_NAME
                elif is_group:
                    content_str = str(m_content) if not isinstance(m_content, bytes) else ""
                    actual_wxid = sender_wxid
                    if ":\n" in content_str:
                        idx = content_str.find(":\n")
                        potential = content_str[:idx]
                        if 1 < len(potential) < 50 and " " not in potential:
                            actual_wxid = potential
                            m_content = content_str[idx+2:]
                    sender = get_contact_name(actual_wxid, contact_conn)
                else:
                    sender = display_name

                content = str(m_content) if not isinstance(m_content, bytes) else "[媒体内容]"
                all_messages.append({
                    "time": datetime.fromtimestamp(m_time, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
                    "ts": m_time,
                    "sender": sender,
                    "type": m_type % 1000,
                    "content": content
                })
        except: pass
        finally: conn.close()
    
    contact_conn.close()
    if not all_messages: return False
    
    all_messages.sort(key=lambda x: x["ts"])
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', display_name or username)
    txt_path = os.path.join(EXPORT_ROOT, f"{safe_name}.txt")
    
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"聊天对象: {display_name} ({username})\n\n")
        for m in all_messages:
            body = f"[{MSG_TYPES.get(m['type'], '未知')}]" if (m['type'] != 1 and m['type'] != 10000) else m['content']
            f.write(f"[{m['time']}] {m['sender']}: {body}\n")
    return True

def main():
    if os.path.exists(EXPORT_ROOT): shutil.rmtree(EXPORT_ROOT)
    os.makedirs(EXPORT_ROOT)
    
    print("正在从通讯录加载会话列表...")
    conn = sqlite3.connect(CONTACT_DB)
    contacts = conn.execute("SELECT username, nick_name, remark FROM contact").fetchall()
    conn.close()
    
    count = 0
    for username, nick, remark in contacts:
        # 跳过一些系统内置 ID
        if username.startswith("gh_") or not username: continue
        
        display = remark if remark else nick
        if export_single_chat(username, display):
            count += 1
            if count % 10 == 0: print(f"已导出 {count} 个会话...")

    print(f"\n全部完成！共导出 {count} 个有效会话。")
    print(f"文件存放在: {EXPORT_ROOT}")

if __name__ == "__main__":
    main()
