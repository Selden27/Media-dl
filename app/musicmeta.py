"""Identify a downloaded track and gather its tags: MusicBrainz, Cover Art Archive, genre mapping.

Only free services without keys are used here (step 1 of docs/tags-plan.md).
"""
import io
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher

import requests
from PIL import Image

USER_AGENT = "MediaDL/1.0 ( https://github.com/Selden27/media-dl )"
MB = "https://musicbrainz.org/ws/2"
COVER_SIZE = 1000

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_mb_lock = threading.Lock()
_mb_last = [0.0]
_cache = {}

# Words that only describe the upload, removed from titles before searching.
NOISE = (r"official\s*(music\s*)?(video|audio|clip|visuali[sz]er|lyric video)?|clip\s*officiel|lyrics?(\s*video)?|"
         r"paroles|audio|video|hd|hq|4k|full\s*hd|visuali[sz]er|music\s*video|mv|explicit|remaster(ed)?(\s*\d{4})?|"
         r"\d{4}\s*remaster(ed)?|with lyrics|free download|out now|premiere")
_NOISE_BRACKET = re.compile(r"[\(\[\{]\s*(?:" + NOISE + r")\s*[\)\]\}]", re.I)
_FEAT = re.compile(r"\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring)\s+[^\)\]]*[\)\]]?", re.I)
VERSION_WORDS = ("live", "remix", "edit", "acoustic", "instrumental", "demo", "version", "mix", "rework")
GUIDED = re.compile(r"guided|guid[ée]e|m[ée]ditation\s+(guid|de\s|du\s|pour\s)|body\s*scan|yoga\s*nidra|talk", re.I)


# --------------------------------------------------------------------- helpers
def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = _FEAT.sub(" ", s)
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:  # "artist" vs "artist & friends"
        ratio = max(ratio, 0.85)
    return ratio


def _credit(ac):
    """Join a MusicBrainz artist-credit list into one string."""
    return "".join(c.get("name", c.get("artist", {}).get("name", "")) + c.get("joinphrase", "") for c in ac or []).strip()


def _mb_get(path, **params):
    key = (path, tuple(sorted(params.items())))
    if key in _cache:
        return _cache[key]
    params["fmt"] = "json"
    for attempt in range(3):
        with _mb_lock:  # MusicBrainz allows one request per second
            wait = 1.05 - (time.time() - _mb_last[0])
            if wait > 0:
                time.sleep(wait)
            _mb_last[0] = time.time()
        try:
            r = _session.get(f"{MB}/{path}", params=params, timeout=20)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 503:  # rate limited
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        _cache[key] = data
        return data
    return None


def _lucene(s):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


# ------------------------------------------------------------------- hints
def hints_from_info(info):
    """What the source site tells us: artist, title, album, year, duration, genres."""
    title = info.get("track") or info.get("title") or ""
    artist = info.get("artist") or info.get("creator") or ""
    if not artist and info.get("artists"):
        artist = ", ".join(info["artists"])
    uploader = (info.get("uploader") or info.get("channel") or "").strip()
    from_site = bool(info.get("track") and artist)

    if not info.get("track"):
        clean = _NOISE_BRACKET.sub("", title)
        clean = re.sub(r"\s*\|\s*.*$", "", clean)  # "Title | Channel promo"
        parts = re.split(r"\s+[-–—]\s+", clean, maxsplit=1)
        if len(parts) == 2 and not artist:
            artist, clean = parts
        elif len(parts) == 2 and _sim(parts[0], artist) > 0.7:
            clean = parts[1]
        title = clean.strip(" -–—\"'")
    if not artist and uploader:
        artist = re.sub(r"\s*-\s*Topic$|VEVO$|\s*Official$", "", uploader, flags=re.I).strip()

    genres = []
    for g in [info.get("genre")] + list(info.get("genres") or []):
        if g and g not in genres:
            genres.append(g)
    year = info.get("release_year")
    if not year and info.get("release_date"):
        year = int(str(info["release_date"])[:4])
    return {
        "title": title.strip(),
        "raw_title": info.get("title") or "",
        "artist": artist.strip(),
        "album": (info.get("album") or "").strip(),
        "year": year,
        "duration": info.get("duration"),
        "genres": genres,
        "from_site": from_site,
        "description": info.get("description") or "",
        "url": info.get("webpage_url") or info.get("original_url") or "",
    }


# ------------------------------------------------------------- musicbrainz
TYPE_RANK = {"Album": 3, "EP": 2, "Single": 1}


def _release_rank(rel):
    rg = rel.get("release-group") or {}
    secondary = rg.get("secondary-types") or []
    return (
        rel.get("status") == "Official",
        TYPE_RANK.get(rg.get("primary-type"), 0) - (2 if "Compilation" in secondary else 0) - (2 if "Live" in secondary else 0),
        -int((rel.get("date") or "9999")[:4] or 9999),
    )


