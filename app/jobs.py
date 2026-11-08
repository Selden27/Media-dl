"""Download jobs: yt-dlp download, then upload to pCloud."""
import os
import re
import shutil
import tempfile
import hashlib
import io
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from yt_dlp.networking.impersonate import ImpersonateTarget # Ajout ***Olivier***

from . import musicmeta, sources, tagging
from .library import Library
from .review import EDITABLE, ReviewStore
from .pcloud import PCloud

AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "mp3")          # mp3, m4a, opus, flac...
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "0")          # 0 = best VBR for mp3, or e.g. 192
VIDEO_MAX_HEIGHT = os.getenv("VIDEO_MAX_HEIGHT", "")     # e.g. 1080, empty = best
AUDIO_FOLDER = os.getenv("PCLOUD_AUDIO_FOLDER", "/Music")
VIDEO_FOLDER = os.getenv("PCLOUD_VIDEO_FOLDER", "/Videos")
WORK_DIR = os.getenv("WORK_DIR", tempfile.gettempdir())
MAX_JOBS_KEPT = int(os.getenv("MAX_JOBS_KEPT", "50"))
JOBS_FILE = os.getenv("JOBS_FILE", "/data/jobs.json")      # download history (survives redeploys)
JOBS_COVER_DIR = os.getenv("JOBS_COVER_DIR", "/data/jobs")  # one small cover per album, for the summaries
COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "")
PROXY = os.getenv("YTDLP_PROXY", "")
MUSIC_TAGGING = os.getenv("MUSIC_TAGGING", "true").lower() == "true"   # identify + tag + file MP3s
AUTO_CONFIDENCE = float(os.getenv("MUSIC_AUTO_CONFIDENCE", "0.85"))    # below: goes to "_A trier"  # e.g. socks5h://warp:1080 (used for downloads only, not pCloud)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
SKIP_EXT = IMAGE_EXT | {".part", ".ytdl", ".json", ".vtt", ".srt"}


def _safe_folder(name):
    name = re.sub(r'[\\/:*?"<>|]+', " ", name or "").strip()
    return name[:120] or "Playlist"


