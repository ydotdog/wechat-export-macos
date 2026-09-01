#!/usr/bin/env python3
"""
导出微信群聊成员昵称

从解密后的 contact/contact.db 读取群聊及其成员, 导出每个群的成员
显示名(备注优先, 其次昵称)、用户名(username)到 JSON/CSV/TXT。

用法:
    python3 export_chat_members.py [--format json|csv|txt] [--output DIR]
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime

CONTACT_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "decrypted", "contact", "contact.db")


def display_name(row):
    """备注优先, 其次昵称; 都没有则返回用户名或空。"""
    remark = (row[2] or "").strip()
    nick = (row[3] or "").strip()
    if remark:
        return remark
    if nick:
        return nick
    return row[0] or ""


def load_groups(conn):
    """返回 {room_username: {'id':.., 'members': [...]}}"""
    groups = {}
    for room_id, room_user in conn.execute("SELECT id, username FROM chat_room"):
        groups[room_user] = {"id": room_id, "members": []}

    # 成员明细: contact 表主键 id -> (username, local_type, remark, nick_name)
    contact = {}
    for cid, username, ltype, remark, nick in conn.execute(
            "SELECT id, username, local_type, remark, nick_name FROM contact"):
        contact[cid] = (username, ltype, remark, nick)

    for room_id, member_id in conn.execute(
            "SELECT room_id, member_id FROM chatroom_member"):
        # room_id -> room username
        room_user = None
        for u, g in groups.items():
            if g["id"] == room_id:
                room_user = u
                break
        if room_user is None:
            continue
        if member_id in contact:
            c = contact[member_id]
            groups[room_user]["members"].append({
                "username": c[0],
                "local_type": c[1],
                "remark": c[2],
                "nick_name": c[3],
                "display_name": display_name(c),
            })
    return groups


def main():
    ap = argparse.ArgumentParser(description="导出微信群聊成员昵称")
    ap.add_argument("--format", choices=["json", "csv", "txt", "all"],
                    default="all", help="输出格式 (默认 all)")
    ap.add_argument("--output", "-o", default=None,
                    help="输出目录(默认: 项目下 export_members)")
    ap.add_argument("--group", "-g", default=None,
                    help="只导出指定群(模糊匹配群名/群 username)")
    args = ap.parse_args()

    if not os.path.exists(CONTACT_DB):
        print(f"错误: 找不到解密后的联系人库 {CONTACT_DB}")
        print("请先运行 python3 decrypt_db.py")
        sys.exit(1)

    conn = sqlite3.connect(CONTACT_DB)
    groups = load_groups(conn)
    conn.close()

    # 群显示名: 群本身在 contact 表有 nickname/remark
    conn = sqlite3.connect(CONTACT_DB)
    room_names = {}
    for username, nick, remark in conn.execute(
            "SELECT username, nick_name, remark FROM contact"):
        room_names[username] = (remark or "").strip() or (nick or "").strip() or username
    conn.close()

    # 过滤
    if args.group:
        groups = {u: g for u, g in groups.items()
                  if args.group in u or args.group in room_names.get(u, "")}

    if not groups:
        print("没有匹配的群")
        sys.exit(1)

    total_rooms = len(groups)
    total_members = sum(len(g["members"]) for g in groups.values())

    out_dir = args.output or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "export_members")
    os.makedirs(out_dir, exist_ok=True)

    fmt = args.format
    if fmt == "all":
        fmts = ["json", "csv", "txt"]
    else:
        fmts = [fmt]

    for f in fmts:
        if f == "json":
            path = os.path.join(out_dir, "chat_members.json")
            data = {}
            for u, g in sorted(groups.items()):
                data[room_names.get(u, u)] = {
                    "username": u,
                    "member_count": len(g["members"]),
                    "members": g["members"],
                }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            print(f"JSON: {path}")

        elif f == "csv":
            path = os.path.join(out_dir, "chat_members.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["群名", "群username", "成员显示名", "备注",
                            "昵称", "成员username"])
                for u, g in sorted(groups.items()):
                    for m in sorted(g["members"], key=lambda x: x["display_name"]):
                        w.writerow([room_names.get(u, u), u,
                                    m["display_name"], m["remark"],
                                    m["nick_name"], m["username"]])
            print(f"CSV: {path}")

        elif f == "txt":
            path = os.path.join(out_dir, "chat_members.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"微信群聊成员导出  {datetime.now():%Y-%m-%d %H:%M:%S}\n")
                fh.write(f"群数: {total_rooms}  成员记录: {total_members}\n")
                fh.write("=" * 60 + "\n\n")
                for u, g in sorted(groups.items(),
                                   key=lambda x: -len(x[1]["members"])):
                    fh.write(f"■ {room_names.get(u, u)}  "
                             f"({u})  成员 {len(g['members'])} 人\n")
                    for m in sorted(g["members"], key=lambda x: x["display_name"]):
                        fh.write(f"   {m['display_name']}"
                                 f"  [{m['username']}]\n")
                    fh.write("\n")
            print(f"TXT: {path}")

    print(f"\n导出完成: {total_rooms} 个群, {total_members} 条成员记录 -> {out_dir}")


if __name__ == "__main__":
    main()
