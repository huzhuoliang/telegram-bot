# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python3 bot.py
python3 bot.py --config /path/to/config.json
```

Send notifications from the CLI or other scripts:
```bash
python3 send.py "Backup completed"
python3 send.py --photo /tmp/screenshot.png --caption "说明"
python3 send.py --video /tmp/recording.mp4 --caption "说明"

# HTTP endpoints
curl -X POST http://127.0.0.1:8765/send       -H 'Content-Type: application/json' -d '{"text":"hello"}'
curl -X POST http://127.0.0.1:8765/send       -H 'Content-Type: application/json' -d '{"text":"<b>bold</b>","parse_mode":"HTML"}'
curl -X POST http://127.0.0.1:8765/send_photo  -H 'Content-Type: application/json' -d '{"photo":"/tmp/img.jpg","caption":"optional"}'
curl -X POST http://127.0.0.1:8765/send_video  -H 'Content-Type: application/json' -d '{"video":"/tmp/clip.mp4","caption":"optional"}'
```

`/send` accepts an optional `parse_mode` field (`"HTML"` or `"MarkdownV2"`). Omit for plain text.

## Systemd service

**Service name: `telegram_bot`**

```bash
# Install (one-time)
sudo cp telegram_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram_bot

# Daily operations
sudo systemctl status telegram_bot
sudo systemctl restart telegram_bot
sudo systemctl stop telegram_bot
sudo journalctl -u telegram_bot -f
```

When `claude_backend = "api"`, also create `/etc/telegram_bot.env` (mode 600):
```
ANTHROPIC_API_KEY=sk-ant-...
```

## Architecture

```
bot.py                — entry point: threads, signal handling, startup/shutdown
telegram_client.py    — Telegram Bot API wrapper (long-poll, send/delete/edit message,
                        send photo/video with URL→download fallback, download file)
                        sendVideo includes ffprobe metadata (width/height/duration)
                        for correct mobile aspect ratio + supports_streaming
router.py             — chat_id auth gate + message type dispatch
handlers/             — handler package (split from monolithic handlers.py)
  __init__.py         — re-exports all handler classes
  common.py           — shared regex patterns, utility functions, system prompts
  shell.py            — ShellHandler (! prefix commands)
  claude.py           — ClaudeHandler (cli/api backends, action markers, LaTeX)
  privileged_claude.py — PrivilegedClaudeHandler ($ prefix, full shell/file access)
  preset.py           — PresetHandler (keyword → response lookup)
  media_archive.py    — MediaArchiveHandler + FileArchiveHandler
  video_download.py   — VideoDownloadHandler (/dl command: Douyin API, yt-dlp)
  email_monitor.py    — EmailMonitorHandler (IMAP monitoring, AI classification, digest)
