# telegram-monitor

Personal Telegram bot service. Runs on your server, polls Telegram for messages, and lets any local script send notifications to your Telegram account.

## Features

- **Notifications** — other scripts/apps POST to a local HTTP endpoint to send messages, photos, or videos
- **Shell execution** — send `!<command>` to run it on the server and get output back
- **Claude AI** — send `?<question>` (or any text) to get an AI response; Claude can also search and send photos/videos inline
- **Preset replies** — configure fixed keyword → response pairs
- **Media archive** — forward photos/videos/documents to the bot and they are saved to the server automatically
- **No public IP needed** — uses long-polling, no webhook required

## Prerequisites

- Python 3.10+
- `requests` and `anthropic` packages (`pip install requests anthropic`)
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))
- Claude Code CLI installed and authenticated (for the default `cli` backend)

## Setup

**1. Save credentials**

```bash
echo "YOUR_BOT_TOKEN" > TOKEN.txt
echo "YOUR_CHAT_ID" > CHAT_ID.txt
```

To find your chat ID: start a conversation with your bot, then visit
`https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id":...}`.

**2. Run**

```bash
python3 bot.py
```

The bot sends "服务已启动。" to your Telegram when it's ready.

## Usage

Send messages to your bot in Telegram:

| Message | Action |
|---------|--------|
| `!ls -la /tmp` | Runs shell command, returns stdout + stderr + exit code |
| `?explain DNS` | Asks Claude, returns response in Chinese |
| `搜索一张杨幂的照片` | Claude finds a photo and sends it to you |
| `ping` | Returns `pong` (preset) |
| `help` | Returns command reference (preset) |
| `!clear` or `/clear` | Clears Claude conversation history |
| any other text | Forwarded to Claude |

**Forwarding media:** Send or forward any photo, video, or document to the bot — it will be saved to `~/telegram_archive/` on the server and the bot will confirm with the saved path.

## Sending notifications from other scripts

While the bot is running, any local process can send messages, photos, or videos:

```bash
# Text
python3 send.py "Backup completed successfully"

# Photo (local file or URL)
python3 send.py --photo /tmp/screenshot.png --caption "今日报表"
python3 send.py --photo "https://example.com/chart.png"

# Video (local file or URL, max 50 MB)
python3 send.py --video /tmp/recording.mp4 --caption "录像"
python3 send.py --video "https://example.com/clip.mp4"
```

HTTP API (accepts local file path or URL for photo/video):

```bash
curl -X POST http://127.0.0.1:8765/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "Deploy finished"}'

curl -X POST http://127.0.0.1:8765/send_photo \
  -H 'Content-Type: application/json' \
  -d '{"photo": "/tmp/img.jpg", "caption": "optional"}'

curl -X POST http://127.0.0.1:8765/send_video \
  -H 'Content-Type: application/json' \
  -d '{"video": "/tmp/clip.mp4", "caption": "optional"}'
```

## Configuration

Edit `config.json` to customize behavior:

```json
{
  "presets": {
    "ping": "pong",
    "status": "服务运行中。"
  },
  "proxy": "http://127.0.0.1:2080",
  "archive_dir": "~/telegram_archive",
  "notify_port": 8765,
  "shell_timeout": 30,
  "claude_backend": "cli",
  "claude_cli_timeout": 120
}
```

### Claude backends

| `claude_backend` | Description |
|---|---|
| `"cli"` (default) | Uses `claude -p` CLI. Requires Claude Code to be installed and logged in. No API key needed. Stateless (no conversation history). |
| `"api"` | Uses Anthropic SDK directly. Requires `ANTHROPIC_API_KEY` env var. Supports rolling conversation history. |

Both backends support inline media delivery — Claude can respond with `[PHOTO: url]` markers that are automatically fetched and sent to you.

## Running as a systemd service

```bash
sudo cp telegram_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram_bot
sudo journalctl -u telegram_bot -f
```

If using `claude_backend = "api"`, store the API key in `/etc/telegram_bot.env` (mode 600):
```
ANTHROPIC_API_KEY=sk-ant-...
```
