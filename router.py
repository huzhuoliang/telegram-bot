"""Routes incoming Telegram messages to the appropriate handler."""

import logging

logger = logging.getLogger(__name__)


class Router:
    def __init__(self, chat_id: str, shell_handler, claude_handler, preset_handler,
                 media_archive_handler=None, file_archive_handler=None,
                 privileged_claude_handler=None, config_path: str = None):
        self.chat_id = str(chat_id).strip()
        self.shell = shell_handler
        self.claude = claude_handler
        self.preset = preset_handler
        self.media_archive = media_archive_handler
        self.file_archive = file_archive_handler
        self.privileged_claude = privileged_claude_handler
        self.config_path = config_path

    def route(self, update: dict) -> str | None:
        """Return reply text, or None if the message should be silently ignored."""

        # Callback query (inline keyboard button click)
        callback_query = update.get("callback_query")
        if callback_query:
            chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
            if chat_id != self.chat_id:
                return None
            data = callback_query.get("data", "")
            cq_id = callback_query.get("id", "")
            msg_id = callback_query.get("message", {}).get("message_id")
            if data.startswith("priv:") and self.privileged_claude:
                self.privileged_claude.resolve_pending_callback(cq_id, msg_id, data[5:])
                return None
            if self.file_archive:
                self.file_archive.handle_callback(callback_query)
            return None

        # Message reaction
        reaction = update.get("message_reaction")
        if reaction:
            chat_id = str(reaction.get("chat", {}).get("id", ""))
            if chat_id != self.chat_id:
                return None
            msg_id = reaction.get("message_id")
            new_reactions = reaction.get("new_reaction", [])
            emojis = [r["emoji"] for r in new_reactions if r.get("type") == "emoji"]
            if emojis:
                logger.info("Reaction on msg#%s: %s", msg_id, " ".join(emojis))
                return " ".join(emojis)
            return None

        message = update.get("message") or update.get("edited_message")
        if not message:
            return None

        # Authorization: only respond to the configured chat
        sender_id = str(message.get("chat", {}).get("id", ""))
        if sender_id != self.chat_id:
            logger.debug("Ignored message from unauthorized chat_id=%s", sender_id)
            return None

        # Media messages (photo, video, document)
        if self.media_archive and any(k in message for k in ("photo", "video", "document")):
            logger.info("Incoming media from [%s]", sender_id)
            caption = message.get("caption", "").strip()
            if "photo" in message and caption and self.claude:
                file_id = message["photo"][-1]["file_id"]
                return self.claude.handle_with_image(caption, file_id)
            return self.media_archive.handle(message)

        text = (message.get("text") or "").strip()
        if not text:
            return None

        logger.info("Incoming [%s]: %s", sender_id, text[:80])

        # Special commands (must be checked before prefix dispatch)
        if text.lower() in ("!clear", "/clear"):
            return self.claude.clear_history()

        if text.lower() == "$clear" and self.privileged_claude:
            return self.privileged_claude.clear_history()

        if text.lower().startswith("/setkey "):
            api_key = text[8:].strip()
            if not api_key:
                return "用法：/setkey &lt;ANTHROPIC_API_KEY&gt;"
            return self.claude.configure_api_backend(api_key, self.config_path)

        if text.lower() == "/setcli":
            return self.claude.configure_cli_backend(self.config_path)

        if text.lower() == "/status":
            return self.claude.status()

        if text.lower() == "/ctx":
            return self.claude.context_stats()

        if text.lower() == "/help":
            return self.claude.help()

        if text.lower() == "/files":
            if self.file_archive:
                self.file_archive.handle_command()
            return None

        # !cmd → shell
        if text.startswith("!"):
            return self.shell.handle(text[1:].strip())

        # $cmd → privileged Claude
        if text.startswith("$") and self.privileged_claude:
            if text.lower() == "$ctx":
                return self.privileged_claude.context_stats()
            inner = text[1:].strip()
            if inner.lower().startswith("whitelist"):
                return self.privileged_claude.handle_whitelist_cmd(inner[9:].strip())
            return self.privileged_claude.handle(inner)

        # ?question → Claude
        if text.startswith("?"):
            return self.claude.handle(text[1:].strip())

        # Preset match
        preset_reply = self.preset.handle(text)
        if preset_reply is not None:
            return preset_reply

        # Default: forward to Claude
        return self.claude.handle(text)
