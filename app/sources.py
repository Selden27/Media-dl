"""Extra identification and genre sources (step 3 of docs/tags-plan.md): AcoustID, Discogs, Last.fm.

Each one is optional: without its key, it is simply skipped.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time

import requests

log = logging.getLogger("media-dl")

ACOUSTID_KEY = os.getenv("ACOUSTID_API_KEY", "").strip()
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "").strip()
LASTFM_KEY = os.getenv("LASTFM_API_KEY", "").strip()
USER_AGENT = "MediaDL/1.0 +https://github.com/Selden27/media-dl"

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_cache = {}
_locks = {name: (threading.Lock(), [0.0]) for name in ("acoustid", "discogs", "lastfm")}
_MIN_GAP = {"acoustid": 0.35, "discogs": 1.1, "lastfm": 0.25}  # requests per second allowed by each API


def _throttle(name):
    lock, last = _locks[name]
    with lock:
        wait = _MIN_GAP[name] - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.time()


def _get(name, url, params=None, headers=None, data=None):
    key = (url, json.dumps(params, sort_keys=True), json.dumps(data, sort_keys=True))
    if key in _cache:
        return _cache[key]
    for attempt in range(3):
        _throttle(name)
        try:
            r = (_session.post(url, data=data, headers=headers, timeout=20) if data is not None
                 else _session.get(url, params=params, headers=headers, timeout=20))
        except requests.RequestException as e:
            log.warning("%s: %s", name, e)
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(3 + attempt * 3)
            continue
        if r.status_code == 404:
            return None
        if not r.ok:
            log.warning("%s: HTTP %s %s", name, r.status_code, r.text[:200])
            return None
        result = r.json()
        _cache[key] = result
        return result
    return None


def parse_duration(text):
    """'4:32' or '1:02:03' -> seconds."""
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs or None


# ------------------------------------------------------------------ AcoustID
def fingerprint(path):
    try:
        out = subprocess.run(["fpcalc", "-json", path], capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout)
        return int(data["duration"]), data["fingerprint"]
    except Exception as e:
        log.warning("fpcalc: %s", e)
        return None, None


def acoustid_lookup(path):
    """[(score, musicbrainz recording id)] best first, from the audio itself."""
    if not ACOUSTID_KEY or not path:
        return []
    duration, fp = fingerprint(path)
    if not fp:
        return []
    data = _get("acoustid", "https://api.acoustid.org/v2/lookup",
                data={"client": ACOUSTID_KEY, "duration": duration, "fingerprint": fp, "meta": "recordingids"})
    if not data or data.get("status") != "ok":
        if data:
            log.warning("acoustid: %s", data.get("error"))
        return []
    found = {}
    for res in data.get("results") or []:
        for rec in res.get("recordings") or []:
            found[rec["id"]] = max(found.get(rec["id"], 0), res.get("score", 0))
    return sorted(((s, rid) for rid, s in found.items()), reverse=True)


# ------------------------------------------------------------------- Discogs
def _discogs(path, **params):
    return _get("discogs", f"https://api.discogs.com/{path}", params=params,
                headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}"})


def _discogs_artist(name):
    return re.sub(r"\s*\(\d+\)$", "", name or "").replace("*", "").strip()


def discogs_search(artist, title=None, album=None):
    """Search releases; returns the raw result list (title, year, genre, style, cover_image, id…)."""
    if not DISCOGS_TOKEN:
        return []
    params = {"type": "release", "per_page": 10}
    if artist:
        params["artist"] = artist
    if album:
        params["release_title"] = album
    elif title:
        params["track"] = title
    data = _discogs("database/search", **params) or {}
    return data.get("results") or []


def discogs_release(release_id):
    if not DISCOGS_TOKEN or not release_id:
        return None
    return _discogs(f"releases/{release_id}")


def discogs_image(release):
    imgs = (release or {}).get("images") or []
    primary = next((i for i in imgs if i.get("type") == "primary"), imgs[0] if imgs else None)
    if not primary:
        return None
    try:
        r = _session.get(primary.get("uri") or primary.get("resource_url"), timeout=30)
        return r.content if r.ok else None
    except requests.RequestException:
        return None


def discogs_genre_votes(results, weight=1.0):
    votes = []
    for res in results[:3]:
        for g in res.get("genre") or []:
            votes.append((g, 1.5 * weight))
        for s in res.get("style") or []:
            votes.append((s, 2.5 * weight))
    return votes


# -------------------------------------------------------------------- Last.fm
def _lastfm(method, **params):
    if not LASTFM_KEY:
        return None
    return _get("lastfm", "https://ws.audioscrobbler.com/2.0/",
                params={"method": method, "api_key": LASTFM_KEY, "format": "json", "autocorrect": 1, **params})


def lastfm_genre_votes(artist, title):
    """Genre votes from the track's and the artist's top tags."""
    votes = []
    if not LASTFM_KEY or not artist:
        return votes
    if title:
        data = _lastfm("track.getTopTags", artist=artist, track=title) or {}
        for t in ((data.get("toptags") or {}).get("tag") or [])[:8]:
            if int(t.get("count") or 0) >= 10:
                votes.append((t["name"], 2.0 * int(t["count"]) / 100))
    data = _lastfm("artist.getTopTags", artist=artist) or {}
    for t in ((data.get("toptags") or {}).get("tag") or [])[:8]:
        if int(t.get("count") or 0) >= 10:
            votes.append((t["name"], 1.0 * int(t["count"]) / 100))
    return votes


# ---------------------------------------------------------------- self test
def self_test():
    """Check each configured key once at start-up and log the result (visible in Coolify logs)."""
    report = {}
    if ACOUSTID_KEY:
        d = _session.get("https://api.acoustid.org/v2/lookup",
                         params={"client": ACOUSTID_KEY, "duration": 1, "fingerprint": "AQAA"}, timeout=15).json()
        bad = (d.get("error") or {}).get("code") == 4
        report["AcoustID"] = "clé invalide" if bad else "OK"
    if DISCOGS_TOKEN:
        r = _session.get("https://api.discogs.com/oauth/identity",
                         headers={"Authorization": f"Discogs token={DISCOGS_TOKEN}"}, timeout=15)
        report["Discogs"] = "OK" if r.ok else f"erreur {r.status_code}"
    if LASTFM_KEY:
        d = _session.get("https://ws.audioscrobbler.com/2.0/", params={"method": "artist.getInfo", "artist": "Cher",
                         "api_key": LASTFM_KEY, "format": "json"}, timeout=15).json()
        report["Last.fm"] = "clé invalide" if d.get("error") else "OK"
    try:
        subprocess.run(["fpcalc", "-version"], capture_output=True, timeout=10, check=True)
        report["fpcalc"] = "OK"
    except Exception:
        report["fpcalc"] = "absent"
    log.warning("sources de tags : %s", ", ".join(f"{k} {v}" for k, v in report.items()) or "aucune clé")
    return report
