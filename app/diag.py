"""Network diagnostics, run inside the container:  python -m app.diag [url]

Prints the public IP seen with and without YTDLP_PROXY, then tries yt-dlp on `url`.
"""
import os
import sys

import requests
import yt_dlp

PROXY = os.getenv("YTDLP_PROXY", "")


def trace(proxy=None):
    try:
        r = requests.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=15,
                         proxies={"http": proxy, "https": proxy} if proxy else None)
        info = dict(line.split("=", 1) for line in r.text.strip().splitlines() if "=" in line)
        return f"ip={info.get('ip')} warp={info.get('warp')} colo={info.get('colo')}"
    except Exception as e:
        return f"error: {e}"


def try_url(url, proxy=None):
    opts = {"quiet": True, "no_warnings": True, "simulate": True}
    if proxy:
        opts["proxy"] = proxy
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return f"OK: {info.get('title')}"
    except Exception as e:
        return f"FAIL: {str(e)[:160]}"


if __name__ == "__main__":
    print("direct :", trace())
    if PROXY:
        print("proxy  :", trace(PROXY))
    if len(sys.argv) > 1:
        print("yt-dlp direct:", try_url(sys.argv[1]))
        if PROXY:
            print("yt-dlp proxy :", try_url(sys.argv[1], PROXY))
