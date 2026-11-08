"""Write ID3v2.3 tags into an MP3 and build its pCloud path from a template."""
import os
import re

from mutagen.id3 import (APIC, COMM, ID3, TALB, TCMP, TCON, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK,
                         TSRC, TXXX, UFID, WOAS, ID3NoHeaderError)

PATH_TEMPLATE = os.getenv("MUSIC_PATH_TEMPLATE", "%albumartist%/%album%/$num(%track%,2) - %title%")
SINGLES_FOLDER = os.getenv("MUSIC_SINGLES_FOLDER", "Singles")
TO_SORT_FOLDER = os.getenv("MUSIC_TO_SORT_FOLDER", "_A trier")
UNKNOWN_ARTIST = "Artiste inconnu"


TXXX_KEYS = (("MusicBrainz Album Id", "release_id"), ("MusicBrainz Artist Id", "artist_id"),
             ("MusicBrainz Album Artist Id", "albumartist_id"), ("MusicBrainz Release Group Id", "release_group_id"),
             ("MusicBrainz Release Track Id", "track_id"), ("MusicBrainz Album Type", "release_type"),
             ("DISCOGS_RELEASE_ID", "discogs_release_id"))
# Frames the app manages. Anything else (lyrics, personal comments, ratings, ReplayGain…) is kept.
MANAGED = ("TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TPOS", "TDRC", "TYER", "TDAT", "TIME", "TORY", "TDOR", "TCON",
           "TPUB", "TSRC", "TCMP", "WOAS", "APIC")


def write_tags(path, t):
    """Write the fields of `t` (dict from musicmeta.identify) into `path`, as ID3v2.3.

    Only the frames the app manages are replaced; a managed field missing from `t` is removed.
    Other frames already in the file are left untouched."""
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    for fid in MANAGED:
        id3.delall(fid)
    for desc, _ in TXXX_KEYS:
        id3.delall(f"TXXX:{desc}")
    for key in [k for k in id3.keys() if k.startswith("UFID:http://musicbrainz.org")]:
        del id3[key]
    for frame in list(id3.getall("COMM")):
        if str(frame).startswith("Source : "):
            id3.delall(frame.HashKey)

    def text(frame, value):
        if value not in (None, ""):
            id3.add(frame(encoding=3, text=str(value)))

    text(TIT2, t.get("title"))
    text(TPE1, t.get("artist"))
    text(TPE2, t.get("albumartist"))
    text(TALB, t.get("album"))
    if t.get("track"):
        text(TRCK, f"{t['track']}/{t['total_tracks']}" if t.get("total_tracks") else t["track"])
    if t.get("disc") and (t.get("total_discs") or 1) > 1:
        text(TPOS, f"{t['disc']}/{t['total_discs']}")
    text(TDRC, t.get("date") or t.get("year"))
    if t.get("original_date"):
        text(TDOR, t["original_date"])
    text(TCON, t.get("genre"))
    text(TPUB, t.get("label"))
    text(TSRC, t.get("isrc"))
    if t.get("compilation"):
        text(TCMP, "1")
    for desc, key in TXXX_KEYS:
        if t.get(key):
            id3.add(TXXX(encoding=3, desc=desc, text=str(t[key]).lower() if key == "release_type" else str(t[key])))
    if t.get("recording_id"):
        id3.add(UFID(owner="http://musicbrainz.org", data=str(t["recording_id"]).encode()))
    if t.get("url"):
        id3.add(WOAS(url=t["url"]))
        id3.add(COMM(encoding=3, lang="fra", desc="", text=f"Source : {t['url']}"))
    if t.get("cover"):
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=t["cover"]))
    id3.save(path, v2_version=3)


def read_tags(id3):
    """Current tags of a file (mutagen ID3 object) as a dict with the same keys as write_tags."""
    def first(fid):
        frame = id3.get(fid)
        return str(frame.text[0]).strip() if frame is not None and getattr(frame, "text", None) else None

    def pair(value):
        if not value:
            return None, None
        a, _, b = value.partition("/")
        return (int(a) if a.strip().isdigit() else None), (int(b) if b.strip().isdigit() else None)

    t = {"title": first("TIT2"), "artist": first("TPE1"), "albumartist": first("TPE2"), "album": first("TALB"),
         "genre": first("TCON"), "label": first("TPUB"), "isrc": first("TSRC"),
         "compilation": first("TCMP") == "1"}
    t["track"], t["total_tracks"] = pair(first("TRCK"))
    t["disc"], t["total_discs"] = pair(first("TPOS"))
    date = first("TDRC") or first("TYER")
    t["date"] = date
    t["year"] = date[:4] if date and date[:4].isdigit() else None
    t["original_date"] = first("TDOR") or first("TORY")
    for desc, key in TXXX_KEYS:
        t[key] = first(f"TXXX:{desc}")
    ufid = next((id3[k] for k in id3.keys() if k.startswith("UFID:http://musicbrainz.org")), None)
    t["recording_id"] = ufid.data.decode(errors="ignore") if ufid is not None else None
    woas = id3.get("WOAS")
    t["url"] = woas.url if woas is not None else None
    pics = id3.getall("APIC")
    pic = next((p for p in pics if p.type == 3), pics[0] if pics else None)
    t["cover"] = pic.data if pic is not None else None
    return t


# ---------------------------------------------------------------- paths
def _clean(segment, limit=120):
    s = str(segment or "")
    s = s.replace("/", "-").replace("\\", "-").replace(":", " -")
    s = re.sub(r'[*?"<>|\x00-\x1f]', "", s)
    s = " ".join(s.split())
    s = re.sub(r"^[\s\-–_.]+", "", s)  # leftover " - " when the track number is empty
    return s[:limit].rstrip(" .")


def render(template, fields):
    """Render a Mp3tag/foobar-like template: %field% and $num(%field%,n)."""
    def field(m):
        return str(fields.get(m.group(1).lower()) or "").replace("/", "∕")

    out = re.sub(r"%(\w+)%", field, template)

    def num(m):
        value, width = m.group(1).strip(), int(m.group(2))
        return value.zfill(width) if value.isdigit() else ""

    out = re.sub(r"\$num\(([^,()]*),\s*(\d+)\)", num, out)
    segments = [_clean(seg.replace("∕", "-")) for seg in out.split("/")]
    return "/".join(s for s in segments if s)


def target_path(t, root, auto_ok):
    """Return (folder, filename) under `root` for a tagged track."""
    root = "/" + root.strip("/")
    artist = t.get("albumartist") or t.get("artist") or UNKNOWN_ARTIST
    title = t.get("title") or "Sans titre"
    if not auto_ok:
        name = _clean(f"{t.get('artist')} - {title}" if t.get("artist") else title)
        return f"{root}/{TO_SORT_FOLDER}", f"{name}.mp3"
    fields = dict(t, albumartist=artist, title=title, track=t.get("track") or "",
                  disc=t.get("disc") or "", year=t.get("year") or "", genre=t.get("genre") or "")
    if not t.get("album") or t.get("release_type") == "Single":
        fields.update(album=SINGLES_FOLDER, track="")
    rel = render(PATH_TEMPLATE, fields)
    folder, _, name = rel.rpartition("/")
    return (f"{root}/{folder}" if folder else root), f"{name or _clean(title)}.mp3"
