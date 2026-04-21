**English** | [中文](README_zh.md)

# telegram-monitor

Personal Telegram bot service. Runs on your server, polls Telegram for messages, and lets any local script send notifications to your Telegram account.

## Features

- **Notifications** — other scripts/apps POST to a local HTTP endpoint to send messages, photos, or videos
- **Shell execution** — send `!<command>` to run it on the server and get output back
- **Claude AI** — send `?<question>` (or any text) to get an AI response; Claude can also search and send photos/videos inline
- **Privileged Claude** — send `$<text>` for an AI assistant with unrestricted shell and file access, with interactive confirmation for commands
- **Video download** — send `/dl <URL>` to download videos from Douyin (watermark-free), Bilibili (4K/HDR), YouTube, and other sites supported by yt-dlp
- **Email monitor** — IMAP-based email monitoring with AI-powered classification (urgent/normal/spam) and periodic digest reports
- **Bilibili favorites monitor** — auto-download videos from monitored Bilibili favorites folders, with persistent queue and optional NAS sync via rsync
- **Bilibili UP monitor** — monitor Bilibili uploaders for new video uploads with notification-only or auto-download mode, WBI-signed API, persistent queue and NAS sync
- **Bilibili archive** — shared persistent archive (BV → NAS path) skips already-downloaded videos with SSH verification; `/<handler> redo <BV>` fast-track command for re-downloading corrupted or deleted files
- **Image recognition** — send a photo with a caption to get Claude's analysis (API backend only)
- **Preset replies** — configure fixed keyword → response pairs
- **Media archive** — forward photos/videos/documents to the bot and they are saved to the server automatically; browse with `/files`
- **LaTeX rendering** — Claude can render LaTeX formulas as images in responses
- **Debug monitor** — real-time TUI to inspect Telegram I/O, Claude API calls, shell commands, and routing (see [DEBUG.md](DEBUG.md))
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
| `搜索一张XXX的照片` | Claude finds a photo and sends it to you |
| `$check disk usage` | Privileged Claude — can run any command (with confirmation) |
| `$$deploy the app` | Privileged Claude — auto-approve all commands (no confirmation) |
| `/dl <URL>` | Download video from Douyin, Bilibili, YouTube, etc. |
| `/email` | Email monitor status; `/email digest`, `/email check`, etc. |
| `/fav` | Bilibili favorites monitor; `/fav folders`, `/fav add`, `/fav download`, `/fav sync`, etc. |
| `/up` | Bilibili UP monitor; `/up add`, `/up download`, `/up mode`, `/up sync`, etc. |
| `/files` | Browse archived files (paginated inline keyboard) |
| `/help` | Display command reference |
| `/status` | Show current Claude backend status |
| `/ctx` / `$ctx` | Context window usage for regular / privileged Claude |
| `/setkey <KEY>` | Set Anthropic API key, switch to API backend |
| `/setcli` | Switch back to CLI backend |
| `!clear` or `/clear` | Clear Claude conversation history |
| `$clear` | Clear privileged Claude conversation history |
| Photo + caption | Claude image recognition (API backend only) |
| Photo / video / document | Auto-saved to `telegram_archive/` on the server |
| Emoji reaction | Bot replies with the same emoji |
| any other text | Forwarded to Claude |

## Sending notifications from other scripts

While the bot is running, any local process can send messages, photos, or videos:

