"""Bilibili cookie management: validate, QR-code login, and auto-refresh.

Usage (programmatic):
    from bilibili_cookies import check_cookie_valid, qr_login, start_refresh_scheduler

    if not check_cookie_valid("/path/to/cookies.txt"):
        qr_login("/path/to/cookies.txt", send_photo_fn, reply_fn)

    # Start daily background refresh (call once at bot startup)
    start_refresh_scheduler("/path/to/cookies.txt", interval_hours=24)

Standalone test:
    python3 bilibili_cookies.py /path/to/cookies.txt
"""

import hashlib
import html.parser
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

# B站 RSA 公钥，用于 cookie 刷新时计算 correspondPath
_RSA_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg\n"
    "Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71\n"
    "nzPjfdTcqMz7djHKETI/HgZKFJDMlS//3Dm0wRFaadOpYv00d7Hh3Mkt7KhXIBN6\n"
    "czaYmlz/3jnkM0GJGQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

# Cache validity check result to avoid hitting API on every /dl
_valid_cache: dict = {"ts": 0.0, "ok": False}
VALID_CACHE_TTL = 1800  # 30 minutes


# ------------------------------------------------------------------
# Cookie validation
# ------------------------------------------------------------------

def check_cookie_valid(cookie_path: str | Path) -> bool:
    """Check if Bilibili cookie file contains a valid logged-in VIP session.

    Results are cached for 30 minutes to avoid excessive API calls.
    """
    now = time.time()
    if now - _valid_cache["ts"] < VALID_CACHE_TTL:
        return _valid_cache["ok"]

    ok = _do_check(cookie_path)
    _valid_cache["ts"] = now
    _valid_cache["ok"] = ok
    return ok


def invalidate_cache():
    """Force next check_cookie_valid() to hit the API."""
    _valid_cache["ts"] = 0.0
    _valid_cache["ok"] = False


def _do_check(cookie_path: str | Path) -> bool:
    cookie_path = Path(cookie_path)
    if not cookie_path.exists():
        logger.info("Bilibili cookie file not found: %s", cookie_path)
        return False

    sessdata = _parse_cookie_value(cookie_path, "SESSDATA")
    if not sessdata:
        logger.info("SESSDATA not found in %s", cookie_path)
        return False

    try:
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": f"SESSDATA={sessdata}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        if data.get("code") != 0:
            logger.info("Bilibili nav API returned code=%s", data.get("code"))
            return False
        nav = data.get("data", {})
        is_login = nav.get("isLogin", False)
        vip_status = nav.get("vipStatus", 0)
        uname = nav.get("uname", "")
        logger.info("Bilibili cookie: login=%s, vip=%s, user=%s", is_login, vip_status, uname)
        return bool(is_login and vip_status == 1)
    except Exception as e:
        logger.warning("Bilibili cookie check failed: %s", e)
        return False


# ------------------------------------------------------------------
# QR-code login
# ------------------------------------------------------------------

def qr_login(
    cookie_path: str | Path,
    send_photo_fn,
    reply_fn,
    timeout: int = 120,
) -> bool:
    """Run the full Bilibili QR-code login flow.

    Args:
        cookie_path: Where to write the Netscape cookie file on success.
        send_photo_fn(image_path: str, caption: str): Send QR image to user.
        reply_fn(text: str): Send status text to user.
        timeout: Max seconds to wait for the user to scan.

    Returns True if login succeeded and cookies were saved.
    """
    cookie_path = Path(cookie_path)

    # Step 1: Request QR code
    try:
        qrcode_key, qr_url = _generate_qrcode()
    except Exception as e:
        logger.exception("Failed to generate Bilibili QR code: %s", e)
        reply_fn(f"❌ 获取B站登录二维码失败：{e}")
        return False

    # Step 2: Render QR image
    try:
        import qrcode
        img = qrcode.make(qr_url)
        qr_path = Path("/tmp/bilibili_qr.png")
        img.save(str(qr_path))
    except Exception as e:
        logger.exception("Failed to render QR code: %s", e)
        reply_fn(f"❌ 生成二维码图片失败：{e}")
        return False

    # Step 3: Send to user
    send_photo_fn(str(qr_path), "🔑 B站 cookie 已失效，请用B站APP扫码登录（2分钟超时）")

    # Step 4: Poll for scan result
    try:
        ok = _poll_qr_login(qrcode_key, cookie_path, reply_fn, timeout)
    finally:
        qr_path.unlink(missing_ok=True)

    if ok:
        invalidate_cache()
    return ok