def search_recording(h):
    """Return MusicBrainz candidates [(confidence, recording, release)], best first."""
    if not h["title"]:
        return []
    q = f'recording:"{_lucene(_norm(h["title"]) or h["title"])}"'
    if h["artist"]:
        q += f' AND artist:"{_lucene(h["artist"])}"'
    data = _mb_get("recording", query=q, limit=15) or {}
    out = []
    for rec in data.get("recordings", []):
        t = _sim(h["title"], rec.get("title"))
        a = _sim(h["artist"], _credit(rec.get("artist-credit"))) if h["artist"] else 0.5
        if h["duration"] and rec.get("length"):
            diff = abs(h["duration"] - rec["length"] / 1000)
            d = 1.0 if diff <= 3 else 0.7 if diff <= 10 else 0.3 if diff <= 30 else 0.0
        else:
            d = 0.5
        conf = 0.38 * t + 0.32 * a + 0.18 * d + 0.12 * (rec.get("score", 0) / 100)
        # A live / remix / edit version the source title does not mention is probably not the one.
        for word in VERSION_WORDS:
            if re.search(rf"\b{word}\b", rec.get("title") or "", re.I) and not re.search(rf"\b{word}\b", h["raw_title"], re.I):
                conf -= 0.12
        releases = sorted(rec.get("releases") or [], key=_release_rank, reverse=True)
        if h["album"]:  # the site named the album: prefer that release
            releases.sort(key=lambda r: _sim(h["album"], r.get("title")) > 0.8, reverse=True)
        out.append((round(conf, 3), rec, releases[0] if releases else None))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _genre_votes(entity_type, mbid, weight, votes):
    if not mbid:
        return
    data = _mb_get(f"{entity_type}/{mbid}", inc="genres+tags") or {}
    for g in data.get("genres") or []:
        votes.append((g["name"], weight * (1 + g.get("count", 0)) * 1.0))
    for t in data.get("tags") or []:
        votes.append((t["name"], weight * (1 + t.get("count", 0)) * 0.5))


def release_details(release_id, recording_id):
    rel = _mb_get(f"release/{release_id}", inc="artist-credits+labels+recordings+release-groups+isrcs") or {}
    track_no = disc_no = total_tracks = None
    isrcs = []
    for medium in rel.get("media") or []:
        for tr in medium.get("tracks") or []:
            if (tr.get("recording") or {}).get("id") == recording_id:
                track_no, disc_no = tr.get("position"), medium.get("position")
                total_tracks = medium.get("track-count")
                isrcs = (tr.get("recording") or {}).get("isrcs") or []
                track_id = tr.get("id")
                break
        if track_no:
            break
    else:
        track_id = None
    rg = rel.get("release-group") or {}
    ac = rel.get("artist-credit") or []
    labels = [li.get("label", {}).get("name") for li in rel.get("label-info") or [] if li.get("label")]
    return {
        "album": rel.get("title"),
        "albumartist": _credit(ac),
        "albumartist_id": (ac[0].get("artist") or {}).get("id") if ac else None,
        "release_id": release_id,
        "release_group_id": rg.get("id"),
        "release_type": rg.get("primary-type"),
        "compilation": "Compilation" in (rg.get("secondary-types") or []) or _credit(ac).lower() == "various artists",
        "date": rel.get("date") or rg.get("first-release-date"),
        "original_date": rg.get("first-release-date"),
        "label": labels[0] if labels else None,
        "track": track_no,
        "total_tracks": total_tracks,
        "disc": disc_no,
        "total_discs": len(rel.get("media") or []) or None,
        "isrc": isrcs[0] if isrcs else None,
        "track_id": track_id,
    }


# ----------------------------------------------------------------- genre
def choose_genre(library, votes, artist, title, description):
    """Pick one genre from the user's list. Returns (genre, unmapped_names)."""
    remembered = library.artist_genre(artist)
    if remembered:
        return remembered, []
    scores, unmapped = {}, {}
    for name, w in votes:
        g = library.map_genre(name)
        if g:
            scores[g] = scores.get(g, 0) + w
        else:
            unmapped[name.lower()] = unmapped.get(name.lower(), 0) + w
    genre = max(scores, key=scores.get) if scores else None
    text = f"{title} {description[:500]}"
    if GUIDED.search(title) or (genre in ("Meditation", "Meditative") and GUIDED.search(text)):
        genre = "Meditation" if "Meditation" in library.genres else genre
    elif genre == "Meditation":
        genre = "Meditative" if "Meditative" in library.genres else genre
    return genre, sorted(unmapped, key=unmapped.get, reverse=True)[:5]


