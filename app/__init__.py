import hmac
import os
import secrets
from urllib.parse import urlencode

import jwt
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

from .jobs import AUDIO_FOLDER, VIDEO_FOLDER, JobManager

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
manager = JobManager()

PCLOUD_CLIENT_ID = os.getenv("PCLOUD_CLIENT_ID", "")
PCLOUD_CLIENT_SECRET = os.getenv("PCLOUD_CLIENT_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

API_TOKEN = os.getenv("API_TOKEN", "")
CF_TEAM = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")   # e.g. myteam.cloudflareaccess.com
CF_AUD = os.getenv("CF_ACCESS_AUD", "").strip()
_jwks = jwt.PyJWKClient(f"https://{CF_TEAM.removeprefix('https://')}/cdn-cgi/access/certs") if CF_TEAM and CF_AUD else None


def _authorized():
    """Accept a valid Cloudflare Access JWT, or the optional API token.

    With neither configured the app is open: only put it behind Cloudflare Access then.
    """
    if not _jwks and not API_TOKEN:
        return True
    if API_TOKEN:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.headers.get("X-API-Key", "")
        if supplied and hmac.compare_digest(supplied, API_TOKEN):
            return True
    if _jwks:
        token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
        if token:
            try:
                key = _jwks.get_signing_key_from_jwt(token).key
                jwt.decode(token, key, algorithms=["RS256"], audience=CF_AUD)
                return True
            except Exception:
                return False
    return False


@app.before_request
def _guard():
    if request.path == "/health":
        return None
    if not _authorized():
        abort(401)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return render_template("index.html", audio_folder=AUDIO_FOLDER, video_folder=VIDEO_FOLDER,
                           pcloud_ok=manager.pcloud.configured, pcloud_oauth=manager.pcloud.oauth_connected,
                           oauth_available=bool(PCLOUD_CLIENT_ID and PCLOUD_CLIENT_SECRET),
                           pcloud_msg=request.args.get("pcloud"))


def _redirect_uri():
    base = PUBLIC_URL or f"https://{request.host}"
    return f"{base}{url_for('pcloud_callback')}"


@app.get("/pcloud/connect")
def pcloud_connect():
    if not (PCLOUD_CLIENT_ID and PCLOUD_CLIENT_SECRET):
        return "PCLOUD_CLIENT_ID et PCLOUD_CLIENT_SECRET ne sont pas configurés.", 400
    session["pcloud_state"] = state = secrets.token_urlsafe(16)
    query = urlencode({"client_id": PCLOUD_CLIENT_ID, "response_type": "code",
                       "redirect_uri": _redirect_uri(), "state": state})
    return redirect(f"https://my.pcloud.com/oauth2/authorize?{query}")


@app.get("/pcloud/callback")
def pcloud_callback():
    if not request.args.get("state") or request.args.get("state") != session.pop("pcloud_state", None):
        return redirect(url_for("index", pcloud="Échec : requête expirée, recommence."))
    code = request.args.get("code")
    if not code:
        return redirect(url_for("index", pcloud="Échec : autorisation refusée."))
    try:
        manager.pcloud.connect_oauth(PCLOUD_CLIENT_ID, PCLOUD_CLIENT_SECRET, code, request.args.get("hostname", ""))
    except Exception as e:
        return redirect(url_for("index", pcloud=f"Échec : {e}"))
    return redirect(url_for("index", pcloud="pCloud est connecté."))


def _request_fields():
    """Read the request whatever way a client sends it: JSON, form, query string or a bare URL body."""
    data = {}
    data.update(request.args.to_dict())
    data.update(request.form.to_dict())
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        data.update(body)
    elif isinstance(body, str):
        data.setdefault("url", body)
    if not data.get("url") and not data.get("link"):
        raw = request.get_data(as_text=True).strip()
        if raw.startswith(("http://", "https://")):
            data["url"] = raw
    return data


def _mode_from(data):
    """Accept mode, format or type, with values like audio/mp3/m4a or video/mp4."""
    value = str(data.get("mode") or data.get("format") or data.get("type") or "audio").lower()
    return "video" if value in ("video", "mp4", "vidéo", "webm", "mkv") else "audio"


@app.post("/api/download")
@app.route("/download", methods=["GET", "POST"])  # older iOS shortcut
def download():
    data = _request_fields()
    url = str(data.get("url") or data.get("link") or "").strip()
    if not url.startswith(("http://", "https://")):
        app.logger.warning("download refused: no valid URL (fields received: %s)", sorted(data))
        return jsonify(error="Lien invalide"), 400
    playlist = str(data.get("playlist", "")).lower() in ("1", "true", "on", "yes")
    job = manager.submit(url, mode=_mode_from(data), playlist=playlist, folder=data.get("folder"),
                         duplicates=str(data.get("duplicates") or os.getenv("DUPLICATES", "skip")).lower())
    app.logger.info("download queued via %s: job %s (%s)", request.path, job["id"], job["mode"])
    return jsonify(job), 202


@app.get("/api/jobs")
def jobs():
    out = []
    for j in manager.list():  # the per-file tag details are only needed server-side
        j = dict(j, files=[{k: v for k, v in f.items() if k != "tags"} for f in j.get("files") or []])
        out.append(j)
    return jsonify(out)


@app.get("/api/jobs/<job_id>")
def job(job_id):
    j = manager.get(job_id)
    return (jsonify(j), 200) if j else (jsonify(error="Introuvable"), 404)


@app.get("/api/jobs/<job_id>/cover/<key>")
def job_cover(job_id, key):
    data = manager.job_cover(job_id, key)
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/jobs/<job_id>/edit")
def job_edit(job_id):
    try:
        created = manager.edit_job(job_id)
    except KeyError:
        return jsonify(error="Téléchargement introuvable ou pas terminé"), 404
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, created=created)


