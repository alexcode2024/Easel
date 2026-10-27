"""One browser configuration for Xiaohongshu transports.

HTTP uses an explicit supported Windows Chrome profile. Playwright keeps its
native UA and Client Hints so its declared version matches its actual engine.
"""
from __future__ import annotations

from typing import Literal

IMPERSONATE = "chrome152"
CHROME_MAJOR = 152
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
CLIENT_HINTS = (
    f'"Chromium";v="{CHROME_MAJOR}", "Not?A_Brand";v="24", '
    f'"Google Chrome";v="{CHROME_MAJOR}"'
)
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,zh-TW;q=0.8"
LOCALE = "zh-CN"
TIMEZONE = "Asia/Shanghai"


def http_options(kind: Literal["document", "api", "media"], *,
                 referer: str = "https://www.xiaohongshu.com/") -> dict:
    """Return fresh transport options; callers add cookies and API signatures."""
    headers = {
        "user-agent": USER_AGENT,
        "sec-ch-ua": CLIENT_HINTS,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "accept-language": ACCEPT_LANGUAGE,
        "referer": referer,
    }
    if kind == "api":
        headers.update({
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.xiaohongshu.com",
            "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-site",
        })
    elif kind == "document":
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin", "upgrade-insecure-requests": "1",
        })
    elif kind == "media":
        headers["accept"] = "*/*"
    else:
        raise ValueError(f"unknown Xiaohongshu request kind: {kind}")
    # Disable the library's navigation defaults on API/media requests. TLS/HTTP
    # impersonation remains enabled; the networking library handles compression.
    return {"impersonate": IMPERSONATE, "default_headers": False, "headers": headers}


def context_options() -> dict:
    """Browser engine owns UA/Client Hints; share non-version context settings."""
    return {
        "locale": LOCALE, "timezone_id": TIMEZONE,
        "viewport": {"width": 1440, "height": 900},
        "extra_http_headers": {"Accept-Language": ACCEPT_LANGUAGE},
    }


def signer():
    """Align xhshow's embedded signing UA with the actual HTTP profile."""
    from xhshow import Xhshow
    from xhshow.config.config import CryptoConfig

    return Xhshow(CryptoConfig(PUBLIC_USERAGENT=USER_AGENT))


def read_cover(url: str, referer: str) -> tuple[bytes, str]:
    """Read a bounded CDN cover with the same HTTP identity, without cookies."""
    from curl_cffi import requests
    import xhs_access

    response = None
    try:
        response = requests.get(url, **http_options("media", referer=referer),
                                timeout=20, stream=True)
        xhs_access.inspect_status(response)
        if response.status_code != 200:
            raise OSError(f"封面 HTTP {response.status_code}")
        data = bytearray()
        limit = 8 * 1024 * 1024
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk[:limit - len(data)])
            if len(data) >= limit:
                break
        return bytes(data), response.headers.get("Content-Type", "")
    except requests.RequestsError as error:
        raise OSError("封面读取失败") from error
    finally:
        if response is not None:
            response.close()
