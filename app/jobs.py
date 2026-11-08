"""Download jobs: yt-dlp download, then upload to pCloud."""
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from yt_dlp.networking.impersonate import ImpersonateTarget # Ajout ***Olivier***

from . import musicmeta, tagging
from .library import Library
from .pcloud import PCloud

AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "mp3")          # mp3, m4a, opus, flac...
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "0")          # 0 = best VBR for mp3, or e.g. 192
VIDEO_MAX_HEIGHT = os.getenv("VIDEO_MAX_HEIGHT", "")     # e.g. 1080, empty = best
AUDIO_FOLDER = os.getenv("PCLOUD_AUDIO_FOLDER", "/Music")
VIDEO_FOLDER = os.getenv("PCLOUD_VIDEO_FOLDER", "/Videos")
WORK_DIR = os.getenv("WORK_DIR", tempfile.gettempdir())
MAX_JOBS_KEPT = int(os.getenv("MAX_JOBS_KEPT", "100"))
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
        self.jobs = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=int(os.getenv("MAX_CONCURRENT", "2")))
        self.pcloud = PCloud.from_env()
        self.library = Library()

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

    # ------------------------------------------------------------- private
    def _trim(self):
        if len(self.jobs) <= MAX_JOBS_KEPT:
            return
        done = sorted((j for j in self.jobs.values() if j["status"] in ("done", "error")), key=lambda j: j["created"])
        for j in done[: len(self.jobs) - MAX_JOBS_KEPT]:
            del self.jobs[j["id"]]

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
                t = musicmeta.identify(entry, self.library)
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
            if not t.get("cover") and thumb:
                t["cover"], t["cover_source"] = musicmeta.thumbnail_cover(thumb), "miniature"
            auto_ok = t["confidence"] >= AUTO_CONFIDENCE
            tagging.write_tags(path, t)
            folder, name = tagging.target_path(t, job["folder"], auto_ok)
            final = os.path.join(os.path.dirname(path), f"up-{i}", name)
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(path, final)
            self._update(job_id, status="uploading", progress=0, message=f"{label}Envoi vers pCloud : {name}")
            res = self.pcloud.upload(final, folder, on_progress=lambda p: self._update(job_id, progress=round(p, 1)))
            res.update(confidence=round(t["confidence"], 2), genre=t.get("genre"), to_sort=not auto_ok,
                       album=t.get("album"), cover=t.get("cover_source"))
            uploaded.append(res)
            os.remove(final)
            self._update(job_id, files=list(uploaded))
        return uploaded

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
                self._update(job_id, status="done", progress=100, finished=time.time(), message=msg)
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
        except Exception as e:
            msg = str(e)
            msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)  # strip yt-dlp colour codes
            if "obtain file audio codec" in msg:
                msg = "Ce média ne contient pas de piste audio."
            elif "not a bot" in msg:
                msg = "Le site bloque le serveur (anti-bot). Ajoute des cookies via la variable YTDLP_COOKIES. " + msg
            self._update(job_id, status="error", error=msg, finished=time.time(), message="Erreur")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