@app.post("/api/jobs/clear")
def clear():
    manager.clear_finished()
    return jsonify(ok=True)


# ------------------------------------------------------------------ validation
def _item_view(item):
    item = dict(item)
    item["preview"] = manager.review_preview(item["id"], {})
    # Suggestions saved with the item: drop genres added, mapped or ignored since then.
    tags = item["tags"] = dict(item["tags"])
    tags["genre_consensus"] = [g for g in tags.get("genre_consensus") or [] if not manager.library._known(g)]
    return item


@app.get("/valider")
def review_page():
    return render_template("review.html")


@app.get("/api/review")
def review_list():
    lib = manager.library
    return jsonify(items=[_item_view(i) for i in manager.review.list()], genres=lib.genres,
                   suggestions=lib.suggestions())


@app.get("/api/review/count")
def review_count():
    return jsonify(count=manager.review.count(), suggestions=len(manager.library.suggestions()))


@app.get("/api/review/<item_id>/cover/<name>")
def review_cover(item_id, name):
    data = manager.review.read_cover(item_id, name)
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/review/<item_id>/preview")
def review_preview(item_id):
    try:
        return jsonify(path=manager.review_preview(item_id, request.get_json(silent=True) or {}))
    except KeyError:
        return jsonify(error="Introuvable"), 404


@app.post("/api/review/<item_id>/candidate")
def review_candidate(item_id):
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_item_view(manager.review_candidate(item_id, data.get("index", 0))))
    except KeyError:
        return jsonify(error="Introuvable"), 404
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.post("/api/review/<item_id>/validate")
def review_validate(item_id):
    data = request.get_json(silent=True) or {}
    try:
        path = manager.review_validate(item_id, data.get("tags") or {}, data.get("cover"),
                                       bool(data.get("remember", True)))
        return jsonify(ok=True, path=path)
    except KeyError:
        return jsonify(error="Introuvable"), 404
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.post("/api/review/<item_id>/dismiss")
def review_dismiss(item_id):
    manager.review_dismiss([item_id])
    return jsonify(ok=True)


@app.post("/api/review/dismiss")
def review_dismiss_many():
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    manager.review_dismiss([str(i) for i in ids])
    return jsonify(ok=True, removed=len(ids))


# ------------------------------------------------------------- genre suggestions
@app.post("/api/genres/suggestion")
def genre_suggestion():
    data = request.get_json(silent=True) or {}
    name, action = (data.get("name") or "").strip(), data.get("action")
    if not name:
        return jsonify(error="Nom manquant"), 400
    lib = manager.library
    try:
        if action == "add":
            added = lib.add_genre_manual(data.get("label") or name.title())
            return jsonify(ok=True, genre=added, genres=lib.genres)
        if action == "map":
            lib.map_genre_to(name, data.get("target"))
        elif action == "ignore":
            lib.ignore_genre(name)
        else:
            return jsonify(error="Action inconnue"), 400
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, genres=lib.genres)


