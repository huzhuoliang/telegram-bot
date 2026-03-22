#!/usr/bin/env python3
"""CLI helper to send Telegram notifications via the local bot service.

Usage:
    python3 send.py "Your message here"
    python3 send.py --photo /path/to/image.png
    python3 send.py --photo /path/to/image.png --caption "Today's report"
    python3 send.py --photo "https://example.com/img.png" --caption "From web"
    python3 send.py --port 8765 "message"
"""

import argparse
import json
import sys
import urllib.request
import urllib.error


def post(url: str, payload: dict, timeout: int = 30):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def main():
    parser = argparse.ArgumentParser(description="Send a Telegram notification via the local bot")
    parser.add_argument("text", nargs="?", help="Message text to send")
    parser.add_argument("--photo", help="Photo to send: local file path or URL")
    parser.add_argument("--caption", default="", help="Caption for the photo")
    parser.add_argument("--port", type=int, default=8765, help="Notify server port (default: 8765)")
    args = parser.parse_args()

    if not args.text and not args.photo:
        parser.error("Provide a message text or --photo")

    base = f"http://127.0.0.1:{args.port}"
    try:
        if args.photo:
            status = post(f"{base}/send_photo", {"photo": args.photo, "caption": args.caption})
        else:
            status = post(f"{base}/send", {"text": args.text})

        if status == 200:
            print("OK")
        else:
            print(f"HTTP {status}", file=sys.stderr)
            sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: {e.reason}", file=sys.stderr)
        print("Is the bot service running?", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
