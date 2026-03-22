#!/usr/bin/env python3
"""CLI helper to send a Telegram notification via the local bot service.

Usage:
    python3 send.py "Your message here"
    python3 send.py "Your message here" --port 8765
"""

import argparse
import json
import sys
import urllib.request
import urllib.error


def main():
    parser = argparse.ArgumentParser(description="Send a Telegram notification via the local bot")
    parser.add_argument("text", help="Message text to send")
    parser.add_argument("--port", type=int, default=8765, help="Notify server port (default: 8765)")
    args = parser.parse_args()

    body = json.dumps({"text": args.text}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/send",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("OK")
            else:
                print(f"HTTP {resp.status}", file=sys.stderr)
                sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: {e.reason}", file=sys.stderr)
        print("Is the bot service running?", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
