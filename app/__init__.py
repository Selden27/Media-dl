import hmac
import os

import jwt
from flask import Flask, abort, jsonify, render_template, request

from .jobs import AUDIO_FOLDER, VIDEO_FOLDER, JobManager

app = Flask(__name__)
manager = JobManager()

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
                           pcloud_ok=manager.pcloud.configured)


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or request.form
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Lien invalide"), 400
    playlist = str(data.get("playlist", "")).lower() in ("1", "true", "on", "yes")
    job = manager.submit(url, mode=data.get("mode", "audio"), playlist=playlist, folder=data.get("folder"))
    return jsonify(job), 202


@app.get("/api/jobs")
def jobs():
    return jsonify(manager.list())


@app.get("/api/jobs/<job_id>")
def job(job_id):
    j = manager.get(job_id)
    return (jsonify(j), 200) if j else (jsonify(error="Introuvable"), 404)


@app.post("/api/jobs/clear")
def clear():
    manager.clear_finished()
    return jsonify(ok=True)