def _generate_qrcode() -> tuple[str, str]:
    """Call Bilibili QR generate API. Returns (qrcode_key, url)."""
    req = urllib.request.Request(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        headers={"User-Agent": USER_AGENT},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read().decode())
    if result.get("code") != 0:
        raise RuntimeError(f"QR generate API error: {result}")
    data = result["data"]
    return data["qrcode_key"], data["url"]


def _poll_qr_login(
    qrcode_key: str,
    cookie_path: Path,
    reply_fn,
    timeout: int,
) -> bool:
    """Poll until user scans, confirms, or timeout."""
    start = time.time()
    notified_scanned = False

    while time.time() - start < timeout:
        time.sleep(3)
        try:
            code, cookies, refresh_token = _poll_once(qrcode_key)
        except Exception as e:
            logger.warning("QR poll error: %s", e)
            continue

        if code == 0:
            # Success — save cookies and refresh_token
            _write_netscape_cookies(cookies, cookie_path)
            if refresh_token:
                _save_refresh_token(cookie_path, refresh_token)
            reply_fn("✅ B站登录成功！继续下载…")
            logger.info("Bilibili QR login success, cookies saved to %s", cookie_path)
            return True
        elif code == 86038:
            reply_fn("❌ 二维码已过期，请重新发起 /dl 下载。")
            return False
        elif code == 86090 and not notified_scanned:
            reply_fn("📱 已扫码，请在手机上确认登录…")
            notified_scanned = True
        # 86101 = not scanned yet — keep waiting

    reply_fn("❌ 扫码登录超时（2分钟），将使用匿名模式下载。")
    return False


def _poll_once(qrcode_key: str) -> tuple[int, dict[str, str], str]:
    """Single poll request. Returns (status_code, cookies_dict, refresh_token).

    We use a raw HTTP connection to capture Set-Cookie headers,
    which urllib's default opener would silently handle/redirect.
    """
    import http.client

    params = urllib.parse.urlencode({"qrcode_key": qrcode_key})
    conn = http.client.HTTPSConnection("passport.bilibili.com", timeout=10)
    try:
        conn.request(
            "GET",
            f"/x/passport-login/web/qrcode/poll?{params}",
            headers={"User-Agent": USER_AGENT},
        )
        resp = conn.getresponse()
        body = resp.read().decode()
        result = json.loads(body)
        data = result.get("data", {})
        code = data.get("code", -1)

        cookies: dict[str, str] = {}
        refresh_token = ""
        if code == 0:
            # Extract cookies from Set-Cookie headers
            for header_value in resp.headers.get_all("Set-Cookie") or []:
                part = header_value.split(";")[0]
                name, _, value = part.partition("=")
                name = name.strip()
                if name:
                    cookies[name] = value.strip()
            logger.info("QR login cookies received: %s", list(cookies.keys()))
            refresh_token = data.get("refresh_token", "")

        return code, cookies, refresh_token
    finally:
        conn.close()


# ------------------------------------------------------------------
# Cookie auto-refresh
# ------------------------------------------------------------------

