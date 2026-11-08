"""Library cleanup (step 4 of docs/tags-plan.md).

A run looks at up to CLEANUP_MAX_FILES MP3s of a pCloud folder without changing anything (only the
ID3 tag at the start of each file is read), proposes fixes (tags, genre, cover, renaming/filing),
then applies the ones the user ticks: renaming/moving is done inside pCloud, tags are rewritten
without re-encoding the audio. Every change is journaled and a run can be undone.

Runs, journal and cover previews live in /data/cleanup (persistent volume).
"""
import io
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from mutagen.id3 import ID3

from . import musicmeta, sources, tagging

DATA_DIR = os.getenv("CLEANUP_DIR", "/data/cleanup")
MAX_FILES = int(os.getenv("CLEANUP_MAX_FILES", "100"))
MAX_RUNS = 10
FIELDS = ("title", "artist", "albumartist", "album", "track", "total_tracks", "disc", "total_discs", "year", "label")
ID_FIELDS = ("release_id", "release_group_id", "recording_id", "artist_id", "albumartist_id", "track_id",
             "release_type", "discogs_release_id", "isrc", "date", "original_date", "compilation")
LABELS = {"title": "Titre", "artist": "Artiste", "albumartist": "Artiste de l'album", "album": "Album",
          "track": "Piste", "total_tracks": "Nb pistes", "disc": "Disque", "total_discs": "Nb disques",
          "year": "Année", "label": "Label", "genre": "Genre"}
BUSY = ("listing", "analyzing", "applying", "undoing")


def _name_hints(name):
    """'03 - Artist - Title.mp3' / '03 - Title.mp3' -> (track, artist, title)."""
    base = os.path.splitext(name)[0]
    track = None
    parts = [p.strip() for p in base.split(" - ")]
    if parts and parts[0].isdigit():
        track = int(parts.pop(0))
    if len(parts) >= 2:
        return track, parts[0], " - ".join(parts[1:])
    return track, None, parts[0] if parts else base


def _cover_info(data):
    if not data:
        return {"present": False}
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        return {"present": True, "w": w, "h": h, "square": abs(w - h) <= max(w, h) * 0.05, "small": max(w, h) < 300}
    except Exception:
        return {"present": True, "w": None, "h": None, "square": True, "small": False}


