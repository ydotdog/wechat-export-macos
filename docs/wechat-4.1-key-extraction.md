# macOS 微信 4.1.x 数据库密钥提取方法学

本文档记录微信 4.1.x(实测 4.1.13,2026-08 版本)数据库密钥提取的完整方法,
以及旧方法失效的原因分析。适用于 macOS (Apple Silicon) 上的微信桌面版。

## 背景

微信桌面版 4.x 使用 WCDB(基于 SQLCipher 4)加密本地 SQLite 数据库。
每个 `.db` 文件使用独立的 AES-256-CBC 密钥 + 16 字节 salt,格式为:

```
page 1: [salt 16B][AES-256-CBC 密文 4000B][IV 16B][HMAC-SHA512 64B]
其他页: [AES-256-CBC 密文 4016B][IV 16B][HMAC-SHA512 64B]
```

- 页面大小 4096,reserve = 80(IV 16 + HMAC 64)
- 密钥 32 字节,直接作为 AES 密钥(raw-key 模式,无 passphrase KDF)
- HMAC 密钥由 `PBKDF2-HMAC-SHA512(key, salt XOR 0x3A, 2, 32)` 派生
- 页面 1 HMAC 覆盖 `page[16:4032] + LE32(pageno=1)`

`decrypt_db.py` 实现了上述参数,`export_chat.py` 负责导出。

## 旧方法(4.0)及其在 4.1 上的失效

### 旧方法原理

微信 4.0 会在进程内存中以 SQLCipher raw-key 字面量形式缓存密钥:

```
x'<64 hex key><32 hex salt>'
```

`find_all_keys_macos` 通过 Mach VM API 读取微信进程内存,匹配
`x'` + 96 个 hex 字符 + `'` 模式,再用数据库文件头 16 字节 salt 校验候选,
输出 `all_keys.json`。

### 4.1 上的失效现象

在微信 4.1.13 上实测(2026-08-29):

| 扫描模式 | 结果 |
|---|---|
| `x'<96hex>'`(旧格式) | 0 命中 |
| `x'<hex>'` 任意长度(含 `X'`/`x"`/`X"` 变体) | 0 命中 |
| 裸 96/64 hex 连续串 | 数十~数百个,全部 HMAC 校验失败 |
| UTF-16LE 编码的 `x'<hex>'` | 0 命中 |
| 内存中的 salt 二进制(16B)命中点附近 ±512B 窗口内 32B 块 | 0 命中 |

结论:4.1 不再以 `x'...'` 字符串(或任何 hex 文本形式)在内存中缓存密钥。
对 `wechat.dylib`(345MB,内嵌 SQLCipher)的符号分析显示 `sqlite3_*`/
`sqlcipher_*` 符号已被 strip,无法直接对 `sqlite3_key_v2` 下断点。

## 新方法:lldb 断点 CommonCrypto CCCrypt

### 原理

微信解密数据库页面时,最终必然调用 macOS 系统库 CommonCrypto 的
`CCCrypt`(或分步式 `CCCryptorCreate`/`CCCryptorUpdate`)执行
AES-256-CBC 解密。密钥以 32 字节原始二进制作为函数参数传入:

```c
CCCryptorStatus CCCrypt(
    CCOperation op,          // x0
    CCAlgorithm alg,         // x1 (0 = AES)
    CCOptions options,       // x2
    const void *key,         // x3  <-- 32 字节密钥指针
    size_t keyLength,        // x4  <-- 应为 32
    const void *iv,          // x5
    const void *dataIn,      // x6
    size_t dataInLength,     // x7
    void *dataOut, ...)
```

通过 lldb 断点拦截该调用,读取寄存器 x3(key 指针)、x4(key 长度),
即可拿到明文密钥。

### 为什么每个库需要独立收集

实测确认微信 4.1.x **每个数据库使用独立密钥**(并非所有库共享一把):

```
session/session.db     = <64 hex, 与 message/contact 均不同>
message/message_2.db   = <64 hex, 独立密钥>
contact/contact.db     = <64 hex, 独立密钥>
...
```

因此抓取密钥时需要在微信中操作,触发各库读写
(滚动聊天记录、打开会话、搜索、查看朋友圈/表情等),
断点回调会对每个候选密钥做 HMAC 校验并记录归属。

### 前置条件

1. 微信已 ad-hoc 重签(移除 Hardened Runtime),见 README 第一步
2. lldb 已安装(Xcode Command Line Tools:`xcode-select --install`)
3. 以 root 运行(task_for_pid 需要权限)
4. 微信正在运行且已登录

### 操作步骤

```bash
# 1. 确保微信运行中, 然后挂载 lldb
sudo lldb -p $(pgrep -x WeChat) \
    -o "command script import $(pwd)/find_keys_lldb.py" \
    -o "process continue"

# 2. 在微信中正常操作 2~5 分钟, 触发各数据库解密:
#    - 滚动聊天记录(触发 message_0/1/2.db)
#    - 打开多个会话窗口
#    - 使用搜索功能
#    - 查看朋友圈(触发 sns.db)

# 3. 观察 lldb 输出, 每匹配一个库打印:
#    [find_keys_lldb] KEY FOUND (n/24): <rel> = <64hex>

# 4. 全部收集完成后 lldb 打印 ALL KEYS FOUND!,
#    或在你认为足够时手动执行:
(lldb) detach
(lldb) quit
```

密钥实时写入 `config.json` 指定的 `keys_file`(默认 `all_keys.json`),
格式与 `decrypt_db.py` 兼容:

```json
{
  "message/message_2.db": {"enc_key": "<64 hex>"},
  "session/session.db":   {"enc_key": "<64 hex>"}
}
```

### 验证已有密钥

```bash
python3 find_keys_lldb.py --verify all_keys.json
# verify: 7 ok, 0 bad, 24 total dbs
```

### 常见问题

| 现象 | 处理 |
|---|---|
| lldb 挂载后无 KEY FOUND 输出 | 在微信里多操作(滚动/搜索),触发数据库读写 |
| 只收集到部分库 | 属正常,先导出已有的; 不常用库(hardlink/solitaire 等)可能需更久 |
| `task_for_pid failed` | 确认用 sudo 运行、微信已重签 |
| 断点命中时微信界面卡顿 | 正常,每次命中仅暂停微秒级 |

## 导出流程(拿到密钥后)

```bash
# 1. 解密(自动读取 all_keys.json)
python3 decrypt_db.py

# 2. 列出所有会话
python3 export_chat.py --list

# 3. 导出单个联系人
python3 export_chat.py --name "张三" --output ~/Downloads/zhangsan
python3 export_chat.py --username wxid_xxx --output ~/Downloads/out
```

已知限制(见 README 常见问题):
- 部分消息显示 `[压缩内容]`(WCDB zstd 压缩,未解压)
- 图片/语音/视频等媒体文件需 `message_resource.db` 密钥关联路径提取

## 参考

- [L1en2407/wechat-decrypt](https://github.com/L1en2407/wechat-decrypt) — C 内存扫描器(4.0)
- [Thearas/wechat-db-decrypt-macos](https://github.com/Thearas/wechat-db-decrypt-macos) — lldb 提取方案
- [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) — 原始内存搜索
- `rmqg/wechat-mac-export` — 4.1.x 上基于 lldb CCCrypt 断点的社区方案(本方法学的思路来源)

> 注意: 上述多个仓库已被 DMCA 下架或自我删库, 本实现为独立复现。
