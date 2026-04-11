[English](README.md) | **中文**

# telegram-monitor

个人 Telegram 机器人服务。部署在你的服务器上，通过长轮询接收 Telegram 消息，并允许本地任意脚本向你的 Telegram 账号发送通知。

## 功能

- **通知推送** — 其他脚本/应用通过 POST 本地 HTTP 接口发送消息、图片或视频
- **Shell 执行** — 发送 `!<命令>` 在服务器上执行并返回输出
- **Claude AI** — 发送 `?<问题>`（或任意文本）获取 AI 回复；Claude 还能搜索并内联发送图片/视频
- **预设回复** — 配置固定的关键词 → 回复对
- **媒体归档** — 转发图片/视频/文档给机器人，自动保存到服务器
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
| `ping` | 返回 `pong`（预设） |
| `help` | 返回命令参考（预设） |
| `!clear` 或 `/clear` | 清除 Claude 对话历史 |
| 其他任意文本 | 转发给 Claude |

**转发媒体：** 发送或转发任何图片、视频或文档给机器人 — 它会保存到服务器的 `telegram_archive/` 目录，并回复确认保存路径。

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