# --------------------------------------------------------------- my genres page
@app.get("/genres")
def genres_page():
    return render_template("genres.html")


@app.get("/api/genres")
def genres_data():
    lib = manager.library
    return jsonify(genres=lib.genres, map=lib.data["genre_map"], artists=lib.data["artist_genres"],
                   suggestions=lib.suggestions())


@app.post("/api/genres")
def genres_update():
    d = request.get_json(silent=True) or {}
    lib, action = manager.library, d.get("action")
    try:
        if action == "add":
            result = lib.add_genre_manual(d.get("name"))
        elif action == "rename":
            result = lib.rename_genre(d.get("name"), d.get("new"))
        elif action == "delete":
            result = lib.delete_genre(d.get("name"))
        elif action == "map":
            result = lib.set_mapping(d.get("source"), d.get("target"))
        elif action == "unmap":
            result = lib.delete_mapping(d.get("source"))
        elif action == "artist":
            result = lib.set_artist(d.get("artist"), d.get("genre"))
        elif action == "unartist":
            result = lib.delete_artist(d.get("artist"))
        else:
            return jsonify(error="Action inconnue"), 400
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, result=result, genres=lib.genres)


# ------------------------------------------------------------------ cleanup
@app.get("/nettoyer")
def cleanup_page():
    return render_template("cleanup.html")


@app.get("/api/cleanup")
def cleanup_list():
    from .cleanup import MAX_FILES
    return jsonify(runs=manager.cleanup.list(), root=AUDIO_FOLDER, max_files=MAX_FILES, busy=manager.cleanup.busy())


@app.get("/api/cleanup/folders")
def cleanup_folders():
    path = request.args.get("path") or AUDIO_FOLDER
    try:
        return jsonify(path="/" + path.strip("/"), folders=manager.pcloud.list_subfolders(path))
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.post("/api/cleanup/start")
def cleanup_start():
    d = request.get_json(silent=True) or {}
    try:
        run = manager.cleanup.start(d.get("folder") or AUDIO_FOLDER, d.get("root") or AUDIO_FOLDER,
                                    {k: bool((d.get("options") or {}).get(k)) for k in ("tags", "genre", "covers", "rename", "lyrics")},
                                    bool(d.get("recheck")))
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(id=run["id"])


@app.get("/api/cleanup/<run_id>")
def cleanup_get(run_id):
    run = manager.cleanup.get(run_id)
    if not run:
        return jsonify(error="Introuvable"), 404
    for it in run.get("items", []):  # the review page data is not needed here
        it.pop("candidates", None)
        it.pop("hints", None)
    return jsonify(run)


@app.get("/api/cleanup/<run_id>/cover/<int:idx>/<kind>")
def cleanup_cover(run_id, idx, kind):
    data = manager.cleanup.cover(run_id, idx, kind)
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _cleanup_action(fn, *args):
    try:
        result = fn(*args)
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, result=result)


@app.post("/api/cleanup/<run_id>/apply")
def cleanup_apply(run_id):
    d = request.get_json(silent=True) or {}
    return _cleanup_action(manager.cleanup.apply, run_id, d.get("selected") or [], d.get("covers") or [])


@app.post("/api/cleanup/<run_id>/undo")
def cleanup_undo(run_id):
    return _cleanup_action(manager.cleanup.undo, run_id)


@app.post("/api/cleanup/<run_id>/review")
def cleanup_review(run_id):
    return _cleanup_action(manager.cleanup.send_to_review, run_id, (request.get_json(silent=True) or {}).get("idx"))


@app.post("/api/cleanup/<run_id>/genre")
def cleanup_genre(run_id):
    d = request.get_json(silent=True) or {}
    return _cleanup_action(manager.cleanup.set_genre, run_id, d.get("idxs") or [], d.get("genre"))


@app.post("/api/cleanup/<run_id>/review-many")
def cleanup_review_many(run_id):
    return _cleanup_action(manager.cleanup.send_many_to_review, run_id, (request.get_json(silent=True) or {}).get("idxs") or [])


@app.post("/api/cleanup/<run_id>/duplicate")
def cleanup_duplicate(run_id):
    d = request.get_json(silent=True) or {}
    return _cleanup_action(manager.cleanup.duplicate, run_id, d.get("idx"), d.get("action"))


@app.post("/api/cleanup/<run_id>/delete")
def cleanup_delete(run_id):
    return _cleanup_action(manager.cleanup.delete, run_id)