```bash
# Text
python3 send.py "Backup completed successfully"

# Photo (local file or URL)
python3 send.py --photo /tmp/screenshot.png --caption "今日报表"
python3 send.py --photo "https://example.com/chart.png"

# Video (local file or URL; up to 2 GB with local Bot API server)
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

## Video download

Send `/dl <URL>` to download videos. Supports:

| Platform | Backend | Notes |
|----------|---------|-------|
| **Douyin** | TikTokDownloader API (Docker) | Watermark-free, highest quality. Paste share text directly — URL is auto-extracted. Cookies auto-refreshed via Playwright. |
| **Bilibili** | yt-dlp | 4K/HDR preferred. Auto-validates cookie; triggers QR-code login if expired. Anonymous mode falls back to 1080p. |
| **YouTube & others** | yt-dlp | Any site supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp). |

After download:
- Files within the upload limit (50 MB cloud / 2 GB local Bot API) are uploaded to Telegram directly
- Larger files return the local path on the server
- AV1-encoded videos are automatically transcoded to H.265 for iPhone compatibility (with live progress updates)

Requires `yt-dlp` and `ffmpeg` installed. For Douyin, also requires the `douyin-api` service running (see [Systemd services](#systemd-services-deployment)).

## Privileged Claude

Send `$<text>` to use an AI assistant with full system access. Unlike regular Claude, it can:
- Execute **any** shell command (including `sudo`)
- Read and write **any** file on the server

**Safety mechanism:** Before executing a shell command, the bot sends a confirmation message with three buttons:
- ✅ **Allow once** — execute this command only
- 📌 **Add to whitelist** — execute and allow this command pattern in the future
- ❌ **Reject** — deny execution (auto-rejects after 60s timeout)

Send `$$<text>` to auto-approve all commands in that session (each command still shows a silent notification).

Whitelist management:
```
$whitelist list              — view current whitelist
$whitelist add <cmd or prefix*>  — add (e.g. ls* for prefix match)
$whitelist remove <number>   — remove by index
```

## Email monitor

IMAP-based email monitoring with AI-powered classification. Requires `email_enabled: true` in `config.json`.

| Command | Action |
|---------|--------|
| `/email` | Show monitor status and statistics |
| `/email digest` | Send AI-generated digest report immediately |
| `/email check` | Force-check all accounts now |
| `/email pause` | Pause monitoring |
| `/email resume` | Resume monitoring |
| `/email send <to> <subject> <body>` | Send an email via SMTP |

Features:
- Each incoming email is classified by AI as **urgent**, **normal**, or **spam**
- Urgent emails trigger immediate Telegram alerts
- Periodic AI-generated digest reports (configurable interval, default 6 hours)
- Supports IMAP IDLE for real-time push (except QQ Mail)

See `email_credentials.json` for account configuration format.

## Bilibili favorites monitor

Auto-download videos from monitored Bilibili favorites folders. Requires `bilibili_fav_enabled: true` in `config.json` and a valid Bilibili cookie (shared with `/dl` video downloads).

| Command | Action |
|---------|--------|
| `/fav` | Show monitor status |
| `/fav folders` | List all your Bilibili favorites folders (with IDs) |
| `/fav list` | List currently monitored folders |
| `/fav add <ID>` | Add folder to monitoring (existing videos marked as known) |
| `/fav remove <ID>` | Remove folder from monitoring |
| `/fav download <ID>` | Queue missing videos from folder (skips archived + downloaded) |
| `/fav download <ID> --force` | Force re-download all videos from folder |
| `/fav redo <BV>` | Force re-download a single video (fast-track queue) |
| `/fav clear_queue` | Empty the main download queue (current download not interrupted) |
| `/fav check` | Trigger immediate check for new videos |
| `/fav sync` | Sync all local files to NAS |
| `/fav queue` | View download queue (current + pending) |
| `/fav pause` / `/fav resume` | Pause/resume monitoring |
| `/fav history [N]` | Recent download history |

Features:
- Polls favorites folders at configurable intervals (default 5 minutes)
- Persistent download queue — survives bot restarts
- Per-folder subdirectories (named after folder title)
- Optional NAS sync via rsync — automatically syncs after download and deletes local files; unsynced files from previous sessions are synced on startup
- Downloads use existing Bilibili VIP cookie for best quality

## Bilibili UP monitor

Monitor Bilibili uploaders (UP主) for new video uploads. Requires `bilibili_up_enabled: true` in `config.json` and a valid Bilibili cookie.

| Command | Action |
|---------|--------|
| `/up` | Show monitor status |
| `/up list` | List monitored uploaders with mode |
| `/up add <UID>` | Add uploader (notify-only mode) |
| `/up add <UID> --download` | Add uploader (auto-download mode) |
| `/up remove <UID>` | Remove uploader from monitoring |
| `/up mode <UID> notify/download` | Switch mode for an uploader |
| `/up download <UID>` | Queue missing videos from uploader (skips already downloaded) |
| `/up download <UID> --force` | Force re-download all videos from uploader |
| `/up redo <BV>` | Force re-download a single video (fast-track queue) |
| `/up rebuild_archive` | Scan NAS and rebuild the shared archive from filenames |
| `/up reconcile` | Remove downloaded_bvids entries not in the archive (recover missed videos) |
| `/up rename_archive` | Rename archived files to `YYYY-MM-DD_<title>_[BV].ext` (background task) |
| `/up clear_queue` | Empty the main download queue (current download not interrupted) |
| `/up check` | Trigger immediate check for new videos |
| `/up sync` | Sync all local files to NAS |
| `/up queue` | View download queue (current + pending) |
| `/up pause` / `/up resume` | Pause/resume monitoring |
| `/up history [N]` | Recent download history |

Features:
- Two modes per uploader: **notify-only** (just sends a Telegram alert) or **auto-download** (downloads + NAS sync)
- Polls at configurable intervals (default 5 minutes) using `last_check_aid` for efficient new-video detection
- Full-download command to queue all existing videos from an uploader
- WBI-signed API requests for Bilibili space endpoint
- Persistent download queue — survives bot restarts
- Per-uploader subdirectories (named after uploader name)
- Reuses NAS sync configuration from favorites monitor
- Batch notifications when many new videos are detected (avoids Telegram rate limits)

## Configuration

Edit `config.json` to customize behavior:

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

To use the local Bot API server (2 GB uploads), set:
```json
"telegram_api_base": "http://127.0.0.1:8081",
"telegram_local_mode": true,
"telegram_upload_limit_mb": 2000
```

### Claude backends

| `claude_backend` | Description |
|---|---|
| `"cli"` (default) | Uses `claude -p` CLI. Requires Claude Code to be installed and logged in. No API key needed. Stateless (no conversation history). |
| `"api"` | Uses Anthropic SDK directly. Requires `ANTHROPIC_API_KEY` env var. Supports rolling conversation history. |

Both backends support inline media delivery — Claude can respond with `[PHOTO: url]` markers that are automatically fetched and sent to you.

## Systemd services deployment

All services use `.service.example` templates. Copy and configure before installing.

### 1. Create environment files

```bash
# Project path (required by all services)
sudo mkdir -p /etc/telegram-bot
echo "PROJECT_DIR=$(pwd)" | sudo tee /etc/telegram-bot/project.env