def refresh_cookie(cookie_path: str | Path) -> bool:
    """Attempt to refresh Bilibili cookie using the refresh_token mechanism.

    Flow:
      1. Check if refresh is needed via cookie/info API
      2. Compute correspondPath (RSA-OAEP encrypted timestamp)
      3. Fetch refresh_csrf from the correspond page
      4. POST /web/cookie/refresh to get new cookies
      5. POST /web/confirm/refresh to finalize

    Returns True if refresh succeeded.
    """
    cookie_path = Path(cookie_path)

    sessdata = _parse_cookie_value(cookie_path, "SESSDATA")
    bili_jct = _parse_cookie_value(cookie_path, "bili_jct")
    refresh_token = _load_refresh_token(cookie_path)

    if not all([sessdata, bili_jct, refresh_token]):
        logger.info("Bilibili refresh: missing credentials (sessdata=%s, jct=%s, token=%s)",
                     bool(sessdata), bool(bili_jct), bool(refresh_token))
        return False

    cookie_header = f"SESSDATA={sessdata}; bili_jct={bili_jct}"

    # Step 1: Check if refresh is needed
    try:
        req = urllib.request.Request(
            f"https://passport.bilibili.com/x/passport-login/web/cookie/info?csrf={bili_jct}",
            headers={"User-Agent": USER_AGENT, "Cookie": cookie_header},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        info = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Bilibili cookie info check failed: %s", e)
        return False

    if info.get("code") == -101:
        logger.info("Bilibili session already expired (not logged in), refresh impossible")
        return False

    if info.get("code") != 0:
        logger.warning("Bilibili cookie info API error: %s", info)
        return False

    refresh_needed = info.get("data", {}).get("refresh", False)
    timestamp = info.get("data", {}).get("timestamp", 0)

    if not refresh_needed:
        logger.info("Bilibili cookie refresh not needed yet")
        # Cookie is still healthy — update cache
        invalidate_cache()
        return True

    logger.info("Bilibili cookie refresh needed (timestamp=%s)", timestamp)

    # Step 2: Compute correspondPath via RSA-OAEP
    try:
        correspond_path = _compute_correspond_path(timestamp)
    except Exception as e:
        logger.warning("Failed to compute correspondPath: %s", e)
        return False

    # Step 3: Get refresh_csrf from correspond page
    try:
        refresh_csrf = _fetch_refresh_csrf(correspond_path, cookie_header)
    except Exception as e:
        logger.warning("Failed to fetch refresh_csrf: %s", e)
        return False

    if not refresh_csrf:
        logger.warning("refresh_csrf not found in correspond page")
        return False

    # Step 4: Do the refresh (raw HTTP to capture Set-Cookie)
    try:
        new_cookies, new_refresh_token = _do_refresh(
            bili_jct, refresh_csrf, refresh_token, cookie_header
        )
    except Exception as e:
        logger.warning("Bilibili cookie refresh POST failed: %s", e)
        return False

    if not new_cookies.get("SESSDATA"):
        logger.warning("Refresh response missing SESSDATA")
        return False

    # Step 5: Confirm refresh
    new_bili_jct = new_cookies.get("bili_jct", "")
    try:
        _confirm_refresh(new_bili_jct, refresh_token)
    except Exception as e:
        # Non-fatal: cookies are already refreshed
        logger.warning("Refresh confirm failed (non-fatal): %s", e)

    # Save new cookies and refresh_token
    _write_netscape_cookies(new_cookies, cookie_path)
    if new_refresh_token:
        _save_refresh_token(cookie_path, new_refresh_token)

    invalidate_cache()
    logger.info("Bilibili cookie refreshed successfully")
    return True


def _compute_correspond_path(timestamp: int) -> str:
    """Encrypt 'refresh_{timestamp}' with Bilibili's RSA public key (OAEP/SHA-256)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    pub_key = serialization.load_pem_public_key(_RSA_PUBLIC_KEY_PEM.encode())
    plaintext = f"refresh_{timestamp}".encode()
    ciphertext = pub_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return ciphertext.hex()


class _CorrespondParser(html.parser.HTMLParser):
    """Extract refresh_csrf from <div id="1-name">...</div>."""

    def __init__(self):
        super().__init__()
        self._in_target = False
        self.refresh_csrf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            for name, value in attrs:
                if name == "id" and value == "1-name":
                    self._in_target = True

    def handle_data(self, data):
        if self._in_target:
            self.refresh_csrf = data.strip()
            self._in_target = False


def _fetch_refresh_csrf(correspond_path: str, cookie_header: str) -> str:
    """Fetch the correspond page and extract refresh_csrf."""
    req = urllib.request.Request(
        f"https://www.bilibili.com/correspond/1/{correspond_path}",
        headers={"User-Agent": USER_AGENT, "Cookie": cookie_header},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    page_html = resp.read().decode()

    parser = _CorrespondParser()
    parser.feed(page_html)
    return parser.refresh_csrf


def _do_refresh(
    old_csrf: str,
    refresh_csrf: str,
    refresh_token: str,
    cookie_header: str,
) -> tuple[dict[str, str], str]:
    """POST cookie refresh. Returns (new_cookies, new_refresh_token).

    Uses raw http.client to capture Set-Cookie headers.
    """
    import http.client

    body = urllib.parse.urlencode({
        "csrf": old_csrf,
        "refresh_csrf": refresh_csrf,
        "source": "main_web",
        "refresh_token": refresh_token,
    })

    conn = http.client.HTTPSConnection("passport.bilibili.com", timeout=10)
    try:
        conn.request(
            "POST",
            "/x/passport-login/web/cookie/refresh",
            body=body,
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": cookie_header,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp = conn.getresponse()
        resp_body = resp.read().decode()
        result = json.loads(resp_body)

        if result.get("code") != 0:
            raise RuntimeError(f"Refresh API error: {result}")

        new_refresh_token = result.get("data", {}).get("refresh_token", "")

        cookies: dict[str, str] = {}
        for header_value in resp.headers.get_all("Set-Cookie") or []:
            part = header_value.split(";")[0]
            name, _, value = part.partition("=")
            name = name.strip()
            if name:
                cookies[name] = value.strip()

        return cookies, new_refresh_token
    finally:
        conn.close()


def _confirm_refresh(new_csrf: str, old_refresh_token: str) -> None:
    """POST confirm to acknowledge the refresh (invalidates old refresh_token)."""
    body = urllib.parse.urlencode({
        "csrf": new_csrf,
        "refresh_token": old_refresh_token,
    }).encode()

    req = urllib.request.Request(
        "https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read().decode())
    if result.get("code") != 0:
        raise RuntimeError(f"Confirm refresh error: {result}")
    logger.info("Bilibili refresh confirmed (old token invalidated)")


# ------------------------------------------------------------------
# Background refresh scheduler
# ------------------------------------------------------------------

_scheduler_started = False


def start_refresh_scheduler(cookie_path: str | Path, interval_hours: int = 24) -> None:
    """Start a daemon thread that refreshes Bilibili cookie periodically.

    Safe to call multiple times — only the first call starts the thread.
    Refresh failures are silently logged (no user notification).
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    cookie_path = Path(cookie_path).expanduser()
    interval = interval_hours * 3600

    def _loop():
        logger.info("Bilibili cookie refresh scheduler started (every %dh)", interval_hours)
        while True:
            time.sleep(interval)
            try:
                ok = refresh_cookie(cookie_path)
                logger.info("Bilibili scheduled refresh: %s", "success" if ok else "skipped/failed")
            except Exception:
                logger.exception("Bilibili scheduled refresh error")

    t = threading.Thread(target=_loop, name="bilibili-refresh", daemon=True)
    t.start()


# ------------------------------------------------------------------
# Cookie file I/O
# ------------------------------------------------------------------

def _parse_cookie_value(cookie_path: Path, name: str) -> str | None:
    """Extract a cookie value from a Netscape cookie file."""
    try:
        for line in cookie_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] == name:
                return parts[6]
    except Exception:
        pass
    return None