douyin_cookies.py     — Playwright headless Chromium → Douyin cookies (auto-refresh)
douyin-api.service    — systemd unit for TikTokDownloader Docker container
notify_server.py      — localhost:8765 HTTP server for outbound notifications
send.py               — CLI helper to POST to notify server (stdlib only)
debug_bus.py          — debug event bus + TCP JSON Lines server (127.0.0.1:8766)
debug.py              — CLI debug monitor (streaming / Rich full-screen TUI / raw JSON)
DEBUG.md              — debug tool documentation (keyboard, mouse, search, architecture)
email_credentials.json — IMAP/SMTP account credentials (never commit)
email_state.json      — processed email UIDs state (auto-generated, never commit)
config.json           — presets, timeouts, model/backend settings
help.txt              — /help command text (static sections; hot-reloaded on each /help)
TOKEN.txt             — Telegram bot token (never commit)
CHAT_ID.txt           — authorized chat ID (never commit)
telegram_bot.service  — systemd unit
```

**Threading:** main thread blocks on `_shutdown_event`; two daemon threads run the polling loop and the notify HTTP server. A third daemon thread runs the debug TCP server. When email monitor is enabled, additional daemon threads run per-account IMAP monitoring and digest scheduling. The notify server uses `server.timeout=1` + `handle_request()` loop (not `serve_forever()`) so shutdown is clean.

## Debug monitor (debug_bus.py + debug.py)

Bot emits structured events via `debug_bus.emit()` at key points (telegram in/out, API request/response, shell execution, tool calls, route decisions). A TCP server on `127.0.0.1:8766` streams events as JSON Lines to connected clients. Zero overhead when no client is connected.

```bash
python3 debug.py                  # streaming mode (default, scrollable)
python3 debug.py --live           # full-screen TUI (Rich, Ctrl+C to quit)
python3 debug.py --filter api     # only API request/response
python3 debug.py --filter tg      # only Telegram in/out
python3 debug.py --filter shell   # only shell commands
python3 debug.py --filter tool    # only tool calls
python3 debug.py --filter route   # only route decisions
python3 debug.py --full           # show complete data (not truncated)
python3 debug.py --raw            # raw JSON Lines (pipe-friendly)
python3 debug.py --raw | jq .     # pretty-print with jq
```

Event types: `telegram_in`, `telegram_out`, `api_request`, `api_response`, `shell_exec`, `tool_call`, `route`.

Full documentation (keyboard shortcuts, mouse support, search, architecture): see [DEBUG.md](DEBUG.md).

**getUpdates transport:** uses POST + JSON body (not GET + query params). Telegram does not reliably parse `allowed_updates` when sent as repeated GET params.

## Message routing (router.py)

Messages from any chat other than `CHAT_ID.txt` are silently dropped.

| Input | Handler |
|---|---|
| Photo + caption | ClaudeHandler — image recognition (api backend only) |
| Photo / video / document (no caption) | MediaArchiveHandler — saves to `archive_dir` |
| Emoji reaction on a message | Replies with the same emoji(s) |
| `!clear` or `/clear` | Clears Claude conversation history |
| `$clear` | Clears privileged Claude conversation history |
| `/ctx` | ClaudeHandler.context_stats() — context window breakdown (api only) |
| `$ctx` | PrivilegedClaudeHandler.context_stats() — privileged context breakdown (api only) |
| `$whitelist <list\|add\|remove>` | PrivilegedClaudeHandler.handle_whitelist_cmd() — manage shell whitelist |
| `/dl <URL or share text>` | VideoDownloadHandler — Douyin (TikTokDownloader API), Bilibili/other (yt-dlp); auto-extracts URL from share text |
| `/email [subcommand]` | EmailMonitorHandler — status, digest, check, pause, resume, send |
| `!<cmd>` | ShellHandler — runs in `~`, sudo blocked |
| `$<text>` | PrivilegedClaudeHandler — runs in background thread; shell commands require user confirmation via reaction (👍 once / 📌 whitelist / 👎 reject); whitelisted commands skip confirmation |
| `?<text>` | ClaudeHandler |
| Preset keyword | PresetHandler — dict lookup from config.json |
| Anything else | ClaudeHandler (default fallback) |

Special commands are checked before prefix dispatch to avoid `!clear` being treated as a shell command.

## Claude backends (handlers/claude.py)

Controlled by `claude_backend` in `config.json`:

- `"cli"` (default) — runs `claude -p <text>` subprocess. Uses existing Claude Code credentials. No API key needed. Stateless (no history).
- `"api"` — uses `anthropic` SDK directly. Requires `ANTHROPIC_API_KEY`. Maintains rolling conversation history (`claude_history_turns` turns). `anthropic` import is lazy (only loaded when this backend is active). `PrivilegedClaudeHandler` additionally compresses history after each turn: tool-call intermediates (assistant tool_use + user tool_result pairs) are stripped, keeping only the original user text and final assistant text. This prevents token exhaustion in long sessions with many tool calls.

Both backends:
- Send `⏳ 处理中...` before calling the backend; on completion edit that message in-place with the reply (`editMessageText`) rather than delete+send. Falls back to delete+send if the edit fails (e.g. HTML parse error).
- Parse `[PHOTO: url]` / `[VIDEO: url]` markers from Claude's response and send media automatically
- Always respond in Chinese (except code, shell output, technical strings)

### Claude action markers

Claude can embed these in its response to trigger media delivery:
```
[PHOTO: <url_or_path>]
[PHOTO: <url_or_path> | <caption>]
[VIDEO: <url_or_path>]
[VIDEO: <url_or_path> | <caption>]
```

For URLs that Telegram can't fetch directly (e.g. Wikipedia), `telegram_client` automatically downloads via local proxy and uploads as file.

## Media archive (handlers/media_archive.py)

Incoming photos/videos/documents are saved to `archive_dir`:
```
~/telegram_archive/
├── photos/     YYYY-MM-DD_HH-MM-SS.jpg
├── videos/     YYYY-MM-DD_HH-MM-SS.<ext>
└── documents/  <original filename>
```

## config.json reference

| Key | Default | Description |
|-----|---------|-------------|
| `presets` | `{}` | Keyword → response mapping (case-insensitive) |
| `proxy` | `""` | HTTP proxy for Telegram API (e.g. `http://127.0.0.1:2080`) |
| `archive_dir` | `"~/telegram_archive"` | Directory for saved incoming media |
| `notify_port` | `8765` | Port for local HTTP notification server |
| `poll_interval` | `2` | Seconds to wait between retries on polling error |
| `shell_timeout` | `30` | Max seconds for shell command execution |
| `shell_output_max_chars` | `3000` | Truncation limit for shell output |
| `claude_backend` | `"cli"` | `"cli"` or `"api"` |
| `claude_model` | `"claude-sonnet-4-6"` | Model (used by `api` backend only) |
| `claude_max_tokens` | `1024` | Max tokens (used by `api` backend only) |
| `claude_history_turns` | `6` | Rolling history window (used by `api` backend only) |
| `claude_cli_timeout` | `120` | Subprocess timeout for `cli` backend |
| `privileged_claude_model` | `"claude-sonnet-4-6"` | Model for `$` privileged handler |
| `privileged_claude_max_tokens` | `4096` | Max tokens for privileged handler |
| `privileged_claude_history_turns` | `6` | Rolling history window for privileged handler |
| `privileged_claude_shell_timeout` | `60` | Shell command timeout for privileged handler |
| `privileged_shell_whitelist` | `[]` | Commands that skip confirmation; suffix `*` = prefix match, exact otherwise |
| `debug_port` | `8766` | Port for debug event TCP server |
| `video_download_dir` | `"~/video_downloads"` | Directory for downloaded videos |
| `video_download_cookies_bilibili` | `""` | Path to Bilibili cookies.txt |
| `video_download_cookies_douyin` | `"~/douyin_cookies.txt"` | Path to Douyin cookies (auto-refreshed by Playwright) |
| `video_download_timeout` | `600` | Download timeout in seconds |
| `email_enabled` | `false` | Enable email monitor |
| `email_credentials_path` | `"email_credentials.json"` | Path to IMAP/SMTP credentials file |
| `email_state_path` | `"email_state.json"` | Path to processed email state file |
| `email_digest_interval_hours` | `6` | Hours between automatic digest reports |
| `email_check_interval` | `30` | Seconds between IMAP polling checks |
| `email_urgent_keywords` | `["urgent", ...]` | Keywords for urgent classification hints |
| `email_claude_model` | `"claude-sonnet-4-6"` | Model for email classification/digest |
| `email_claude_max_tokens` | `200` | Max tokens for per-email classification |
| `log_file` | (none) | Optional log file path; stdout only if omitted |
| `log_level` | `"INFO"` | Logging level |

