"""Thin wrapper around the Telegram Bot HTTP API."""

import logging
import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4096

logger = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, token: str, chat_id: str, proxy: str = ""):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()
        self._session = requests.Session()
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
            logger.info("Using proxy: %s", proxy)

    def _url(self, method: str) -> str:
        return TELEGRAM_API.format(token=self.token, method=method)

    def send_message(self, text: str, parse_mode: str = "") -> bool:
        """Send text to the configured chat. Splits messages > 4096 chars.
        Never raises; returns True on full success."""
        if not text:
            return True

        chunks = [text[i:i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)]
        success = True
        for chunk in chunks:
            payload = {"chat_id": self.chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                resp = self._session.post(self._url("sendMessage"), json=payload, timeout=10)
                if not resp.ok:
                    logger.warning("sendMessage failed: %s %s", resp.status_code, resp.text[:200])
                    success = False
            except Exception as e:
                logger.warning("sendMessage exception: %s", e)
                success = False
        return success

    def get_updates(self, offset: int, timeout: int = 30) -> list:
        """Long-poll for new updates. Returns list of update dicts, [] on error."""
        params = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        try:
            resp = self._session.get(
                self._url("getUpdates"),
                params=params,
                timeout=(10, timeout + 5),  # connect=10s, read=timeout+5s
            )
            if resp.ok:
                data = resp.json()
                if data.get("ok"):
                    return data.get("result", [])
                logger.warning("getUpdates not ok: %s", data)
            else:
                logger.warning("getUpdates HTTP %s", resp.status_code)
        except requests.exceptions.ReadTimeout:
            pass  # normal long-poll timeout with no messages
        except Exception as e:
            logger.warning("getUpdates exception: %s", e)
        return []