def _write_netscape_cookies(cookies: dict[str, str], path: Path) -> None:
    """Write cookies in Netscape/Mozilla cookies.txt format for yt-dlp."""
    path.parent.mkdir(parents=True, exist_ok=True)

    domain = ".bilibili.com"
    expiry = str(int(time.time()) + 365 * 86400)

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by bilibili_cookies.py",
        "",
    ]
    for name, value in cookies.items():
        secure = "TRUE" if name == "SESSDATA" else "FALSE"
        lines.append(f"{domain}\tTRUE\t/\t{secure}\t{expiry}\t{name}\t{value}")

    path.write_text("\n".join(lines) + "\n")
    logger.info("Wrote %d cookies to %s", len(cookies), path)


def _refresh_token_path(cookie_path: Path) -> Path:
    """Path for the refresh_token JSON file, alongside the cookie file."""
    return cookie_path.with_suffix(".refresh.json")


def _save_refresh_token(cookie_path: Path | str, token: str) -> None:
    path = _refresh_token_path(Path(cookie_path))
    path.write_text(json.dumps({"refresh_token": token, "ts": time.time()}))
    logger.info("Saved refresh_token to %s", path)


def _load_refresh_token(cookie_path: Path | str) -> str | None:
    path = _refresh_token_path(Path(cookie_path))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("refresh_token")
    except Exception:
        return None


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cookie_file = sys.argv[1] if len(sys.argv) > 1 else "bilibili_cookie.txt"

    # Try refresh first
    if Path(cookie_file).exists():
        print("Attempting cookie refresh...")
        ok = refresh_cookie(cookie_file)
        print(f"Refresh result: {ok}")
        if ok:
            invalidate_cache()
            valid = check_cookie_valid(cookie_file)
            print(f"Cookie valid after refresh: {valid}")
            sys.exit(0)

    valid = check_cookie_valid(cookie_file)
    print(f"Cookie valid: {valid}")

    if not valid:
        print("Starting QR login...")

        def _cli_send_photo(path, caption):
            print(caption)
            try:
                import qrcode
                key, url = _generate_qrcode()
                qr = qrcode.QRCode()
                qr.add_data(url)
                qr.print_ascii()
            except Exception:
                print(f"QR image saved to: {path}")

        def _cli_reply(text):
            print(text)

        ok = qr_login(cookie_file, _cli_send_photo, _cli_reply)
        print(f"Login result: {ok}")
