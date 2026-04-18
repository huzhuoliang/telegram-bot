[English](README.md) | **中文**

# telegram-monitor

个人 Telegram 机器人服务。部署在你的服务器上，通过长轮询接收 Telegram 消息，并允许本地任意脚本向你的 Telegram 账号发送通知。

## 功能

- **通知推送** — 其他脚本/应用通过 POST 本地 HTTP 接口发送消息、图片或视频
- **Shell 执行** — 发送 `!<命令>` 在服务器上执行并返回输出
- **Claude AI** — 发送 `?<问题>`（或任意文本）获取 AI 回复；Claude 还能搜索并内联发送图片/视频
- **特权 Claude** — 发送 `$<文本>` 使用拥有完整 Shell 和文件访问权限的 AI 助手，执行命令前需交互确认
- **视频下载** — 发送 `/dl <链接>` 下载抖音（无水印）、B站（4K/HDR）、YouTube 及其他 yt-dlp 支持的平台视频
- **邮件监控** — 基于 IMAP 的邮件监控，AI 智能分类（紧急/普通/垃圾），定时生成摘要报告
- **B站收藏夹监控** — 自动下载监控的 B站收藏夹中新增视频，支持持久化队列和 NAS rsync 同步
- **B站UP主监控** — 监控指定 B站 UP主 的新视频上传，支持仅通知或自动下载模式，WBI 签名 API，持久化队列和 NAS 同步
- **图片识别** — 发送图片附带文字说明，Claude 会分析图片内容（仅 API 后端）
- **预设回复** — 配置固定的关键词 → 回复对
- **媒体归档** — 转发图片/视频/文档给机器人，自动保存到服务器；使用 `/files` 浏览归档
- **LaTeX 渲染** — Claude 可在回复中渲染 LaTeX 公式为图片
- **调试监控** — 实时 TUI 查看 Telegram I/O、Claude API 调用、Shell 命令和路由决策（详见 [DEBUG.md](DEBUG.md)）
- **无需公网 IP** — 使用长轮询，无需 Webhook

## 环境要求

