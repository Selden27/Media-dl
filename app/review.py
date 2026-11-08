"""Tracks waiting for validation (filed in "_A trier"). Kept on the persistent volume.

/data/review.json holds the items; /data/review/<id>/<name>.jpg holds the cover choices.
"""
import json
import os
import shutil
import threading
import time
import uuid

DATA_FILE = os.getenv("REVIEW_FILE", "/data/review.json")
COVER_DIR = os.getenv("REVIEW_COVER_DIR", "/data/review")
EDITABLE = ("title", "artist", "albumartist", "album", "track", "total_tracks", "disc", "total_discs", "year",
            "genre", "label")


class ReviewStore:
    def __init__(self, path=DATA_FILE, cover_dir=COVER_DIR):
        self.path, self.cover_dir = path, cover_dir
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as fh:
                self.items = json.load(fh)
        except (OSError, ValueError):
            self.items = {}

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ----------------------------------------------------------------- covers
    def cover_path(self, item_id, name):
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        return os.path.join(self.cover_dir, item_id, f"{safe}.jpg")

    def save_cover(self, item_id, name, data):
        if not data:
            return None
        path = self.cover_path(item_id, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return name

    def read_cover(self, item_id, name):
        try:
            with open(self.cover_path(item_id, name), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    # ------------------------------------------------------------------ items
    def add(self, tags, upload, thumb_cover=None, reason="à trier", root=None, job_id=None, file_index=None,
            group=None):
        """`reason`: "à trier" (low confidence, file in _A trier), "genre manquant" or "modification"
        (file already at its final place: validating rewrites it in place)."""
        item_id = uuid.uuid4().hex[:12]
        covers = []
        if tags.get("cover"):
            covers.append({"name": "actuelle", "label": f"Actuelle ({tags.get('cover_source') or '?'})"})
            self.save_cover(item_id, "actuelle", tags["cover"])
        if thumb_cover and thumb_cover != tags.get("cover"):
            covers.append({"name": "miniature", "label": "Miniature de la vidéo"})
            self.save_cover(item_id, "miniature", thumb_cover)
        item = {
            "id": item_id, "created": time.time(), "fileid": upload.get("fileid"), "path": upload.get("path"),
            "tags": {k: v for k, v in tags.items() if k not in ("cover", "candidates", "hints", "_thumb_cover")},
            "candidates": tags.get("candidates") or [], "hints": tags.get("hints") or {},
            "covers": covers, "cover": covers[0]["name"] if covers else None,
            "reason": reason, "root": root, "job_id": job_id, "file_index": file_index,
            "group": group or item_id,
        }
        with self.lock:
            self.items[item_id] = item
            self._save()
        return item

    def list(self):
        with self.lock:
            return sorted((dict(i) for i in self.items.values()), key=lambda i: i["created"])

    def get(self, item_id):
        with self.lock:
            item = self.items.get(item_id)
            return json.loads(json.dumps(item)) if item else None

    def update(self, item_id, **fields):
        with self.lock:
            if item_id in self.items:
                self.items[item_id].update(fields)
                self._save()

    def remove(self, item_id):
        with self.lock:
            self.items.pop(item_id, None)
            self._save()
        shutil.rmtree(os.path.join(self.cover_dir, item_id), ignore_errors=True)

    def count(self):
        with self.lock:
            return len(self.items)

    def find(self, job_id, file_index):
        with self.lock:
            return next((i["id"] for i in self.items.values()
                         if i.get("job_id") == job_id and i.get("file_index") == file_index), None)
