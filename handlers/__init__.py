"""Handler classes for the Telegram bot."""

from handlers.shell import ShellHandler
from handlers.claude import ClaudeHandler
from handlers.privileged_claude import PrivilegedClaudeHandler
from handlers.preset import PresetHandler
from handlers.media_archive import MediaArchiveHandler, FileArchiveHandler
from handlers.video_download import VideoDownloadHandler

__all__ = [
    "ShellHandler",
    "ClaudeHandler",
    "PrivilegedClaudeHandler",
    "PresetHandler",
    "MediaArchiveHandler",
    "FileArchiveHandler",
    "VideoDownloadHandler",
]
