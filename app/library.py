"""Persistent music-library settings: genre list, genre mappings, artist memory.

Seeded from app/data/*.txt on first start, then kept in /data/library.json (persistent volume)
so it can later be edited from the site without touching the code.
"""
import json
import os
import threading

DATA_FILE = os.getenv("LIBRARY_FILE", "/data/library.json")
SEED_DIR = os.path.join(os.path.dirname(__file__), "data")


def _read_lines(name):
    try:
        with open(os.path.join(SEED_DIR, name), encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return []


def _seed():
    genres = _read_lines("genres.txt")
    mapping = {}
    for ln in _read_lines("genre_map.txt"):
        if "=" in ln:
            src, dst = (p.strip() for p in ln.split("=", 1))
            if src and dst:
                mapping[src.lower()] = dst
    return {"genres": genres, "genre_map": mapping, "artist_genres": {}, "ignored_genres": []}


class Library:
    def __init__(self, path=DATA_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        seed = _seed()
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        for key, value in seed.items():
            data.setdefault(key, value)
        # New entries added to the repository's mapping file reach existing installs too,
        # without overriding choices already saved.
        for src, dst in seed["genre_map"].items():
            data["genre_map"].setdefault(src, dst)
        return data

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except OSError:
                pass  # read-only or missing volume: keep working from memory

    # ------------------------------------------------------------------ genres
    @property
    def genres(self):
        return list(self.data["genres"])

    def map_genre(self, name):
        """Return the genre from the user's list matching a database genre/tag, or None."""
        if not name:
            return None
        key = name.strip().lower()
        for g in self.data["genres"]:
            if g.lower() == key:
                return g
        dst = self.data["genre_map"].get(key)
        if dst and dst in self.data["genres"]:
            return dst
        return None

    # ----------------------------------------------------------- artist memory
    @staticmethod
    def _artist_key(artist):
        return " ".join((artist or "").lower().split())

    def artist_genre(self, artist):
        return self.data["artist_genres"].get(self._artist_key(artist))

    def remember_artist_genre(self, artist, genre):
        key = self._artist_key(artist)
        if key and genre in self.data["genres"]:
            self.data["artist_genres"][key] = genre
            self.save()
