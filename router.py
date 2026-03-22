"""Routes incoming Telegram messages to the appropriate handler."""

import logging

logger = logging.getLogger(__name__)


class Router:
    def __init__(self, chat_id: str, shell_handler, claude_handler, preset_handler):
        self.chat_id = str(chat_id).strip()
        self.shell = shell_handler
        self.claude = claude_handler
        self.preset = preset_handler

    def route(self, update: dict) -> str | None:
        """Return reply text, or None if the message should be silently ignored."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return None

        # Authorization: only respond to the configured chat
        sender_id = str(message.get("chat", {}).get("id", ""))
        if sender_id != self.chat_id:
            logger.debug("Ignored message from unauthorized chat_id=%s", sender_id)
            return None

        text = (message.get("text") or "").strip()
        if not text:
            return None

        logger.info("Incoming [%s]: %s", sender_id, text[:80])

        # !cmd → shell
        if text.startswith("!"):
            return self.shell.handle(text[1:].strip())

        # ?question → Claude
        if text.startswith("?"):
            return self.claude.handle(text[1:].strip())

        # Special: clear Claude history
        if text.lower() in ("!clear", "/clear"):
            return self.claude.clear_history()

        # Preset match
        preset_reply = self.preset.handle(text)
        if preset_reply is not None:
            return preset_reply

        # Default: forward to Claude
        return self.claude.handle(text)
