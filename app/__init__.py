import hmac
import os
import secrets
from urllib.parse import urlencode

import jwt
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

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
