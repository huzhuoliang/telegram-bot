"""Message handlers: shell execution, Claude AI, and preset responses."""

import logging
import re
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Claude uses these markers in its response to trigger media actions.
# Formats:
#   [PHOTO: <url_or_path>]
#   [PHOTO: <url_or_path> | <caption>]
#   [VIDEO: <url_or_path>]
#   [VIDEO: <url_or_path> | <caption>]
_ACTION_RE = re.compile(r'\[(PHOTO|VIDEO):\s*([^\]|]+?)(?:\s*\|\s*([^\]]*))?\]')

_SYSTEM_PROMPT = """You are a helpful assistant running as a Telegram bot on the user's personal server. Be concise. Telegram supports basic Markdown.

IMPORTANT: Every message must have a text reply. Never return an empty response.

You can send media to the user by including action markers anywhere in your response:
  [PHOTO: <url_or_path>]
  [PHOTO: <url_or_path> | <caption>]
  [VIDEO: <url_or_path>]
  [VIDEO: <url_or_path> | <caption>]

These markers are automatically extracted and executed — the user will receive the media directly. Use publicly accessible URLs (e.g. Wikipedia Commons, direct image links). Do not explain how to use any CLI tool; just include the marker and the media will be delivered.

When the user asks to search for or find a photo/video of someone, always find a URL and send it using the marker. "搜索一张X的照片" means find and send a photo of X.

Examples:
- "搜索一张杨幂的照片" → [PHOTO: https://...] 这是杨幂的照片。
- "发一张刘亦菲的图片" → [PHOTO: https://...] 已发送。
- User asks to send a file on the server → [PHOTO: /home/user/image.jpg] 已发送。
"""


class ShellHandler:
    def __init__(self, timeout: int = 30, max_chars: int = 3000, cwd: str = None):
        self.timeout = timeout
        self.max_chars = max_chars
        self.cwd = cwd or str(Path.home())

    def _is_sudo(self, command: str) -> bool:
        import shlex
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        return bool(tokens) and tokens[0] == "sudo"

    def handle(self, command: str) -> str:
        if not command:
            return "Usage: !<shell command>"
        if self._is_sudo(command):
            return "Error: sudo is not allowed"
        logger.info("Shell: %s", command)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.cwd,
            )
            output = result.stdout + result.stderr
            truncated = False
            if len(output) > self.max_chars:
                output = output[: self.max_chars]
                truncated = True
            reply = f"Exit: {result.returncode}\n{output}"
            if truncated:
                reply += f"\n[truncated at {self.max_chars} chars]"
            return reply.strip()
        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.timeout}s"
        except Exception as e:
            return f"Error: {e}"


class ClaudeHandler:
    """Supports two backends, switchable via the 'claude_backend' config key:

    - "cli"  (default): invokes `claude -p <text>` as a subprocess.
              Uses existing Claude Code credentials; no API key needed.
              Each call is stateless (no conversation history).

    - "api": calls Anthropic SDK directly.
              Requires ANTHROPIC_API_KEY env var.
              Maintains a rolling conversation history (claude_history_turns).

    Both backends support action markers ([PHOTO: url], [VIDEO: url]) in
    Claude's response, which are automatically extracted and sent via Telegram.
    """

    def __init__(
        self,
        backend: str = "cli",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        history_turns: int = 6,
        cli_timeout: int = 60,
        telegram_client=None,
    ):
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens
        self.history_turns = history_turns
        self.cli_timeout = cli_timeout
        self._telegram_client = telegram_client
        self._history: list[dict] = []
        self._lock = threading.Lock()
        self._api_client = None  # lazy-init only if backend == "api"

    def _get_api_client(self):
        if self._api_client is None:
            import anthropic
            self._api_client = anthropic.Anthropic()
        return self._api_client

    def _execute_actions(self, response: str) -> str:
        """Extract [PHOTO:] / [VIDEO:] markers, send the media, return cleaned text."""
        if not self._telegram_client:
            return response

        def _run(match):
            kind = match.group(1)
            target = match.group(2).strip()
            caption = (match.group(3) or "").strip()
            logger.info("Action from Claude: %s %s", kind, target)
            if kind == "PHOTO":
                self._telegram_client.send_photo(target, caption)
            elif kind == "VIDEO":
                self._telegram_client.send_video(target, caption)
            return ""  # remove marker from text reply

        cleaned = _ACTION_RE.sub(_run, response).strip()
        # If the entire response was action markers, ensure we still send something
        return cleaned or "已发送。"

    def _call_cli(self, text: str) -> str:
        prompt = f"{_SYSTEM_PROMPT}\nUser: {text}"
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=self.cli_timeout,
        )
        raw = result.stdout.strip() or result.stderr.strip() or "No response"
        return self._execute_actions(raw)

    def _call_api(self, text: str) -> str:
        # history is already locked by caller
        self._history.append({"role": "user", "content": text})
        max_msgs = self.history_turns * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]
        client = self._get_api_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_SYSTEM_PROMPT,
            messages=self._history,
        )
        raw = response.content[0].text
        # Store cleaned text in history so markers don't pollute future context
        cleaned = self._execute_actions(raw)
        self._history.append({"role": "assistant", "content": cleaned or raw})
        return cleaned

    def handle(self, text: str) -> str:
        if not text:
            return "Usage: ?<question>"
        logger.info("Claude [%s]: %s", self.backend, text[:80])
        with self._lock:
            try:
                if self.backend == "api":
                    return self._call_api(text)
                else:
                    return self._call_cli(text)
            except subprocess.TimeoutExpired:
                return f"Claude CLI timed out after {self.cli_timeout}s"
            except FileNotFoundError:
                return "Error: `claude` CLI not found in PATH"
            except Exception as e:
                if self.backend == "api" and self._history and self._history[-1]["role"] == "user":
                    self._history.pop()  # roll back poisoned history entry
                logger.warning("Claude error: %s", e)
                return f"Claude error: {e}"

    def clear_history(self) -> str:
        with self._lock:
            self._history.clear()
        return "Conversation history cleared."


class PresetHandler:
    def __init__(self, presets: dict):
        self._presets = {k.lower(): v for k, v in presets.items()}

    def handle(self, text: str) -> str | None:
        return self._presets.get(text.lower().strip())