class JobManager:
    def __init__(self):
        self.jobs = self._load_jobs()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=int(os.getenv("MAX_CONCURRENT", "2")))
        self.pcloud = PCloud.from_env()
        self.library = Library()
        self.review = ReviewStore()
        threading.Thread(target=self._self_test, daemon=True).start()

    @staticmethod
    def _load_jobs():
        try:
            with open(JOBS_FILE, encoding="utf-8") as fh:
                jobs = json.load(fh)
        except (OSError, ValueError):
            return {}
        for j in jobs.values():  # a restart stopped whatever was running
            if j.get("status") not in ("done", "error"):
                j.update(status="error", error="Interrompu par un redémarrage de l'application.", message="Erreur")
        return jobs

    def _save_jobs(self):
        with self.lock:
            data = json.dumps(self.jobs, ensure_ascii=False)
        try:
            os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
            with open(JOBS_FILE + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(JOBS_FILE + ".tmp", JOBS_FILE)
        except OSError:
            pass

    @staticmethod
    def _save_job_cover(job_id, data):
        """Keep a small copy of a cover for the summary card; returns its key (hash)."""
        if not data:
            return None
        key = hashlib.sha1(data).hexdigest()[:16]
        path = os.path.join(JOBS_COVER_DIR, job_id, f"{key}.jpg")
        if not os.path.exists(path):
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(data)).convert("RGB")
                img.thumbnail((300, 300))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                img.save(path, "JPEG", quality=85)
                # full-size copy, used as the "current cover" when the track is edited later
                with open(os.path.join(JOBS_COVER_DIR, job_id, f"{key}-full.jpg"), "wb") as fh:
                    fh.write(data)
            except Exception:
                return None
        return key

    @staticmethod
    def job_cover(job_id, key, full=False):
        safe = "".join(c for c in key if c.isalnum())
        try:
            with open(os.path.join(JOBS_COVER_DIR, job_id, f"{safe}{'-full' if full else ''}.jpg"), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    @staticmethod
    def _drop_job_files(job_id):
        shutil.rmtree(os.path.join(JOBS_COVER_DIR, job_id), ignore_errors=True)

    @staticmethod
    def _self_test():
        try:
            sources.self_test()
        except Exception as e:
            sources.log.warning("sources de tags : test impossible (%s)", e)

    # -------------------------------------------------------------- public
    def submit(self, url, mode="audio", playlist=False, folder=None):
        mode = "video" if mode == "video" else "audio"
        folder = (folder or "").strip() or (VIDEO_FOLDER if mode == "video" else AUDIO_FOLDER)
        job = {
            "id": uuid.uuid4().hex[:12],
            "url": url,
            "mode": mode,
            "playlist": bool(playlist),
            "folder": "/" + folder.strip("/"),
            "status": "queued",
            "progress": 0.0,
            "title": None,
            "message": "En attente",
            "files": [],
            "error": None,
            "created": time.time(),
            "finished": None,
        }
        with self.lock:
            self.jobs[job["id"]] = job
            self._trim()
        self._save_jobs()
        self.pool.submit(self._run, job["id"])
        return dict(job)

    def list(self):
        with self.lock:
            return sorted((dict(j) for j in self.jobs.values()), key=lambda j: j["created"], reverse=True)

    def get(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            return dict(j) if j else None

    def clear_finished(self):
        with self.lock:
            for k in [k for k, j in self.jobs.items() if j["status"] in ("done", "error")]:
                del self.jobs[k]
                self._drop_job_files(k)
        self._save_jobs()

    # ------------------------------------------------------------- private
    def _trim(self):
        if len(self.jobs) <= MAX_JOBS_KEPT:
            return
        done = sorted((j for j in self.jobs.values() if j["status"] in ("done", "error")), key=lambda j: j["created"])
        for j in done[: len(self.jobs) - MAX_JOBS_KEPT]:
            del self.jobs[j["id"]]
            self._drop_job_files(j["id"])

    def _update(self, job_id, **fields):
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(fields)

    def _ydl_opts(self, job, workdir):
        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                pct = (d.get("downloaded_bytes", 0) / total * 100) if total else 0
                info = d.get("info_dict") or {}
                idx, count = info.get("playlist_index"), info.get("n_entries")
                prefix = f"[{idx}/{count}] " if idx and count else ""
                self._update(job["id"], status="downloading", progress=round(pct, 1),
                             message=f"{prefix}Téléchargement {info.get('title') or ''}".strip())
            elif d["status"] == "finished":
                self._update(job["id"], status="processing", message="Conversion…")

        opts = {
            "impersonate": ImpersonateTarget(client="firefox"), # Ajout ***Olivier***
            "outtmpl": os.path.join(workdir, "%(title).180B.%(ext)s"),
            "noplaylist": not job["playlist"],
            "progress_hooks": [hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "writethumbnail": True,
            "ignoreerrors": job["playlist"],  # skip unavailable items in playlists
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
        }
        if PROXY:
            opts["proxy"] = PROXY
        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            opts["cookiefile"] = COOKIES_FILE

        if job["mode"] == "audio" and self._tagging(job):
            # Tags and cover are written afterwards by tagging.write_tags; keep the thumbnail file.
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": AUDIO_FORMAT, "preferredquality": AUDIO_QUALITY},
            ]
        elif job["mode"] == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": AUDIO_FORMAT, "preferredquality": AUDIO_QUALITY},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]
        else:
            h = f"[height<={VIDEO_MAX_HEIGHT}]" if VIDEO_MAX_HEIGHT else ""
            opts["format"] = f"bv*{h}[ext=mp4]+ba[ext=m4a]/bv*{h}+ba/b{h}/b"
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ]
        return opts

    @staticmethod
    def _tagging(job):
        return MUSIC_TAGGING and job["mode"] == "audio" and AUDIO_FORMAT == "mp3"

    @staticmethod
    def _downloaded(info):
        """[(final file path, entry info, thumbnail path)] for every item yt-dlp produced."""
        entries = info.get("entries") if info.get("_type") == "playlist" else [info]
        out = []
        for e in entries or []:
            if not e:
                continue
            thumb = next((t["filepath"] for t in reversed(e.get("thumbnails") or [])
                          if t.get("filepath") and os.path.exists(t["filepath"])), None)
            for rd in e.get("requested_downloads") or []:
                if rd.get("filepath") and os.path.exists(rd["filepath"]):
                    out.append((rd["filepath"], e, thumb))
        return out

    def _tag_and_upload(self, job_id, job, items):
        n = len(items)
        tag_list = []
        for i, (path, entry, thumb) in enumerate(items, 1):
            label = f"({i}/{n}) " if n > 1 else ""
            self._update(job_id, status="processing", progress=round((i - 1) / n * 100, 1),
                         message=f"{label}Recherche des tags…")
            try:
                t = musicmeta.identify(entry, self.library, path=path, auto_confidence=AUTO_CONFIDENCE)
            except Exception as e:  # MusicBrainz down, unexpected data…: keep what the site gave
                h = musicmeta.hints_from_info(entry)
                t = {"title": h["title"], "artist": h["artist"], "albumartist": h["artist"], "url": h["url"],
                     "confidence": 0.0, "genre": None, "cover": None, "tag_error": str(e)[:200]}
            tag_list.append(t)
        if n > 1:
            try:
                musicmeta.align_to_release(tag_list, AUTO_CONFIDENCE)
            except Exception:
                pass

        uploaded = []
        for i, ((path, entry, thumb), t) in enumerate(zip(items, tag_list), 1):
            label = f"({i}/{n}) " if n > 1 else ""
            thumb_cover = musicmeta.thumbnail_cover(thumb) if thumb else None
            if not t.get("cover") and thumb_cover:
                t["cover"], t["cover_source"] = thumb_cover, "miniature"
            auto_ok = t["confidence"] >= AUTO_CONFIDENCE
            self.library.record_unmapped(t.get("unmapped_genres") or [], t.get("artist"), t.get("genre_consensus") or [])
            tagging.write_tags(path, t)
            folder, name = tagging.target_path(t, job["folder"], auto_ok)
            final = os.path.join(os.path.dirname(path), f"up-{i}", name)
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(path, final)
            self._update(job_id, status="uploading", progress=0, message=f"{label}Envoi vers pCloud : {name}")
            res = self.pcloud.upload(final, folder, on_progress=lambda p: self._update(job_id, progress=round(p, 1)))
            res.update(confidence=round(t["confidence"], 2), genre=t.get("genre"), to_sort=not auto_ok,
                       album=t.get("album"), cover=t.get("cover_source"),
                       title=t.get("title"), artist=t.get("artist"), albumartist=t.get("albumartist"),
                       year=t.get("year"), track=t.get("track"), total_tracks=t.get("total_tracks"),
                       source=t.get("source"), duration=entry.get("duration"),
                       release=t.get("release_id") or t.get("discogs_release_id"),
                       cover_key=self._save_job_cover(job_id, t.get("cover")),
                       thumb_key=self._save_job_cover(job_id, thumb_cover) if thumb_cover != t.get("cover") else None,
                       root=job["folder"],
                       tags={k: v for k, v in t.items() if k not in ("cover",)})
            reason = "à trier" if not auto_ok else ("genre manquant" if not t.get("genre") else None)
            if reason:
                try:
                    item = self.review.add(t, res, thumb_cover, reason=reason, root=job["folder"], job_id=job_id,
                                           file_index=i - 1, group=self._group_key(job_id, t))
                    res["review_id"] = item["id"]
                except Exception as e:
                    sources.log.warning("review: %s", e)
            uploaded.append(res)
            os.remove(final)
            self._update(job_id, files=list(uploaded))
            self._save_jobs()
        return uploaded

    @staticmethod
    def _group_key(job_id, t):
        return f"{job_id}|{(t.get('albumartist') or '').lower()}|{(t.get('album') or '').lower()}"

    @staticmethod
    def summarize(files):
        """What the summary card shows: album-level tags and anything worth a second look."""
        if not files:
            return None
        first = files[0]
        albums = {(f.get("albumartist"), f.get("album")) for f in files if f.get("album")}
        genres = sorted({f.get("genre") for f in files if f.get("genre")})
        anomalies = []
        if not genres:
            anomalies.append("aucun genre")
        elif len(genres) > 1:
            anomalies.append("genres différents : " + ", ".join(genres))
        if any(f.get("cover") == "miniature" for f in files):
            anomalies.append("pochette : miniature de la vidéo")
        elif not any(f.get("cover_key") for f in files):
            anomalies.append("sans pochette")
        to_sort = sum(1 for f in files if f.get("to_sort"))
        if to_sort:
            anomalies.append(f"{to_sort} piste(s) dans « {tagging.TO_SORT_FOLDER} »")
        confs = [f.get("confidence") or 0 for f in files if not f.get("to_sort")]
        if confs and min(confs) < 0.9:
            anomalies.append(f"confiance moyenne ({round(min(confs) * 100)} %)")
        if len(albums) > 1:
            anomalies.append(f"pistes réparties sur {len(albums)} albums")
        expected = max((f.get("total_tracks") or 0 for f in files), default=0)
        if len(files) > 1 and expected and len(files) < expected:
            anomalies.append(f"{len(files)}/{expected} pistes")
        return {
            "kind": "album" if len(files) > 1 else "single",
            "title": first.get("title"), "artist": first.get("artist"),
            "album": first.get("album"), "albumartist": first.get("albumartist") or first.get("artist"),
            "year": first.get("year"), "genres": genres, "tracks": len(files),
            "sources": sorted({f.get("source") for f in files if f.get("source")}),
            "confidence": min((f.get("confidence") or 0) for f in files),
            "cover_key": next((f.get("cover_key") for f in files if f.get("cover_key")), None),
            "folder": first.get("path", "").rsplit("/", 1)[0],
            "anomalies": anomalies,
            "in_review": sum(1 for f in files if f.get("review_id")),
        }

    def _run(self, job_id):
        job = self.get(job_id)
        workdir = tempfile.mkdtemp(prefix=f"dl-{job_id}-", dir=WORK_DIR)
        try:
            if not self.pcloud.configured:
                raise RuntimeError("pCloud n'est pas configuré (PCLOUD_USERNAME/PCLOUD_PASSWORD ou PCLOUD_AUTH_TOKEN).")
            self._update(job_id, status="downloading", message="Analyse du lien…")
            with yt_dlp.YoutubeDL(self._ydl_opts(job, workdir)) as ydl:
                info = ydl.extract_info(job["url"], download=True)
            if not info:
                raise RuntimeError("Rien n'a pu être téléchargé depuis ce lien.")
            title = info.get("title") or job["url"]
            target = job["folder"]
            if info.get("_type") == "playlist":
                target = f"{target}/{_safe_folder(title)}"
            self._update(job_id, title=title)

            if self._tagging(job):
                items = self._downloaded(info)
                if not items:
                    raise RuntimeError("Le téléchargement n'a produit aucun fichier.")
                uploaded = self._tag_and_upload(job_id, job, items)
                to_sort = sum(1 for f in uploaded if f["to_sort"])
                msg = f"{len(uploaded)} fichier(s) rangé(s)"
                if to_sort:
                    msg += f", dont {to_sort} dans « {tagging.TO_SORT_FOLDER} » (à vérifier)"
                self._update(job_id, status="done", progress=100, finished=time.time(), message=msg,
                             summary=self.summarize(uploaded))
                self._save_jobs()
                return

            files = sorted(
                os.path.join(workdir, f) for f in os.listdir(workdir)
                if os.path.splitext(f)[1].lower() not in SKIP_EXT and os.path.isfile(os.path.join(workdir, f))
            )
            if not files:
                raise RuntimeError("Le téléchargement n'a produit aucun fichier.")

            uploaded = []
            for i, path in enumerate(files, 1):
                name = os.path.basename(path)
                self._update(job_id, status="uploading", progress=0,
                             message=f"Envoi vers pCloud ({i}/{len(files)}) : {name}")
                res = self.pcloud.upload(path, target, on_progress=lambda p: self._update(job_id, progress=round(p, 1)))
                uploaded.append(res)
                os.remove(path)
                self._update(job_id, files=list(uploaded))

            self._update(job_id, status="done", progress=100, finished=time.time(),
                         message=f"{len(uploaded)} fichier(s) envoyé(s) dans {target}")
            self._save_jobs()
        except Exception as e:
            msg = str(e)
            msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)  # strip yt-dlp colour codes
            if "obtain file audio codec" in msg:
                msg = "Ce média ne contient pas de piste audio."
            elif "not a bot" in msg:
                msg = "Le site bloque le serveur (anti-bot). Ajoute des cookies via la variable YTDLP_COOKIES. " + msg
            self._update(job_id, status="error", error=msg, finished=time.time(), message="Erreur")
            self._save_jobs()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


    # --------------------------------------------------------------- editing
    def edit_job(self, job_id):
        """Send the files of a finished download to the validation page, without moving them."""
        job = self.get(job_id)
        if not job or job["status"] != "done":
            raise KeyError(job_id)
        created = 0
        files = job["files"]
        for idx, f in enumerate(files):
            if not f.get("tags") or not f.get("fileid"):
                continue  # video, or downloaded before the history existed
            if f.get("review_id") and self.review.get(f["review_id"]):
                continue
            t = dict(f["tags"])
            t["cover"] = self.job_cover(job_id, f["cover_key"], full=True) if f.get("cover_key") else None
            thumb = self.job_cover(job_id, f["thumb_key"], full=True) if f.get("thumb_key") else None
            reason = "à trier" if f.get("to_sort") else "modification"
            item = self.review.add(t, {"fileid": f["fileid"], "path": f["path"]}, thumb, reason=reason,
                                   root=f.get("root") or job["folder"], job_id=job_id, file_index=idx,
                                   group=self._group_key(job_id, t))
            f["review_id"] = item["id"]
            created += 1
        self._update(job_id, files=files, summary=self.summarize(files))
        self._save_jobs()
        return created

    def _job_file_validated(self, item, res, tags, cover=None, cover_source=None):
        """Keep the download history in step after a validation."""
        job_id, idx = item.get("job_id"), item.get("file_index")
        if job_id is None or idx is None:
            return
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or idx >= len(job["files"]):
                return
            f = job["files"][idx]
            f.update(path=res["path"], name=res["name"], fileid=res.get("fileid") or f.get("fileid"), to_sort=False,
                     review_id=None, genre=tags.get("genre"), title=tags.get("title"), artist=tags.get("artist"),
                     albumartist=tags.get("albumartist"), album=tags.get("album"), year=tags.get("year"),
                     track=tags.get("track"), total_tracks=tags.get("total_tracks"), confidence=1.0,
                     tags={k: v for k, v in tags.items() if k != "cover"})
            f["cover"] = cover_source
            job["summary"] = self.summarize(job["files"])
        f["cover_key"] = self._save_job_cover(job_id, cover)
        with self.lock:
            job["summary"] = self.summarize(job["files"])
        self._save_jobs()

    # ------------------------------------------------------------ validation
    @staticmethod
    def _clean_edits(edits):
        out = {}
        for key in EDITABLE:
            if key not in edits:
                continue
            value = edits[key]
            value = str(value).strip() if value is not None else ""
            if key in ("track", "total_tracks", "disc", "total_discs"):
                out[key] = int(value) if value.isdigit() and int(value) > 0 else None
            else:
                out[key] = value or None
        return out

    def review_preview(self, item_id, edits):
        item = self.review.get(item_id)
        if not item:
            raise KeyError(item_id)
        tags = dict(item["tags"], **self._clean_edits(edits))
        folder, name = tagging.target_path(tags, self._root_of(item), True)
        return f"{folder}/{name}"

    @staticmethod
    def _root_of(item):
        if item.get("root"):
            return item["root"]
        path = item.get("path") or ""
        marker = f"/{tagging.TO_SORT_FOLDER}/"
        return path.split(marker)[0] if marker in path else AUDIO_FOLDER

    def review_candidate(self, item_id, index):
        item = self.review.get(item_id)
        if not item:
            raise KeyError(item_id)
        try:
            cand = item["candidates"][int(index)]
        except (IndexError, ValueError, TypeError):
            raise RuntimeError("Ce résultat n'existe plus.")
        details = musicmeta.candidate_details(cand, item.get("hints") or {})
        if not details:
            raise RuntimeError("Impossible de charger ce candidat.")
        cover = details.pop("cover", None)
        cover_source = details.pop("cover_source", None)
        tags = dict(item["tags"])
        for k in ("release_id", "release_group_id", "recording_id", "discogs_release_id", "track_id", "isrc",
                  "date", "original_date", "release_type", "compilation", "total_discs", "disc"):
            tags.pop(k, None)
        tags.update({k: v for k, v in details.items() if v is not None})
        tags["source"] = cand.get("source")
        covers = item["covers"]
        if cover:
            name = f"candidat-{index}"
            self.review.save_cover(item_id, name, cover)
            if not any(c["name"] == name for c in covers):
                covers.append({"name": name, "label": f"{cover_source} ({cand.get('album') or 'candidat'})"})
            item["cover"] = name
        self.review.update(item_id, tags=tags, covers=covers, cover=item["cover"])
        return self.review.get(item_id)

    def review_validate(self, item_id, edits, cover_name=None, remember=True):
        """Retag the file stored in "_A trier" and move it to its final place in pCloud."""
        item = self.review.get(item_id)
        if not item:
            raise KeyError(item_id)
        tags = dict(item["tags"], **self._clean_edits(edits))
        if not item.get("fileid") or not self.pcloud.exists(item["fileid"]):
            raise RuntimeError("Le fichier n'est plus dans pCloud (déplacé ou supprimé ?). Retire-le de la liste.")
        cover = self.review.read_cover(item_id, cover_name) if cover_name else None
        workdir = tempfile.mkdtemp(prefix=f"rv-{item_id}-", dir=WORK_DIR)
        try:
            folder, name = tagging.target_path(tags, self._root_of(item), True)
            local = os.path.join(workdir, name)
            self.pcloud.download(item["fileid"], local)
            tagging.write_tags(local, dict(tags, cover=cover))
            same_place = f"{folder}/{name}" == item.get("path")
            # Same path: overwrite (pCloud keeps the previous version). New path: upload, then remove the old one.
            res = self.pcloud.upload(local, folder, overwrite=same_place)
            if not same_place:
                self.pcloud.delete(item["fileid"])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if remember and tags.get("genre"):
            self.library.remember_artist_genre(tags.get("albumartist") or tags.get("artist"), tags["genre"])
            if tags.get("artist") and tags.get("artist") != tags.get("albumartist"):
                self.library.remember_artist_genre(tags["artist"], tags["genre"])
        if not cover:
            cover_source = None
        elif cover_name == "miniature":
            cover_source = "miniature"
        elif cover_name == "actuelle":
            cover_source = tags.get("cover_source")
        else:  # another result's cover: "Cover Art Archive (album)" -> "Cover Art Archive"
            label = next((c["label"] for c in item.get("covers", []) if c["name"] == cover_name), "")
            cover_source = label.split(" (")[0] or None
        self._job_file_validated(item, res, tags, cover, cover_source)
        self.review.remove(item_id)
        return res["path"]
