# Google One 合作商优惠链接提取工具

从已 root 的 Pixel 手机里，把 Google One 的 partner-offer 领取链接批量提取出来，
并统一转换为 `https://one.google.com/offer/...` 格式。

缓存文件位置：

```
/data/user/0/com.google.android.apps.subscriptions.red/files/accounts/<id>/SqliteKeyValueCache:PartnerOfferEligibilityCache.db
```

文件名里有 `:`，Windows 不能直接保存。需要完整拉库调试时，脚本会先复制到
`/sdcard/` 再 `adb pull` 下来；默认快扫不拉完整 DB。

## 前置条件

- Python 3.8+
- `adb`（Android platform-tools）。三种方式任选：
  1. **加到系统 PATH**（永久）：`setx PATH "%PATH%;C:\path\to\platform-tools"`，重开终端
  2. **只给当前会话**：PowerShell 里 `$env:Path += ";C:\path\to\platform-tools"`
  3. **不改 PATH**：设环境变量 `ADB_PATH` 指到 `adb.exe`，或在 Web UI 的「设置」里填路径（会存到 `.settings.json`，下次自动加载）
- 手机：
  - 已 root（Magisk / KernelSU），并且在管理器里允许 **Shell** 使用 root
  - 已开 USB 调试并授权这台电脑
  - Google One App 已登录，最好先打开一次优惠页让缓存生成

## 使用方式一：Web UI（推荐）

```powershell
pip install -r requirements.txt
python app.py
```

启动后会自动打开 [http://127.0.0.1:7373](http://127.0.0.1:7373) 。界面里能：

- 查看 adb / 设备 / root 状态徽章
- 多设备时下拉选择要操作的序列号
- 一键「扫描链接」，结果按账户分组列出
- 每条链接支持「复制」和「在默认浏览器打开」
- 「准备 Chrome」会在手机端打开 Chrome 并自动点已有账号授权，再刷新网页登录 token
- 每条 partner 链接支持「自动领取」，会在本机 Chromium 注入登录 Cookie 后打开
- 勾选 **匹配所有 one.google.com 链接** 可以放宽匹配

## 使用方式二：命令行

```powershell
# 最常用：打印所有 offer 链接
python extract_offers.py

# 多设备时指定序列号
python extract_offers.py --serial 1A2B3C4D

# 放宽匹配（任意 one.google.com 链接）
python extract_offers.py --all-urls

# 保留拉下来的 db 文件方便自己分析
python extract_offers.py --keep --out-dir .\dbs

# 强制使用旧的 adb pull + 本地 SQLite/原始字节扫描方式
python extract_offers.py --pull-db
```

默认会优先走快扫路径：在手机端用 `strings | grep one.google.com`
只返回疑似包含链接的文本，再在本机用同一套正则精确提取。这样避免每次
把整个缓存 DB 复制到 `/sdcard/` 再 `adb pull`。使用 `--keep`、`--out-dir`
或 `--pull-db` 时会回到旧的完整 DB 拉取方式，方便排查。

## 输出示例

```
[*] Found 1 account dir(s): 3f2a...
[+] account 3f2a...: 2 url(s)

============================================================
Extracted links:
============================================================

# account 3f2a...
https://one.google.com/offer/XXXX?source=...
https://one.google.com/offer/YYYY?source=...
```

把链接复制到浏览器（需要同 Google 账号登录态）就能走领取流程。

## 自动领取

Web UI 的「刷新 tokens」会做两件事：

1. 从手机 root 数据库里读取 Google 账号信息、Android ID 和 Chrome 已缓存的
   `OAuthLogin` token。
2. 如果没有 OAuthLogin token，先点「准备 Chrome」：脚本会在手机端打开 Chrome，
   自动点击常见的已有账号登录/同意/继续按钮，然后再刷新 token。

手机需要保持解锁。遇到验证码、密码、异常安全提示时不会处理，需要手动接管。
拿到 token 后，电脑端会用它换取 Google 网页 Cookie，并由「自动领取」在本机
Chromium 里打开优惠链接。

## 原理

该 db 是 Google 内部的 `SqliteKeyValueCache` —— 一个 key/value 表，
value 是 protobuf 序列化的字节。默认快扫做两步：

1. 在手机端对缓存 DB 跑 `strings | grep one.google.com`，只把疑似包含链接的文本传回本机。
2. 本机再用正则从这些文本里精确提取 URL。

如果加 `--pull-db`、`--keep` 或 `--out-dir`，会走旧的完整扫描：

1. 用 `sqlite3` 只读打开，遍历所有表所有列，把 BLOB/TEXT 值里匹配正则的 URL 抽出来。
2. 再对整个文件做一遍原始字节正则，兜底未提交到主文件的 WAL/free page 里的数据。

最后去重按账号分组打印。

## 常见问题

- `**Root (su) is not available**`：打开 Magisk，超级用户里给 `Shell` 授权。
- `**No accounts found**`：先打开 Google One 登录一下，或确认包名没变
（部分机型上是 `com.google.android.apps.subscriptions.red`）。
- **没匹配到链接**：可能缓存还没下发。在 App 里点一下「优惠 / Offers」
页刷一次，再跑脚本；或用 `--all-urls` 看看里面都有什么。