# Telegram Bot API credentials (required only for local Bot API server)
sudo tee /etc/telegram-bot/api.env > /dev/null <<EOF
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
EOF
sudo chmod 600 /etc/telegram-bot/api.env

# Anthropic API key (required only for claude_backend="api")
# sudo tee /etc/telegram_bot.env > /dev/null <<EOF
# ANTHROPIC_API_KEY=sk-ant-...
# EOF
# sudo chmod 600 /etc/telegram_bot.env
```

### 2. Generate service files from templates

```bash
# Main bot service — replace YOUR_USER with your username
sed "s/YOUR_USER/$(whoami)/" telegram_bot.service.example > telegram_bot.service

# Docker services — no changes needed, just copy
cp telegram-bot-api.service.example telegram-bot-api.service
cp douyin-api.service.example douyin-api.service
```

### 3. Install and start

```bash
sudo cp telegram_bot.service telegram-bot-api.service douyin-api.service /etc/systemd/system/
sudo systemctl daemon-reload

# Main bot (required)
sudo systemctl enable --now telegram_bot

# Local Bot API server (optional, enables 2 GB uploads)
sudo systemctl enable --now telegram-bot-api

# Douyin downloader API (optional, for /dl douyin links)
sudo systemctl enable --now douyin-api
```

### Migrating to local Bot API server

The local Bot API server requires a one-time migration from the cloud API:

```bash
# 1. Stop bot
sudo systemctl stop telegram_bot

# 2. Log out from cloud API
curl "https://api.telegram.org/bot$(cat TOKEN.txt)/logOut"

# 3. Start local server and wait for it to be ready
sudo systemctl start telegram-bot-api

# 4. Verify
curl http://127.0.0.1:8081/bot$(cat TOKEN.txt)/getMe

# 5. Update config.json (set telegram_api_base, telegram_local_mode, telegram_upload_limit_mb)

# 6. Restart bot
sudo systemctl start telegram_bot
```

**Rollback:** Call `logOut` on the local server, wait 10 minutes (Telegram cooldown), clear `telegram_api_base` in config, restart bot.

### Daily operations

```bash
sudo systemctl status telegram_bot
sudo systemctl restart telegram_bot
sudo journalctl -u telegram_bot -f
```
