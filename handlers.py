"""Message handlers: shell execution, Claude AI, and preset responses."""

import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


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
    """

    def __init__(
        self,
        backend: str = "cli",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        history_turns: int = 6,
        cli_timeout: int = 60,
    ):
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens
        self.history_turns = history_turns
        self.cli_timeout = cli_timeout
        self._history: list[dict] = []
        self._lock = threading.Lock()
        self._api_client = None  # lazy-init only if backend == "api"

    def _get_api_client(self):
        if self._api_client is None:
            import anthropic
            self._api_client = anthropic.Anthropic()
        return self._api_client

    def _call_cli(self, text: str) -> str:
        result = subprocess.run(
            ["claude", "-p", text],
            capture_output=True,
            text=True,
            timeout=self.cli_timeout,
        )
        return result.stdout.strip() or result.stderr.strip() or "No response"

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
            system=(
                "You are a helpful assistant running as a Telegram bot on the user's "
                "personal server. Be concise. Telegram supports basic Markdown."
            ),
            messages=self._history,
        )
        reply = response.content[0].text
        self._history.append({"role": "assistant", "content": reply})
        return reply

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
