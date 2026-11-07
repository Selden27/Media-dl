"""Minimal pCloud API client: OAuth / login, folder creation and streaming upload."""
import json
import os
import threading

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

# pCloud keeps EU and US accounts on different API hosts.
HOSTS = {"eu": "eapi.pcloud.com", "us": "api.pcloud.com"}
# Where the OAuth token obtained through the "Connecter pCloud" button is kept (persistent volume).
TOKEN_FILE = os.getenv("PCLOUD_TOKEN_FILE", "/data/pcloud.json")


class PCloudError(Exception):
    pass


class PCloud:
    def __init__(self, region=None, auth_token=None, access_token=None, username=None, password=None):
        self.region = (region or "").lower() or None
        self.auth_token = auth_token or None
        self.access_token = access_token or None
        self.username = username or None
        self.password = password or None
        self.host = HOSTS.get(self.region) if self.region else None
        self._lock = threading.Lock()
        self._folder_cache = {}

    @classmethod
    def from_env(cls):
        access_token = os.getenv("PCLOUD_ACCESS_TOKEN")
        region = os.getenv("PCLOUD_REGION")
        saved = cls._load_token()
        if not access_token and saved:
            access_token = saved.get("access_token")
            region = region or saved.get("region")
        return cls(
            region=region,
            auth_token=os.getenv("PCLOUD_AUTH_TOKEN"),
            access_token=access_token,
            username=os.getenv("PCLOUD_USERNAME"),
            password=os.getenv("PCLOUD_PASSWORD"),
        )

    # ----------------------------------------------------------------- oauth
    @staticmethod
    def _load_token():
        try:
            with open(TOKEN_FILE) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @property
    def oauth_connected(self):
        return bool(self.access_token)

    def connect_oauth(self, client_id, client_secret, code, hostname):
        """Exchange an OAuth code for a permanent access token and keep it."""
        host = hostname if hostname in HOSTS.values() else HOSTS["eu"]
        r = requests.post(f"https://{host}/oauth2_token",
                          data={"client_id": client_id, "client_secret": client_secret, "code": code}, timeout=30)
        data = r.json()
        if data.get("result") != 0:
            raise PCloudError(f"OAuth pCloud: {data.get('result')} {data.get('error')}")
        region = "us" if host == HOSTS["us"] else "eu"
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as fh:
            json.dump({"access_token": data["access_token"], "region": region, "uid": data.get("uid")}, fh)
        os.chmod(TOKEN_FILE, 0o600)
        with self._lock:
            self.access_token, self.region, self.host = data["access_token"], region, host
            self.auth_token = None
            self._folder_cache.clear()

    @property
    def configured(self):
        return bool(self.auth_token or self.access_token or (self.username and self.password))

    # ------------------------------------------------------------------ auth
    def _candidate_hosts(self):
        if self.host:
            return [self.host]
        return [HOSTS["eu"], HOSTS["us"]]

    def _login(self):
        """Resolve the API host and, with username/password, get an auth token."""
        with self._lock:
            if self.host and (self.auth_token or self.access_token):
                return
            last_error = None
            for host in self._candidate_hosts():
                params, headers = {}, {}
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"
                elif self.auth_token:
                    params = {"auth": self.auth_token}
                else:
                    params = {"getauth": 1, "logout": 1, "username": self.username, "password": self.password}
                try:
                    r = requests.post(f"https://{host}/userinfo", data=params, headers=headers, timeout=30)
                    data = r.json()
                except Exception as e:  # network / JSON error
                    last_error = str(e)
                    continue
                if data.get("result") == 0:
                    self.host = host
                    if not (self.auth_token or self.access_token):
                        self.auth_token = data["auth"]
                    return
                last_error = f"{data.get('result')}: {data.get('error')}"
            hint = ""
            if not (self.access_token or self.auth_token) and last_error and last_error.split(":")[0] in ("1022", "2297"):
                hint = (" — pCloud demande un code de vérification pour cette connexion (nouvel appareil ou 2FA)."
                        " Utilise plutôt un jeton : PCLOUD_ACCESS_TOKEN (voir README, « rclone authorize pcloud »).")
            raise PCloudError(f"Connexion pCloud impossible ({last_error}){hint}")

    def _auth_kwargs(self):
        if self.access_token:
            return {}, {"Authorization": f"Bearer {self.access_token}"}
        return {"auth": self.auth_token}, {}

    def call(self, method, **params):
        self._login()
        auth_params, headers = self._auth_kwargs()
        r = requests.post(f"https://{self.host}/{method}", data={**params, **auth_params}, headers=headers, timeout=60)
        data = r.json()
        if data.get("result") != 0:
            raise PCloudError(f"pCloud {method}: {data.get('result')} {data.get('error')}")
        return data

    # --------------------------------------------------------------- folders
    def ensure_folder(self, path):
        """Create every level of `path` if missing and return its folder id."""
        path = "/" + path.strip("/")
        if path == "/":
            return 0
        if path in self._folder_cache:
            return self._folder_cache[path]
        current = ""
        folder_id = 0
        for part in path.strip("/").split("/"):
            current += "/" + part
            if current in self._folder_cache:
                folder_id = self._folder_cache[current]
                continue
            data = self.call("createfolderifnotexists", folderid=folder_id, name=part)
            folder_id = data["metadata"]["folderid"]
            self._folder_cache[current] = folder_id
        return folder_id

    # ---------------------------------------------------------------- upload
    def upload(self, local_path, folder_path, on_progress=None):
        """Stream a file to pCloud without loading it in memory."""
        folder_id = self.ensure_folder(folder_path)
        self._login()
        auth_params, headers = self._auth_kwargs()
        filename = os.path.basename(local_path)
        with open(local_path, "rb") as fh:
            encoder = MultipartEncoder(fields={"file": (filename, fh, "application/octet-stream")})
            total = encoder.len

            def cb(monitor):
                if on_progress and total:
                    on_progress(monitor.bytes_read / total * 100)

            monitor = MultipartEncoderMonitor(encoder, cb)
            params = {"folderid": folder_id, "nopartial": 1, "renameifexists": 1, **auth_params}
            r = requests.post(
                f"https://{self.host}/uploadfile",
                params=params,
                data=monitor,
                headers={**headers, "Content-Type": monitor.content_type},
                timeout=(30, 3600),
            )
        data = r.json()
        if data.get("result") != 0:
            raise PCloudError(f"Upload pCloud: {data.get('result')} {data.get('error')}")
        meta = data["metadata"][0]
        return {"name": meta["name"], "size": meta.get("size"), "path": f"{folder_path.rstrip('/')}/{meta['name']}"}
