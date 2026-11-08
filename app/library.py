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
    return {"genres": genres, "genre_map": mapping, "artist_genres": {}, "ignored_genres": [], "genre_seen": {},
            "removed_genres": []}


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
        # Genres added to the repository's genres.txt reach existing installs too, unless the user
        # removed them from the site.
        known = {g.lower() for g in data["genres"]} | {g.lower() for g in data["removed_genres"]}
        data["genres"] += [g for g in seed["genres"] if g.lower() not in known]
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

    # ------------------------------------------------------ genre suggestions
    def _known(self, name):
        key = name.strip().lower()
        return (any(g.lower() == key for g in self.data["genres"]) or key in self.data["genre_map"]
                or key in self.data["ignored_genres"])

    def record_unmapped(self, names, artist, consensus=()):
        """Count genres found for a track that are not in the list (for suggestions)."""
        changed = False
        for name in names:
            key = name.strip().lower()
            if not key or self._known(key):
                continue
            seen = self.data["genre_seen"].setdefault(key, {"count": 0, "artists": [], "consensus": False})
            seen["count"] += 1
            a = self._artist_key(artist)
            if a and a not in seen["artists"]:
                seen["artists"] = (seen["artists"] + [a])[-10:]
            if key in consensus:
                seen["consensus"] = True
            changed = True
        if changed:
            self.save()

    def suggestions(self):
        """Genres worth adding: several sources agreed on one track, or seen on 3+ tracks of 2+ artists."""
        out = []
        for key, seen in self.data["genre_seen"].items():
            if self._known(key):
                continue
            if seen.get("consensus") or (seen["count"] >= 3 and len(seen["artists"]) >= 2):
                out.append({"name": key, "count": seen["count"], "artists": len(seen["artists"]),
                            "consensus": bool(seen.get("consensus"))})
        return sorted(out, key=lambda x: (-x["count"], x["name"]))

    def add_genre(self, name):
        name = " ".join(name.split())
        if name and not any(g.lower() == name.lower() for g in self.data["genres"]):
            self.data["genres"].append(name)
        self.data["genre_seen"].pop(name.lower(), None)
        self.save()
        return name

    def map_genre_to(self, name, target):
        if target not in self.data["genres"]:
            raise ValueError("genre inconnu")
        self.data["genre_map"][name.strip().lower()] = target
        self.data["genre_seen"].pop(name.strip().lower(), None)
        self.save()

    def ignore_genre(self, name):
        key = name.strip().lower()
        if key not in self.data["ignored_genres"]:
            self.data["ignored_genres"].append(key)
        self.data["genre_seen"].pop(key, None)
        self.save()

    # -------------------------------------------------------- list management
    def _find(self, name):
        key = (name or "").strip().lower()
        return next((g for g in self.data["genres"] if g.lower() == key), None)

    def rename_genre(self, old, new):
        old, new = self._find(old), " ".join((new or "").split())
        if not old or not new:
            raise ValueError("genre introuvable ou nom vide")
        if self._find(new) and self._find(new) != old:
            raise ValueError(f"« {new} » existe déjà")
        self.data["genres"] = [new if g == old else g for g in self.data["genres"]]
        for table in ("genre_map", "artist_genres"):
            self.data[table] = {k: (new if v == old else v) for k, v in self.data[table].items()}
        self.save()

    def delete_genre(self, name):
        g = self._find(name)
        if not g:
            raise ValueError("genre introuvable")
        self.data["genres"].remove(g)
        self.data["removed_genres"] = sorted(set(self.data["removed_genres"]) | {g})
        for table in ("genre_map", "artist_genres"):
            self.data[table] = {k: v for k, v in self.data[table].items() if v != g}
        self.save()

    def add_genre_manual(self, name):
        name = " ".join((name or "").split())
        if not name:
            raise ValueError("nom vide")
        existing = self._find(name)
        if existing:
            return existing
        self.data["genres"].append(name)
        self.data["removed_genres"] = [g for g in self.data["removed_genres"] if g.lower() != name.lower()]
        self.data["genre_seen"].pop(name.lower(), None)
        self.save()
        return name

    def set_mapping(self, source, target):
        target = self._find(target)
        if not source or not target:
            raise ValueError("correspondance invalide")
        self.data["genre_map"][source.strip().lower()] = target
        self.save()

    def delete_mapping(self, source):
        self.data["genre_map"].pop((source or "").strip().lower(), None)
        self.save()

    def set_artist(self, artist, genre):
        genre = self._find(genre)
        if not artist or not genre:
            raise ValueError("artiste ou genre invalide")
        self.data["artist_genres"][self._artist_key(artist)] = genre
        self.save()

    def delete_artist(self, artist):
        self.data["artist_genres"].pop(self._artist_key(artist), None)
        self.save()
