"""Minimal pCloud API client: login, folder creation and streaming upload."""
import os
import threading

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

# pCloud keeps EU and US accounts on different API hosts.
HOSTS = {"eu": "eapi.pcloud.com", "us": "api.pcloud.com"}


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
        return cls(
            region=os.getenv("PCLOUD_REGION"),
            auth_token=os.getenv("PCLOUD_AUTH_TOKEN"),
            access_token=os.getenv("PCLOUD_ACCESS_TOKEN"),
            username=os.getenv("PCLOUD_USERNAME"),
            password=os.getenv("PCLOUD_PASSWORD"),
        )

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
                params = {"getauth": 1, "logout": 1}
                headers = {}
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"
                elif self.auth_token:
                    params = {"auth": self.auth_token}
                else:
                    params.update(username=self.username, password=self.password)
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
            raise PCloudError(f"Connexion pCloud impossible ({last_error})")

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
