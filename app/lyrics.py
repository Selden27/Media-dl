"""Lyrics from LRCLIB (free, no key): synced (LRC) when available, else plain text.

Evermusic reads the lyrics tag (ID3 USLT) and shows LRC timestamps as scrolling lyrics.
"""
import logging
import os

import requests

log = logging.getLogger("media-dl")
ENABLED = os.getenv("LYRICS", "true").lower() == "true"
API = "https://lrclib.net/api"
_session = requests.Session()
_session.headers["User-Agent"] = "MediaDL/1.0 (https://github.com/Selden27/media-dl)"


def _norm(s):
    return " ".join((s or "").lower().split())


def fetch(artist, title, album=None, duration=None):
    """Returns {"text", "synced", "instrumental"} or None when nothing reliable is found."""
    if not ENABLED or not artist or not title:
        return None
    try:
        rec = None
        if album and duration:
            r = _session.get(f"{API}/get", params={"artist_name": artist, "track_name": title,
                                                   "album_name": album, "duration": round(duration)}, timeout=15)
            rec = r.json() if r.ok else None
        if not rec:
            r = _session.get(f"{API}/search", params={"artist_name": artist, "track_name": title}, timeout=15)
            results = r.json() if r.ok else []
            # same title, and close duration when we know it (avoids another version's lyrics)
            results = [x for x in results if _norm(x.get("trackName")) == _norm(title)]
            if duration:
                results = [x for x in results if x.get("duration") and abs(x["duration"] - duration) <= 5]
            results.sort(key=lambda x: (not x.get("syncedLyrics"), not x.get("plainLyrics")))
            rec = results[0] if results else None
    except Exception as e:
        log.warning("lyrics: %s", e)
        return None
    if not rec:
        return None
    if rec.get("instrumental"):
        return {"text": None, "synced": False, "instrumental": True}
    text = rec.get("syncedLyrics") or rec.get("plainLyrics")
    return {"text": text, "synced": bool(rec.get("syncedLyrics")), "instrumental": False} if text else None
