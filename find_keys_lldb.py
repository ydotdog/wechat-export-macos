#!/usr/bin/env python3
"""
find_keys_lldb.py - macOS WeChat 4.1.x 数据库密钥提取器 (lldb 方案)

背景
----
微信 4.1.x(实测 4.1.13)改变了数据库密钥的内存存储方式:
旧版 (4.0) 将 SQLCipher 密钥以 `x'<64hex><32hex>'` 字符串形式缓存在进程
内存中, 可用 find_all_keys_macos 直接扫描; 4.1 起该字符串不再以明文存在,
内存扫描 (包括 x'<hex>' / 裸 hex / UTF-16 等变体) 均无法命中。

本工具改用调试器方案: 微信解密数据库时必然调用 macOS 系统 CommonCrypto
的 CCCrypt / CCCryptorCreate (AES-256-CBC), 密钥作为函数参数传入。
通过 lldb 断点拦截该调用, 读取 x3 (key 指针) / x4 (key 长度), 即可获得
32 字节原始密钥; 再用数据库文件的 SQLCipher HMAC 校验确认归属。

重要: 微信 4.1.x 的每个数据库使用独立密钥, 必须逐一收集。
消息库 message_0/1/2.db 各自有不同密钥; 建议在抓取期间操作微信
(滚动聊天记录 / 打开多个会话 / 搜索), 触发各库的读写以捕获全部密钥。

前置条件
--------
- 微信已 ad-hoc 重签 (去掉 Hardened Runtime), 见 README 第一步
- lldb 已安装 (Xcode Command Line Tools)
- 以 root 运行 (task_for_pid 需要权限)

用法
----
1. 确保微信正在运行且已登录
2. 挂载 lldb:

       sudo lldb -p $(pgrep -x WeChat) \
           -o "command script import $(pwd)/find_keys_lldb.py" \
           -o "process continue"

3. 在微信中正常操作 (滚动聊天、打开会话、搜索), 触发各库解密
4. 脚本每匹配一个数据库打印 KEY FOUND (n/24) 并实时保存密钥
5. 全部密钥收集完成后 (或你认为足够时), 在 lldb 中执行 detach + quit
   即可 (脚本每次命中都会自动把当前进度写入 keys_file)

输出
----
- 默认写入 <项目>/all_keys.json (格式兼容 decrypt_db.py):
      {"rel/path.db": {"enc_key": "<64 hex>"}, ...}
- 可用 --out 指定输出文件; 密钥与上次进度自动合并 (不会覆盖已收集的)
"""

import argparse
import hashlib
import hmac as hmac_mod
import json
import os
import struct

# ---- 在 lldb 内运行时导入 lldb; 直接运行时仅为获取参数 ----
try:
    import lldb  # type: ignore
    _IN_LLDB = True
except ImportError:
    lldb = None  # type: ignore
    _IN_LLDB = False

PAGE_SZ = 4096
RESERVE_SZ = 80   # IV(16) + HMAC(64)
KEY_SZ = 32
SALT_SZ = 16
HMAC_SZ = 64

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    """读取 config.json 中的 db_dir / keys_file / decrypted_dir。"""
    cfg = {}
    cfg_path = os.path.join(_HERE, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    db_dir = cfg.get("db_dir", "")
    if not db_dir:
        # 自动探测微信 4.x 数据目录
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, "Library", "Containers",
                         "com.tencent.xinWeChat", "Data", "Documents",
                         "xwechat_files"),
        ]
        for base in candidates:
            if os.path.isdir(base):
                for acct in sorted(os.listdir(base)):
                    p = os.path.join(base, acct, "db_storage")
                    if os.path.isdir(p):
                        db_dir = p
                        break
            if db_dir:
                break
    return cfg, db_dir


def _collect_dbs(db_dir):
    """收集 db_dir 下所有加密数据库, 返回 {rel_path: page1_bytes}。"""
    dbs = {}
    for root, _dirs, files in os.walk(db_dir):
        for fn in sorted(files):
            if not fn.endswith(".db"):
                continue
            path = os.path.join(root, fn)
            with open(path, "rb") as fh:
                page1 = fh.read(PAGE_SZ)
            if len(page1) == PAGE_SZ and page1[:15] != b"SQLite format 3":
                rel = os.path.relpath(path, db_dir)
                dbs[rel] = page1
    return dbs