# ----------------------------------------------------------------- cover
def _to_jpeg(raw, square=False):
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    if square and abs(img.width - img.height) > 2:
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
    if max(img.size) > COVER_SIZE:
        img.thumbnail((COVER_SIZE, COVER_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def fetch_cover(release_id, release_group_id):
    for kind, mbid in (("release", release_id), ("release-group", release_group_id)):
        if not mbid:
            continue
        try:
            r = _session.get(f"https://coverartarchive.org/{kind}/{mbid}/front-1200", timeout=30)
            if r.ok and r.content:
                return _to_jpeg(r.content), "Cover Art Archive"
        except Exception:
            continue
    return None, None


def thumbnail_cover(path):
    """Fallback: the video thumbnail, cropped to a square."""
    try:
        with open(path, "rb") as fh:
            return _to_jpeg(fh.read(), square=True)
    except Exception:
        return None


# ----------------------------------------------------------------- main
def identify(info, library):
    """Return a dict of tags + 'confidence' (0..1) + 'cover' (JPEG bytes or None)."""
    h = hints_from_info(info)
    tags = {
        "title": h["title"], "artist": h["artist"], "albumartist": h["artist"], "album": h["album"] or None,
        "year": str(h["year"]) if h["year"] else None, "url": h["url"], "confidence": 0.0, "source": "site",
        "genre": None, "unmapped_genres": [],
    }
    votes = [(g, 3.0) for g in h["genres"]]  # genre given by the site (SoundCloud, Bandcamp)

    candidates = search_recording(h)
    if candidates and candidates[0][0] >= 0.6:
        conf, rec, rel = candidates[0]
        tags.update(title=rec.get("title") or h["title"], artist=_credit(rec.get("artist-credit")) or h["artist"],
                    recording_id=rec.get("id"), source="MusicBrainz", confidence=conf)
        ac = rec.get("artist-credit") or []
        tags["artist_id"] = (ac[0].get("artist") or {}).get("id") if ac else None
        if rel:
            tags.update({k: v for k, v in release_details(rel["id"], rec["id"]).items() if v is not None})
            if tags.get("date"):
                tags["year"] = tags["date"][:4]
            _genre_votes("release-group", tags.get("release_group_id"), 1.0, votes)
        else:
            tags["confidence"] = round(conf * 0.9, 3)  # recording without any release: less sure
        _genre_votes("recording", rec.get("id"), 0.8, votes)
        _genre_votes("artist", tags.get("artist_id"), 0.6, votes)
    elif h["from_site"]:
        # The site gave artist + track itself (YouTube Music, SoundCloud): fairly reliable.
        tags["confidence"] = 0.7 if h["album"] else 0.6

    tags["genre"], tags["unmapped_genres"] = choose_genre(library, votes, tags["artist"], tags["title"], h["description"])
    if not tags.get("albumartist"):
        tags["albumartist"] = tags["artist"]
    if tags.get("compilation"):
        tags["albumartist"] = "Various Artists"

    cover, cover_src = (None, None)
    if tags.get("release_id"):
        cover, cover_src = fetch_cover(tags.get("release_id"), tags.get("release_group_id"))
    tags["cover"], tags["cover_source"] = cover, cover_src
    tags["candidates"] = [
        {"confidence": c, "title": r.get("title"), "artist": _credit(r.get("artist-credit")),
         "album": (rl or {}).get("title"), "recording_id": r.get("id"), "release_id": (rl or {}).get("id")}
        for c, r, rl in candidates[:5]
    ]
    return tags


def align_to_release(tag_list, auto_confidence):
    """Album download: put tracks that matched another release back on the album most tracks share."""
    sure = [t.get("release_id") for t in tag_list if t.get("release_id") and t["confidence"] >= auto_confidence]
    if len(sure) < 2:
        return
    best = max(set(sure), key=sure.count)
    if sure.count(best) < len(tag_list) / 2:
        return
    rel = _mb_get(f"release/{best}", inc="artist-credits+labels+recordings+release-groups+isrcs") or {}
    tracks = [tr for m in rel.get("media") or [] for tr in m.get("tracks") or []]
    model = next(t for t in tag_list if t.get("release_id") == best)
    for t in tag_list:
        if t.get("release_id") == best:
            continue
        match = max(tracks, key=lambda tr: _sim(t.get("title"), tr.get("title")), default=None)
        if not match or _sim(t.get("title"), match.get("title")) < 0.85:
            continue
        rec = match.get("recording") or {}
        t.update({k: v for k, v in release_details(best, rec.get("id")).items() if v is not None})
        t.update(title=match.get("title") or t["title"], recording_id=rec.get("id"),
                 confidence=max(t["confidence"], 0.9), cover=model.get("cover"), cover_source=model.get("cover_source"),
                 albumartist="Various Artists" if t.get("compilation") else t.get("albumartist"))
        if t.get("date"):
            t["year"] = t["date"][:4]