- Python 3.10+
- `requests` 和 `anthropic` 包（`pip install requests anthropic`）
- 一个 Telegram Bot Token（通过 [@BotFather](https://t.me/BotFather) 创建）
- 已安装并认证的 Claude Code CLI（用于默认的 `cli` 后端）

## 安装配置

**1. 保存凭据**

```bash
echo "YOUR_BOT_TOKEN" > TOKEN.txt
echo "YOUR_CHAT_ID" > CHAT_ID.txt
```

获取你的 Chat ID：先与机器人发起对话，然后访问
`https://api.telegram.org/bot<TOKEN>/getUpdates` 查找 `"chat":{"id":...}`。

**2. 运行**

```bash
python3 bot.py
```

机器人就绪后会向你的 Telegram 发送 "服务已启动。"。

## 使用方式

在 Telegram 中向机器人发送消息：

| 消息 | 动作 |
|------|------|
| `!ls -la /tmp` | 执行 Shell 命令，返回 stdout + stderr + 退出码 |
| `?explain DNS` | 询问 Claude，返回中文回复 |
| `搜索一张XXX的照片` | Claude 搜索图片并发送给你 |
| `$检查磁盘使用情况` | 特权 Claude — 可执行任意命令（需确认） |
| `$$部署应用` | 特权 Claude — 自动批准所有命令（免确认） |
| `/dl <链接>` | 下载抖音、B站、YouTube 等平台视频 |
| `/email` | 邮件监控状态；`/email digest`、`/email check` 等 |
| `/fav` | B站收藏夹监控；`/fav folders`、`/fav add`、`/fav download`、`/fav sync` 等 |
| `/up` | B站UP主监控；`/up add`、`/up download`、`/up mode`、`/up sync` 等 |
| `/files` | 浏览归档文件（分页 inline keyboard） |
| `/help` | 显示命令帮助 |
| `/status` | 查看当前 Claude 后端状态 |
| `/ctx` / `$ctx` | 查看普通 / 特权 Claude 上下文用量 |
| `/setkey <KEY>` | 设置 Anthropic API 密钥，切换到 API 后端 |
| `/setcli` | 切回 CLI 后端 |
| `!clear` 或 `/clear` | 清空 Claude 对话历史 |
| `$clear` | 清空特权 Claude 对话历史 |
| 图片 + 文字说明 | Claude 图片识别（仅 API 后端） |
| 图片 / 视频 / 文档 | 自动保存到服务器 `telegram_archive/` |
| 表情反应 | 机器人回复相同的 emoji |
| 其他任意文本 | 转发给 Claude |

## 从其他脚本发送通知

机器人运行时，任何本地进程都可以发送消息、图片或视频：

```bash
# 文本
python3 send.py "备份已完成"

# 图片（本地文件或 URL）
python3 send.py --photo /tmp/screenshot.png --caption "今日报表"
python3 send.py --photo "https://example.com/chart.png"

# 视频（本地文件或 URL；使用本地 Bot API 服务器时最大支持 2 GB）
python3 send.py --video /tmp/recording.mp4 --caption "录像"
python3 send.py --video "https://example.com/clip.mp4"
```

HTTP API（photo/video 支持本地文件路径或 URL）：

```bash
curl -X POST http://127.0.0.1:8765/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "部署完成"}'

curl -X POST http://127.0.0.1:8765/send_photo \
  -H 'Content-Type: application/json' \
  -d '{"photo": "/tmp/img.jpg", "caption": "可选说明"}'

curl -X POST http://127.0.0.1:8765/send_video \
  -H 'Content-Type: application/json' \
  -d '{"video": "/tmp/clip.mp4", "caption": "可选说明"}'
```

## 视频下载

发送 `/dl <链接>` 下载视频。支持平台：

| 平台 | 后端 | 说明 |
|------|------|------|
| **抖音** | TikTokDownloader API（Docker） | 无水印最高画质。可直接粘贴分享文本，自动提取链接。Cookie 通过 Playwright 自动刷新。 |
| **B站** | yt-dlp | 4K/HDR 优先。自动验证 Cookie，失效时触发扫码登录。匿名模式最高 1080p。 |
| **YouTube 及其他** | yt-dlp | 支持所有 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 兼容的网站。 |

下载完成后：
- 文件在上传限制内（云端 50 MB / 本地 Bot API 2 GB）→ 直接上传到 Telegram
- 超过限制 → 返回服务器本地路径
- AV1 编码的视频自动转码为 H.265（iPhone 兼容），带实时进度显示

需要安装 `yt-dlp` 和 `ffmpeg`。抖音下载还需要运行 `douyin-api` 服务（见 [Systemd 服务部署](#systemd-服务部署)）。

## 特权 Claude

发送 `$<文本>` 使用拥有完整系统访问权限的 AI 助手。与普通 Claude 不同，它可以：
- 执行**任意** Shell 命令（包括 `sudo`）
- 读写服务器上的**任意**文件

**安全机制：** 执行 Shell 命令前，机器人会发送确认消息，带有三个按钮：
- ✅ **允许一次** — 仅执行本次命令
- 📌 **加入白名单** — 执行并允许后续相同命令模式
- ❌ **拒绝** — 拒绝执行（60 秒超时自动拒绝）

发送 `$$<文本>` 自动批准该次会话中的所有命令（每条命令仍会静默通知）。

白名单管理：
```
$whitelist list              — 查看白名单
$whitelist add <命令或前缀*>  — 添加（如 ls* 表示前缀匹配）
$whitelist remove <序号>     — 按序号删除
```

## 邮件监控

基于 IMAP 的邮件监控，支持 AI 智能分类。需要在 `config.json` 中设置 `email_enabled: true`。

| 命令 | 动作 |
|------|------|
| `/email` | 显示监控状态和统计信息 |
| `/email digest` | 立即发送 AI 生成的邮件摘要 |
| `/email check` | 立即检查所有账号新邮件 |
| `/email pause` | 暂停监控 |
| `/email resume` | 恢复监控 |
| `/email send <收件人> <主题> <正文>` | 通过 SMTP 发送邮件 |

功能特点：
- 每封新邮件由 AI 自动分类为**紧急**、**普通**或**垃圾邮件**
- 紧急邮件立即推送 Telegram 提醒
- 定时生成 AI 邮件摘要报告（默认 6 小时间隔，可配置）
- 支持 IMAP IDLE 实时推送（QQ 邮箱除外）

账号配置格式参见 `email_credentials.json`。

## B站收藏夹监控

自动下载监控的 B站收藏夹中新增视频。需要在 `config.json` 中设置 `bilibili_fav_enabled: true`，并确保 B站 Cookie 有效（与 `/dl` 视频下载共用）。

| 命令 | 动作 |
|------|------|
| `/fav` | 查看监控状态 |
| `/fav folders` | 列出所有 B站收藏夹（含 ID） |
| `/fav list` | 查看当前监控中的收藏夹 |
| `/fav add <ID>` | 添加收藏夹监控（现有视频标记为已知） |
| `/fav remove <ID>` | 移除收藏夹监控 |
| `/fav download <ID>` | 全量下载收藏夹所有视频 |
| `/fav check` | 立即检查新视频 |
| `/fav sync` | 同步本地文件到 NAS |
| `/fav queue` | 查看下载队列（当前 + 等待） |
| `/fav pause` / `/fav resume` | 暂停/恢复监控 |
| `/fav history [N]` | 最近下载记录 |

功能特点：
- 可配置轮询间隔（默认 5 分钟）自动检测新增视频
- 持久化下载队列 — 重启后自动恢复
- 按收藏夹名称分子文件夹存放
- 可选 NAS 同步（rsync） — 下载后自动同步并删除本地文件；启动时自动补同步之前未同步的文件
- 使用已有的 B站大会员 Cookie 下载最高画质

## B站UP主监控

监控指定 B站 UP主 的新视频上传。需要在 `config.json` 中设置 `bilibili_up_enabled: true`，并确保 B站 Cookie 有效。

| 命令 | 动作 |
|------|------|
| `/up` | 查看监控状态 |
| `/up list` | 查看监控中的 UP主 及模式 |
| `/up add <UID>` | 添加 UP主 监控（仅通知模式） |
| `/up add <UID> --download` | 添加 UP主 监控（自动下载模式） |
| `/up remove <UID>` | 移除 UP主 监控 |
| `/up mode <UID> notify/download` | 切换通知/下载模式 |
| `/up download <UID>` | 下载该 UP主 缺失的视频（跳过已下载） |
| `/up download <UID> --force` | 强制重新下载该 UP主 的所有视频 |
| `/up check` | 立即检查新视频 |
| `/up sync` | 同步本地文件到 NAS |
| `/up queue` | 查看下载队列（当前 + 等待） |
| `/up pause` / `/up resume` | 暂停/恢复监控 |
| `/up history [N]` | 最近下载记录 |

功能特点：
- 每个 UP主 支持两种模式：**仅通知**（只发 Telegram 提醒）或**自动下载**（下载 + NAS 同步）
- 可配置轮询间隔（默认 5 分钟），使用 `last_check_aid` 高效检测新视频
- 支持全量下载命令，一次性下载 UP主 所有视频
- 使用 WBI 签名访问 B站空间 API
- 持久化下载队列 — 重启后自动恢复
- 按 UP主 名称分子文件夹存放
- 复用收藏夹监控的 NAS 同步配置
- 大量新视频时自动合并为一条通知（避免 Telegram 限流）

## 配置

编辑 `config.json` 自定义行为：

```json
{
  "presets": {
    "ping": "pong",
    "status": "服务运行中。"
  },
  "proxy": "",
  "archive_dir": "telegram_archive",
  "notify_port": 8765,
  "shell_timeout": 30,
  "claude_backend": "cli",
  "claude_cli_timeout": 120,
  "telegram_api_base": "",
  "telegram_local_mode": false,
  "telegram_upload_limit_mb": 50
}
```

使用本地 Bot API 服务器（支持 2 GB 上传）时，设置：
```json
"telegram_api_base": "http://127.0.0.1:8081",
"telegram_local_mode": true,
"telegram_upload_limit_mb": 2000
```

### Claude 后端

| `claude_backend` | 说明 |
|---|---|
| `"cli"`（默认） | 使用 `claude -p` CLI。需要安装并登录 Claude Code。无需 API 密钥。无状态（无对话历史）。 |
| `"api"` | 直接使用 Anthropic SDK。需要 `ANTHROPIC_API_KEY` 环境变量。支持滚动对话历史。 |

两种后端都支持内联媒体 — Claude 可以在回复中使用 `[PHOTO: url]` 标记，系统会自动获取并发送给你。

## Systemd 服务部署

所有服务使用 `.service.example` 模板。安装前需复制并配置。

### 1. 创建环境文件

```bash
# 项目路径（所有服务共用）
sudo mkdir -p /etc/telegram-bot
echo "PROJECT_DIR=$(pwd)" | sudo tee /etc/telegram-bot/project.env

# Telegram Bot API 凭据（仅本地 Bot API 服务器需要）
sudo tee /etc/telegram-bot/api.env > /dev/null <<EOF
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
EOF
sudo chmod 600 /etc/telegram-bot/api.env

# Anthropic API 密钥（仅 claude_backend="api" 时需要）
# sudo tee /etc/telegram_bot.env > /dev/null <<EOF
# ANTHROPIC_API_KEY=sk-ant-...
# EOF
# sudo chmod 600 /etc/telegram_bot.env
```

### 2. 从模板生成服务文件

```bash
# 主机器人服务 — 将 YOUR_USER 替换为你的用户名
sed "s/YOUR_USER/$(whoami)/" telegram_bot.service.example > telegram_bot.service

# Docker 服务 — 无需修改，直接复制
cp telegram-bot-api.service.example telegram-bot-api.service
cp douyin-api.service.example douyin-api.service
```

### 3. 安装并启动

```bash
sudo cp telegram_bot.service telegram-bot-api.service douyin-api.service /etc/systemd/system/
sudo systemctl daemon-reload

# 主机器人（必需）
sudo systemctl enable --now telegram_bot

# 本地 Bot API 服务器（可选，启用 2 GB 上传）
sudo systemctl enable --now telegram-bot-api

# 抖音下载器 API（可选，用于 /dl 抖音链接）
sudo systemctl enable --now douyin-api
```

### 迁移到本地 Bot API 服务器

本地 Bot API 服务器需要从云端 API 进行一次性迁移：

```bash
# 1. 停止机器人
sudo systemctl stop telegram_bot

# 2. 从云端 API 注销
curl "https://api.telegram.org/bot$(cat TOKEN.txt)/logOut"

# 3. 启动本地服务器并等待就绪
sudo systemctl start telegram-bot-api

# 4. 验证
curl http://127.0.0.1:8081/bot$(cat TOKEN.txt)/getMe

# 5. 更新 config.json（设置 telegram_api_base、telegram_local_mode、telegram_upload_limit_mb）

# 6. 重启机器人
sudo systemctl start telegram_bot
```

**回退：** 在本地服务器上调用 `logOut`，等待 10 分钟（Telegram 冷却期），清除 config 中的 `telegram_api_base`，重启机器人。

### 日常运维

```bash
sudo systemctl status telegram_bot
sudo systemctl restart telegram_bot
sudo journalctl -u telegram_bot -f
```