class Cleanup:
    def __init__(self, manager):
        self.m = manager  # JobManager: pcloud, library, review
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.runs = self._load("runs.json", {})
        self.state = self._load("state.json", {})  # fileid -> "modified" value, or "*": already handled
        for r in self.runs.values():
            if r.get("status") in BUSY:
                r.update(status="error", message="Interrompu par un redémarrage de l'application.")

    # ------------------------------------------------------------ storage
    @staticmethod
    def _path(*parts):
        return os.path.join(DATA_DIR, *parts)

    def _load(self, name, default):
        try:
            with open(self._path(name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def _save(self):
        with self.lock:
            runs, state = json.dumps(self.runs, ensure_ascii=False), json.dumps(self.state)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            for name, data in (("runs.json", runs), ("state.json", state)):
                with open(self._path(name + ".tmp"), "w", encoding="utf-8") as fh:
                    fh.write(data)
                os.replace(self._path(name + ".tmp"), self._path(name))
        except OSError:
            pass

    def _save_cover(self, run_id, idx, kind, data):
        if not data:
            return False
        os.makedirs(self._path(run_id), exist_ok=True)
        with open(self._path(run_id, f"{idx}-{kind}.jpg"), "wb") as fh:
            fh.write(data)
        return True

    def cover(self, run_id, idx, kind):
        kind = kind if kind in ("old", "new", "cur", "orig") else "old"
        try:
            with open(self._path(run_id, f"{int(idx)}-{kind}.jpg"), "rb") as fh:
                return fh.read()
        except (OSError, ValueError):
            return None

    def _update(self, run_id, **fields):
        with self.lock:
            self.runs[run_id].update(fields)

    # ------------------------------------------------------------- public
    def list(self):
        with self.lock:
            runs = sorted(self.runs.values(), key=lambda r: r["created"], reverse=True)
            return [{k: v for k, v in r.items() if k not in ("items", "journal")} for r in runs]

    def get(self, run_id):
        with self.lock:
            r = self.runs.get(run_id)
            return json.loads(json.dumps(r)) if r else None

    def busy(self):
        with self.lock:
            return any(r["status"] in BUSY for r in self.runs.values())

    def start(self, folder, root, options, recheck=False):
        if self.busy():
            raise RuntimeError("Un nettoyage est déjà en cours.")
        if not any(options.get(k) for k in ("tags", "genre", "covers", "rename")):
            raise RuntimeError("Choisis au moins une correction.")
        run = {"id": uuid.uuid4().hex[:10], "created": time.time(), "folder": "/" + folder.strip("/"),
               "root": "/" + (root or folder).strip("/"), "options": options, "recheck": bool(recheck),
               "status": "listing", "message": "Liste des fichiers…", "progress": 0, "found": 0, "skipped": 0,
               "items": [], "journal": []}
        with self.lock:
            self.runs[run["id"]] = run
            for old in sorted(self.runs.values(), key=lambda r: r["created"])[:-MAX_RUNS]:
                if old["status"] not in BUSY:
                    self.runs.pop(old["id"], None)
                    shutil.rmtree(self._path(old["id"]), ignore_errors=True)
        self._save()
        self.pool.submit(self._analyze, run["id"])
        return run

    def delete(self, run_id):
        with self.lock:
            r = self.runs.get(run_id)
            if not r or r["status"] in BUSY:
                raise RuntimeError("Nettoyage introuvable ou en cours.")
            self.runs.pop(run_id)
        shutil.rmtree(self._path(run_id), ignore_errors=True)
        self._save()

    # ------------------------------------------------------------ analyze
    def _analyze(self, run_id):
        try:
            run = self.get(run_id)
            opts = run["options"]
            files = self.m.pcloud.list_files(run["folder"])
            pending = [f for f in files if run["recheck"] or self.state.get(str(f["fileid"])) not in (f["modified"], "*")]
            self._update(run_id, found=len(files), skipped=len(files) - len(pending))
            pending = pending[:MAX_FILES]
            if not pending:
                self._update(run_id, status="ready", message="Rien à analyser : tout est déjà propre.", progress=100)
                self._save()
                return

            # 1. read the current tags (only the start of each file)
            items = []
            for i, f in enumerate(pending):
                self._update(run_id, status="analyzing", progress=round(i / len(pending) * 40),
                             message=f"Lecture des tags ({i + 1}/{len(pending)}) : {f['name']}")
                try:
                    id3 = ID3(fileobj=io.BytesIO(self.m.pcloud.read_head(f["fileid"])))
                    cur = tagging.read_tags(id3)
                except Exception:
                    cur = {k: None for k in FIELDS + ID_FIELDS + ("genre", "url", "cover")}
                cover = cur.pop("cover", None)
                if cover:
                    self._save_cover(run_id, i, "cur", cover)
                    self._save_cover(run_id, i, "old", musicmeta._to_jpeg(cover) if len(cover) > 200_000 else cover)
                track, n_artist, n_title = _name_hints(f["name"])
                items.append({"idx": i, "fileid": f["fileid"], "path": f["path"], "name": f["name"],
                              "modified": f["modified"], "current": cur, "cover_info": _cover_info(cover),
                              "name_hints": {"track": track, "artist": n_artist, "title": n_title}})

            # 2. identify, once per album when possible
            lib = self.m.library
            auto = self.m.auto_confidence

            def needs(it):
                c, ci = it["current"], it["cover_info"]
                return {
                    "tags": bool(opts.get("tags")) and (not all(c.get(k) for k in ("title", "artist", "album", "track", "year"))
                                                         or not (c.get("release_id") or c.get("discogs_release_id"))),
                    "genre": bool(opts.get("genre")) and (not c.get("genre") or lib._find(c["genre"]) != c["genre"]),
                    "covers": bool(opts.get("covers")) and (not ci["present"] or not ci.get("square") or ci.get("small")),
                }

            groups = {}
            for it in items:
                c = it["current"]
                key = ((c.get("albumartist") or c.get("artist") or "").lower(), (c.get("album") or "").lower())
                groups.setdefault(key if key[1] else ("", it["path"]), []).append(it)
            done = 0
            for key, group in groups.items():
                base = None
                for it in group:
                    done += 1
                    self._update(run_id, progress=40 + round(done / len(items) * 50),
                                 message=f"Identification ({done}/{len(items)}) : {it['name']}")
                    need = needs(it)
                    it["needs"] = need
                    if not any(need.values()):
                        it["ident"] = None
                        continue
                    c, nh = it["current"], it["name_hints"]
                    title = c.get("title") or nh["title"]
                    t = None
                    if base and not (base.get("release_id") or base.get("discogs_release_id")) and key[1]:
                        # The album's first track is in no database: the others won't be either.
                        # Keep their own tags, share the genre / cover found for the album.
                        t = {"confidence": base["confidence"], "source": base.get("source"), "genre": base.get("genre"),
                             "cover": base.get("cover"), "cover_source": base.get("cover_source"),
                             "unmapped_genres": base.get("unmapped_genres")}
                    elif base and base["confidence"] >= 0.6:
                        try:
                            t = musicmeta.track_in_release(base, title)
                        except Exception:
                            t = None
                        if t:
                            t.update(genre=base.get("genre"), cover=base.get("cover"), cover_source=base.get("cover_source"),
                                     candidates=base.get("candidates"), hints=base.get("hints"))
                    if not t:
                        info = {"title": title, "track": title if (c.get("artist") or nh["artist"]) else None,
                                "artist": c.get("artist") or nh["artist"], "album": c.get("album"),
                                "duration": None, "webpage_url": c.get("url"), "genre": c.get("genre")}
                        try:
                            t = musicmeta.identify(info, lib, path=None, auto_confidence=auto)
                        except Exception as e:
                            sources.log.warning("cleanup identify: %s", e)
                            t = None
                        if t and not base:
                            base = t
                        if t:
                            lib.record_unmapped(t.get("unmapped_genres") or [], t.get("artist"), t.get("genre_consensus") or [])
                    it["ident"] = t

            # 3. proposals
            if opts.get("tags"):
                for group in groups.values():
                    titles = [(it["current"].get("title") or "") for it in group]
                    if len(group) >= 2 and all(" - " in t for t in titles):
                        prefixes = {t.split(" - ", 1)[0] for t in titles}
                        if len(prefixes) == 1:
                            prefix = next(iter(prefixes))
                            for it in group:
                                it["strip_prefix"] = prefix
            for it in items:
                self._propose(run_id, run, it)
                it.pop("ident", None)
            # 4. conflicts: two files aiming at one path, or a path already taken in pCloud
            targets = {}
            for it in items:
                if it.get("new_path") and it["new_path"] != it["path"]:
                    targets.setdefault(it["new_path"].lower(), []).append(it)
            for path, its in targets.items():
                if len(its) > 1:
                    for it in its:
                        others = ", ".join(o["path"] for o in its if o is not it)
                        it.update(conflict=f"doublon probable : même destination que {others}", checked=False)
                elif self.m.pcloud.path_exists(its[0]["new_path"]):
                    its[0].update(conflict="un fichier existe déjà à cet endroit", checked=False)
            clean = [it for it in items if not it["has_changes"]]
            with self.lock:
                for it in clean:
                    self.state[str(it["fileid"])] = it["modified"]
                self.runs[run_id].update(items=[it for it in items if it["has_changes"]], clean=len(clean),
                                         status="ready", progress=100,
                                         message=f"{len(items) - len(clean)} fichier(s) à corriger, {len(clean)} déjà propre(s).")
            self._save()
        except Exception as e:
            sources.log.warning("cleanup: %s", e)
            self._update(run_id, status="error", message=f"Erreur : {e}")
            self._save()

    def _propose(self, run_id, run, it):
        opts, lib, auto = run["options"], self.m.library, self.m.auto_confidence
        cur, t, ci = it["current"], it.get("ident"), it["cover_info"]
        need = it.get("needs") or {}
        conf = t["confidence"] if t else None
        final = dict(cur)
        changes, from_ident, ids_changed = {}, False, False

        if need.get("tags") and t and conf >= 0.6:
            single_disc = (t.get("total_discs") or 1) <= 1
            for k in FIELDS + ID_FIELDS:
                new = t.get(k)
                if new in (None, "", False) or str(new) == str(cur.get(k) or ""):
                    continue
                if single_disc and k in ("disc", "total_discs"):
                    continue  # not written for single-disc albums: not a real change
                final[k] = new
                if k in FIELDS:
                    changes[k] = [cur.get(k), new]
                    from_ident = True
                else:
                    ids_changed = True
        if it.get("strip_prefix") and (final.get("title") or "").startswith(it["strip_prefix"] + " - "):
            stripped = final["title"][len(it["strip_prefix"]) + 3:].strip()
            if stripped:
                changes["title"] = [cur.get("title"), stripped]
                final["title"] = stripped
        if opts.get("tags") and not final.get("albumartist") and final.get("artist"):
            final["albumartist"] = final["artist"]
            changes["albumartist"] = [cur.get("albumartist"), final["albumartist"]]
        if opts.get("tags") and not final.get("title") and it["name_hints"]["title"]:
            final["title"] = it["name_hints"]["title"]
            changes["title"] = [cur.get("title"), final["title"]]

        if need.get("genre"):
            artist = final.get("albumartist") or final.get("artist")
            listed = lib._find(cur.get("genre")) if cur.get("genre") else None
            proposal = (listed or lib.artist_genre(artist) or lib.map_genre(cur.get("genre") or "")
                        or (t.get("genre") if t and conf and conf >= 0.6 else None))
            if proposal and proposal != cur.get("genre"):
                final["genre"] = proposal
                changes["genre"] = [cur.get("genre"), proposal]
                from_ident = from_ident or (proposal == (t or {}).get("genre") and not listed)

        cover_change, cover_source = False, None
        if need.get("covers"):
            new = t.get("cover") if t and conf and conf >= 0.6 and t.get("cover") else None
            if new and t.get("cover_source") not in (None, "miniature"):
                cover_source, from_ident = t.get("cover_source"), True
            elif ci["present"] and not ci.get("square"):
                cur_cover = self.cover(run_id, it["idx"], "cur")
                new, cover_source = (musicmeta.thumbnail_cover_bytes(cur_cover), "recadrée") if cur_cover else (None, None)
            else:
                new = None
            if new:
                cover_change = self._save_cover(run_id, it["idx"], "new", new)

        new_path = None
        if opts.get("rename") and (final.get("artist") or final.get("albumartist")) and final.get("title"):
            folder, name = tagging.target_path(final, run["root"], True)
            new_path = f"{folder}/{name}"
            if new_path == it["path"]:
                new_path = None

        has_changes = bool(changes or cover_change or new_path or ids_changed)
        it["album_key"] = f"{(final.get('albumartist') or final.get('artist') or '').lower()}|{(final.get('album') or '').lower()}"
        it.update(final=final, changes=changes, ids_changed=ids_changed, cover_change=cover_change,
                  cover_source=cover_source, new_path=new_path, confidence=conf,
                  source=(t or {}).get("source"), has_changes=has_changes, conflict=None, status="pending",
                  candidates=(t or {}).get("candidates") or [], hints=(t or {}).get("hints") or {},
                  checked=has_changes and (not from_ident or (conf or 0) >= auto))

    # -------------------------------------------------------------- apply
    def apply(self, run_id, selected):
        run = self.get(run_id)
        if not run or run["status"] != "ready":
            raise RuntimeError("Ce nettoyage n'est pas prêt à être appliqué.")
        self._update(run_id, status="applying", message="Application…", progress=0)
        self._save()
        self.pool.submit(self._apply, run_id, {int(i) for i in selected})

    def _apply(self, run_id, selected):
        run = self.get(run_id)
        items = run["items"]
        todo = [it for it in items if it["idx"] in selected and it["status"] == "pending"]
        n_ok = n_err = 0
        for k, it in enumerate(todo, 1):
            self._update(run_id, progress=round((k - 1) / max(len(todo), 1) * 100),
                         message=f"Application ({k}/{len(todo)}) : {it['name']}")
            try:
                entry = self._apply_item(run_id, run, it)
                it.update(status="applied", result_path=entry["new_path"])
                with self.lock:
                    self.runs[run_id]["journal"].append(entry)
                    self.state[str(entry["new_fileid"])] = "*"
                n_ok += 1
            except Exception as e:
                it.update(status="error", error=str(e)[:300])
                n_err += 1
            with self.lock:
                self.runs[run_id]["items"] = items
            self._save()
        with self.lock:
            for it in items:  # left unticked: don't propose them again next time
                if it["status"] == "pending":
                    it["status"] = "skipped"
                    self.state[str(it["fileid"])] = it["modified"]
            self.runs[run_id].update(items=items, status="done", progress=100,
                                     message=f"{n_ok} fichier(s) corrigé(s)" + (f", {n_err} erreur(s)" if n_err else ""))
        self._save()

    def _apply_item(self, run_id, run, it):
        pc = self.m.pcloud
        old_path = it["path"]
        new_path = it.get("new_path") or old_path
        folder, _, name = new_path.rpartition("/")
        rewrite = bool(it["changes"] or it["cover_change"] or it["ids_changed"])
        entry = {"idx": it["idx"], "old_path": old_path, "old_fileid": it["fileid"], "new_path": new_path}
        if not rewrite:
            res = pc.move(it["fileid"], folder, name)
            entry.update(mode="move", new_fileid=res.get("fileid") or it["fileid"], new_path=res["path"])
            return entry
        work = tempfile.mkdtemp(prefix=f"cl-{run_id}-", dir=os.getenv("WORK_DIR", tempfile.gettempdir()))
        try:
            local = os.path.join(work, name)
            pc.download(it["fileid"], local)
            try:
                old = tagging.read_tags(ID3(local))
            except Exception:
                old = {}
            old_cover = old.pop("cover", None)
            self._save_cover(run_id, it["idx"], "orig", old_cover)
            cover = self.cover(run_id, it["idx"], "new") if it["cover_change"] else old_cover
            tagging.write_tags(local, dict(it["final"], cover=cover))
            same = new_path == old_path
            res = pc.upload(local, folder, overwrite=same)
            if not same:
                pc.delete(it["fileid"])
        finally:
            shutil.rmtree(work, ignore_errors=True)
        entry.update(mode="rewrite", new_fileid=res.get("fileid"), new_path=res["path"], old_tags=old,
                     had_cover=bool(old_cover))
        return entry

    # --------------------------------------------------------------- undo
    def undo(self, run_id):
        run = self.get(run_id)
        if not run or run["status"] != "done" or not run["journal"]:
            raise RuntimeError("Rien à annuler pour ce nettoyage.")
        self._update(run_id, status="undoing", message="Annulation…", progress=0)
        self._save()
        self.pool.submit(self._undo, run_id)

    def _undo(self, run_id):
        run = self.get(run_id)
        pc, journal, errors = self.m.pcloud, run["journal"], []
        for k, e in enumerate(reversed(journal), 1):
            self._update(run_id, progress=round(k / len(journal) * 100), message=f"Annulation ({k}/{len(journal)})")
            folder, _, name = e["old_path"].rpartition("/")
            try:
                if e["mode"] == "move":
                    pc.move(e["new_fileid"], folder, name)
                else:
                    work = tempfile.mkdtemp(prefix=f"un-{run_id}-", dir=os.getenv("WORK_DIR", tempfile.gettempdir()))
                    try:
                        local = os.path.join(work, name)
                        pc.download(e["new_fileid"], local)
                        old_cover = self.cover(run_id, e["idx"], "orig") if e.get("had_cover") else None
                        tagging.write_tags(local, dict(e.get("old_tags") or {}, cover=old_cover))
                        same = e["old_path"] == e["new_path"]
                        pc.upload(local, folder, overwrite=same)
                        if not same:
                            pc.delete(e["new_fileid"])
                    finally:
                        shutil.rmtree(work, ignore_errors=True)
                with self.lock:
                    self.state.pop(str(e["new_fileid"]), None)
            except Exception as ex:
                errors.append(f"{e['old_path']} : {ex}")
        self._update(run_id, status="undone" if not errors else "error",
                     message="Nettoyage annulé." if not errors else "Annulation incomplète : " + " ; ".join(errors[:3]),
                     progress=100)
        self._save()

    def set_genre(self, run_id, idxs, genre):
        """Album-wide genre chosen on the report: becomes the proposed genre of those files."""
        genre = self.m.library._find(genre) if genre else None
        if not genre:
            raise RuntimeError("Genre inconnu.")
        idxs = {int(i) for i in idxs}
        with self.lock:
            run = self.runs.get(run_id)
            if not run or run["status"] != "ready":
                raise RuntimeError("Ce nettoyage n'est plus modifiable.")
            for it in run["items"]:
                if it["idx"] in idxs and it["status"] == "pending":
                    it["final"]["genre"] = genre
                    if genre != it["current"].get("genre"):
                        it["changes"]["genre"] = [it["current"].get("genre"), genre]
                    else:
                        it["changes"].pop("genre", None)
                    it["checked"] = not it.get("conflict")
        self._save()
        return genre

    def send_many_to_review(self, run_id, idxs):
        return [self.send_to_review(run_id, i) for i in idxs]

    # ----------------------------------------------- send to the review page
    def send_to_review(self, run_id, idx):
        run = self.get(run_id)
        it = next((i for i in run["items"] if i["idx"] == int(idx)), None) if run else None
        if not it or it["status"] != "pending":
            raise RuntimeError("Fichier introuvable ou déjà traité.")
        cover = self.cover(run_id, it["idx"], "new") or self.cover(run_id, it["idx"], "cur")
        tags = dict(it["final"], cover=cover, cover_source=it.get("cover_source") or "actuelle",
                    confidence=it.get("confidence") or 0, source=it.get("source") or "tags existants",
                    candidates=it.get("candidates") or [], hints=it.get("hints") or {"title": it["name"]})
        key = f"cleanup|{run_id}|{(it['final'].get('albumartist') or '').lower()}|{(it['final'].get('album') or '').lower()}"
        item = self.m.review.add(tags, {"fileid": it["fileid"], "path": it["path"]}, reason="nettoyage",
                                 root=run["root"], group=key)
        self.m.review.update(item["id"], cleanup_run=run_id, cleanup_idx=it["idx"])
        with self.lock:
            for i in self.runs[run_id]["items"]:
                if i["idx"] == it["idx"]:
                    i.update(status="review", checked=False)
        self._save()
        return item["id"]

    def on_validated(self, item, res):
        """A cleanup file was validated on the review page."""
        run_id, idx = item.get("cleanup_run"), item.get("cleanup_idx")
        with self.lock:
            run = self.runs.get(run_id)
            for i in (run or {}).get("items", []):
                if i["idx"] == idx:
                    i.update(status="applied", result_path=res["path"])
            if res.get("fileid"):
                self.state[str(res["fileid"])] = "*"
        self._save()
