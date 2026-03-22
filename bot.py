#!/usr/bin/env python3
"""Telegram bot service.

Starts two background threads:
  - polling_thread: long-polls Telegram for incoming messages and routes them
  - notify_thread:  HTTP server on localhost for other apps to send notifications

Usage:
    python3 bot.py [--config config.json]

Requires ANTHROPIC_API_KEY environment variable.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
from pathlib import Path

from handlers import ClaudeHandler, PresetHandler, ShellHandler
from notify_server import run_notify_server
from router import Router
from telegram_client import TelegramClient

BASE_DIR = Path(__file__).parent
_shutdown_event = threading.Event()


def handle_signal(signum, frame):
    logging.info("Signal %d received, shutting down...", signum)
    _shutdown_event.set()


def setup_logging(log_file: str | None, log_level: str):
    level = getattr(logging, log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3
            )
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)


def load_credentials() -> tuple[str, str]:
    token_file = BASE_DIR / "TOKEN.txt"
    chat_id_file = BASE_DIR / "CHAT_ID.txt"
    if not token_file.exists():
        sys.exit(f"Missing {token_file}")
    if not chat_id_file.exists():
        sys.exit(f"Missing {chat_id_file}")
    token = token_file.read_text().strip()
    chat_id = chat_id_file.read_text().strip()
    return token, chat_id


def polling_loop(client: TelegramClient, router: Router, poll_interval: int):
    logger = logging.getLogger("polling")
    offset = 0
    logger.info("Polling started")
    while not _shutdown_event.is_set():
        try:
            updates = client.get_updates(offset, timeout=30)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    reply = router.route(update)
                    if reply:
                        client.send_message(reply)
                except Exception as e:
                    logger.exception("Error handling update %s: %s", update.get("update_id"), e)
        except Exception as e:
            logger.warning("Polling error: %s", e)
            _shutdown_event.wait(timeout=poll_interval)
    logger.info("Polling stopped")


def main():
    parser = argparse.ArgumentParser(description="Telegram bot service")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_file"), config.get("log_level", "INFO"))
    logger = logging.getLogger("bot")

    claude_backend = config.get("claude_backend", "cli")
    if claude_backend == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set — Claude API backend will fail on use")

    token, chat_id = load_credentials()
    client = TelegramClient(token, chat_id)

    shell_handler = ShellHandler(
        timeout=config.get("shell_timeout", 30),
        max_chars=config.get("shell_output_max_chars", 3000),
    )
    claude_handler = ClaudeHandler(
        backend=claude_backend,
        model=config.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=config.get("claude_max_tokens", 1024),
        history_turns=config.get("claude_history_turns", 6),
        cli_timeout=config.get("claude_cli_timeout", 60),
    )
    preset_handler = PresetHandler(config.get("presets", {}))
    router = Router(chat_id, shell_handler, claude_handler, preset_handler)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    poll_thread = threading.Thread(
        target=polling_loop,
        args=(client, router, config.get("poll_interval", 2)),
        name="polling",
        daemon=True,
    )
    notify_thread = threading.Thread(
        target=run_notify_server,
        args=(client, config.get("notify_port", 8765), _shutdown_event),
        name="notify_server",
        daemon=True,
    )

    poll_thread.start()
    notify_thread.start()

    logger.info("Bot started (chat_id=%s, notify_port=%d)", chat_id, config.get("notify_port", 8765))
    client.send_message("Bot started.")

    _shutdown_event.wait()

    logger.info("Waiting for threads to finish...")
    poll_thread.join(timeout=5)
    notify_thread.join(timeout=3)
    logger.info("Bot stopped")


if __name__ == "__main__":
    main()