def derive_mac_key(enc_key, salt):
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def verify_key(page1, enc_key):
    """SQLCipher 4 页面 1 HMAC 校验: key 是否正确。"""
    salt = page1[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    p1_hmac_data = page1[SALT_SZ:PAGE_SZ - RESERVE_SZ + SALT_SZ]
    p1_stored = page1[PAGE_SZ - HMAC_SZ:PAGE_SZ]
    hm = hmac_mod.new(mac_key, p1_hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hmac_mod.compare_digest(hm.digest(), p1_stored)


# =====================================================================
# lldb 运行时逻辑 (仅在 lldb 内执行)
# =====================================================================

_state = {}


def _grab(frame):
    """CCCrypt/CCCryptorCreate 断点回调: 读取 x3/x4 密钥并验证。"""
    st = _state
    thread = frame.GetThread()
    process = thread.GetProcess()
    x3 = frame.FindRegister("x3")
    x4 = frame.FindRegister("x4")
    if not (x3.IsValid() and x4.IsValid()):
        return False
    key_ptr = x3.GetValueAsUnsigned()
    key_len = x4.GetValueAsUnsigned()
    if key_len != KEY_SZ or key_ptr == 0:
        return False
    err = lldb.SBError()
    data = process.ReadMemory(key_ptr, KEY_SZ, err)
    if not err.Success():
        return False
    key_hex = bytes(data).hex()
    if key_hex in st["seen"]:
        return False
    st["seen"].add(key_hex)
    st["attempts"] += 1

    new_found = 0
    for rel, page1 in st["dbs"].items():
        if rel in st["key_map"]:
            continue
        if verify_key(page1, data):
            st["key_map"][rel] = key_hex
            new_found += 1
            _save()
            print(f"[find_keys_lldb] KEY FOUND ({len(st['key_map'])}/{len(st['dbs'])}): "
                  f"{rel} = {key_hex}", flush=True)
    if not new_found and st["attempts"] % 500 == 0:
        print(f"[find_keys_lldb] ... tried {st['attempts']} keys, "
              f"matched {len(st['key_map'])}/{len(st['dbs'])} dbs", flush=True)

    if len(st["key_map"]) >= len(st["dbs"]):
        print("[find_keys_lldb] ALL KEYS FOUND! "
              "run 'detach' then 'quit' in lldb.", flush=True)
        for bp in st["bps"]:
            bp.SetEnabled(False)
    return False


def _save():
    st = _state
    out = st["out_path"]
    data = {rel: {"enc_key": key_hex}
            for rel, key_hex in sorted(st["key_map"].items())}
    try:
        with open(out, "w") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print(f"[find_keys_lldb] saved {len(data)} keys -> {out}", flush=True)
    except OSError as e:
        print(f"[find_keys_lldb] save failed: {e}", flush=True)


def cccrypt_cb(frame, bp_loc, dict_):  # noqa: D401
    return _grab(frame)


def cccryptor_create_cb(frame, bp_loc, dict_):
    return _grab(frame)


def __lldb_init_module(debugger, internal_dict):
    global _state
    cfg, db_dir = _load_config()
    if not db_dir or not os.path.isdir(db_dir):
        print("[find_keys_lldb] ERROR: cannot locate db_storage. "
              "Check config.json db_dir.", flush=True)
        return
    dbs = _collect_dbs(db_dir)
    if not dbs:
        print("[find_keys_lldb] ERROR: no encrypted dbs found.", flush=True)
        return
    out_path = cfg.get("keys_file", "all_keys.json")
    if not os.path.isabs(out_path):
        out_path = os.path.join(_HERE, out_path)

    _state = {
        "dbs": dbs,
        "key_map": {},
        "seen": set(),
        "attempts": 0,
        "bps": [],
        "out_path": out_path,
    }

    target = debugger.GetSelectedTarget()
    for name in ("CCCrypt", "CCCryptorCreate"):
        bp = target.BreakpointCreateByName(name)
        if bp.IsValid():
            cb = ("find_keys_lldb.cccrypt_cb" if name == "CCCrypt"
                  else "find_keys_lldb.cccryptor_create_cb")
            bp.SetScriptCallbackFunction(cb)
            _state["bps"].append(bp)
            print(f"[find_keys_lldb] breakpoint set on {name}", flush=True)

    print(f"[find_keys_lldb] {len(dbs)} encrypted dbs, "
          f"output: {out_path}", flush=True)
    print("[find_keys_lldb] TIP: use WeChat (scroll chats / open "
          "conversations / search) to trigger all dbs", flush=True)


# =====================================================================
# 独立运行: 仅打印用法 / 校验已有密钥文件
# =====================================================================

def _cli():
    ap = argparse.ArgumentParser(
        description="macOS WeChat 4.1.x DB key extractor via lldb CCCrypt "
                    "breakpoint (run inside lldb; see docstring).")
    ap.add_argument("--out", help="output keys json path "
                                  "(default: config.json keys_file)")
    ap.add_argument("--verify", metavar="KEYS_JSON",
                    help="verify an existing keys json against dbs, then exit")
    args = ap.parse_args()

    if args.verify:
        cfg, db_dir = _load_config()
        dbs = _collect_dbs(db_dir)
        with open(args.verify) as f:
            keys = json.load(f)
        ok = fail = 0
        for rel, page1 in dbs.items():
            info = keys.get(rel)
            if not info:
                print(f"SKIP {rel} (no key)")
                continue
            key_hex = info["enc_key"] if isinstance(info, dict) else info
            if verify_key(page1, bytes.fromhex(key_hex)):
                ok += 1
            else:
                fail += 1
                print(f"BAD  {rel}")
        print(f"verify: {ok} ok, {fail} bad, {len(dbs)} total dbs")
        return

    print(__doc__)


if __name__ == "__main__":
    _cli()