## Email monitor (handlers/email_monitor.py)

IMAP-based email monitoring with AI-powered classification and summarization.

**Features:**
- IMAP polling (configurable interval, default 30s); IDLE supported for non-QQ providers (per-account `"idle": true/false`)
- AI classification: each email classified as urgent/normal/spam via Claude API
- Urgent emails trigger immediate Telegram alerts
- Periodic AI-generated digest reports (natural language summary, not a raw list)
- Send emails via SMTP (`/email send`)

**Commands:**

| Command | Action |
|---|---|
| `/email` | Show monitor status and statistics |
| `/email digest` | Send digest report immediately |
| `/email check` | Force-check all accounts now |
| `/email pause` | Pause monitoring |
| `/email resume` | Resume monitoring |
| `/email send <to> <subject> <body>` | Send email (single or multi-line) |

**Credentials file (`email_credentials.json`):**
```json
{
  "accounts": [
    {
      "id": "qq",
      "host": "imap.qq.com",
      "port": 993,
      "username": "user@qq.com",
      "password": "imap-auth-code",
      "folders": ["INBOX"],
      "idle": false
    }
  ]
}
```

SMTP host is auto-derived from IMAP host (`imap.` → `smtp.`, port 465 SSL). Override with `"smtp_host"` and `"smtp_port"` if needed.

**State file (`email_state.json`):** auto-generated, tracks processed UIDs per account (rolling window of 500). Atomic writes via tmp+rename. New accounts process only the latest 20 emails on first run.

**Known issue:** QQ Mail advertises IMAP IDLE capability but does not push notifications. Set `"idle": false` for QQ accounts.

## Douyin video download (TikTokDownloader)

Douyin downloads use [TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader) running as a Docker container API service.

**Service name: `douyin-api`**

```bash
sudo systemctl status douyin-api
sudo systemctl restart douyin-api
sudo journalctl -u douyin-api -f
```

- Docker image: `ghcr.io/joeanamier/tiktok-downloader:latest`
- Network: `--network host` (shares host TUN proxy)
- API endpoint: `http://127.0.0.1:5555/douyin/detail`
- Config: `/home/huzhuoliang/douyin_downloader/settings.json`
- Cookies: auto-refreshed via `douyin_cookies.py` (Playwright headless, 1 hour TTL)
