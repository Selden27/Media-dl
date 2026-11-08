"""Write ID3v2.3 tags into an MP3 and build its pCloud path from a template."""
import os
import re

from mutagen.id3 import (APIC, COMM, ID3, TALB, TCMP, TCON, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK,
                         TSRC, TXXX, UFID, WOAS, ID3NoHeaderError)

PATH_TEMPLATE = os.getenv("MUSIC_PATH_TEMPLATE", "%albumartist%/%album%/$num(%track%,2) - %title%")
SINGLES_FOLDER = os.getenv("MUSIC_SINGLES_FOLDER", "Singles")
TO_SORT_FOLDER = os.getenv("MUSIC_TO_SORT_FOLDER", "_A trier")
UNKNOWN_ARTIST = "Artiste inconnu"


def write_tags(path, t):
    """Replace every tag in `path` with the ones in `t` (dict from musicmeta.identify)."""
    try:
        id3 = ID3(path)
        id3.delete(path)
    except ID3NoHeaderError:
        pass
    id3 = ID3()

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
    for desc, key in (("MusicBrainz Album Id", "release_id"), ("MusicBrainz Artist Id", "artist_id"),
                      ("MusicBrainz Album Artist Id", "albumartist_id"),
                      ("MusicBrainz Release Group Id", "release_group_id"),
                      ("MusicBrainz Release Track Id", "track_id"), ("MusicBrainz Album Type", "release_type")):
        if t.get(key):
            id3.add(TXXX(encoding=3, desc=desc, text=str(t[key]).lower() if key == "release_type" else str(t[key])))
    if t.get("recording_id"):
        id3.add(UFID(owner="http://musicbrainz.org", data=t["recording_id"].encode()))
    if t.get("url"):
        id3.add(WOAS(url=t["url"]))
        id3.add(COMM(encoding=3, lang="fra", desc="", text=f"Source : {t['url']}"))
    if t.get("cover"):
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=t["cover"]))
    id3.save(path, v2_version=3)


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
