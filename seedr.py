"""Seedr.cc REST v1 client (https://www.seedr.cc/docs/api/rest/v1/).

Auth: HTTP Basic with the user's Seedr email + password.
Note from Seedr's own docs: REST v1 with Basic auth is only secure for
personal use - which is exactly what this addon is (one user's own server).

Endpoints used:
  POST /rest/transfer/magnet   {magnet: <magnet link>}  -> transfer id
  GET  /rest/transfer/{id}                               -> progress
  GET  /rest/folder                                      -> root listing
  GET  /rest/folder/{folder_id}                          -> folder listing
  GET  /rest/file/{file_id}                              -> direct download
  GET  /rest/file/{file_id}/hls                          -> HLS video stream
"""

import re
import time
import requests

BASE = "https://www.seedr.cc/rest"
VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class Seedr:
    def __init__(self, email, password, timeout=15):
        self.auth = (email, password)
        self.timeout = timeout

    def _get(self, path):
        r = requests.get(BASE + path, auth=self.auth, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path, data):
        r = requests.post(BASE + path, data=data, auth=self.auth,
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def list_folder(self, folder_id=None):
        path = "/folder" + (f"/{folder_id}" if folder_id else "")
        return self._get(path)

    def add_magnet(self, magnet):
        """Returns the transfer id, or None on failure."""
        try:
            res = self._post("/transfer/magnet", {"magnet": magnet})
            if isinstance(res, dict):
                return res.get("id") or res.get("transfer_id")
        except Exception:
            return None
        return None

    def transfer_status(self, transfer_id):
        try:
            return self._get(f"/transfer/{transfer_id}")
        except Exception:
            return None

    def delete_file(self, file_id):
        """Delete one file. Returns True on success."""
        try:
            self._post(f"/file/{file_id}/delete", {})
            return True
        except Exception:
            pass
        try:
            r = requests.delete(BASE + f"/file/{file_id}", auth=self.auth,
                                timeout=self.timeout)
            return r.status_code < 400
        except Exception:
            return False

    def user_space(self):
        """(used_bytes, total_bytes) or (None, None) if Seedr won't say.

        Tries /rest/user with several likely field names, then falls back
        to summing the whole library by listing folders.
        """
        try:
            u = self._get("/user")
            if isinstance(u, dict):
                total = (u.get("space_max") or u.get("space") or
                         (u.get("package") or {}).get("space"))
                used = u.get("space_used")
                if total and used is not None:
                    return int(used), int(total)
        except Exception:
            pass
        try:
            used = 0
            root = self.list_folder()
            for f in root.get("files", []) or []:
                used += f.get("size", 0) or 0
            for folder in root.get("folders", []) or []:
                try:
                    data = self.list_folder(folder.get("id"))
                    for f in data.get("files", []) or []:
                        used += f.get("size", 0) or 0
                except Exception:
                    continue
            return used, None
        except Exception:
            return None, None

    def find_file(self, file_id):
        """True if file_id still exists anywhere in the library."""
        try:
            root = self.list_folder()
            for f in root.get("files", []) or []:
                if str(f.get("id")) == str(file_id):
                    return True
            for folder in root.get("folders", []) or []:
                try:
                    data = self.list_folder(folder.get("id"))
                except Exception:
                    continue
                for f in data.get("files", []) or []:
                    if str(f.get("id")) == str(file_id):
                        return True
        except Exception:
            pass
        return False

    def find_folder(self, torrent_name, before_ids):
        """Find a newly appeared folder whose name matches the torrent."""
        try:
            root = self.list_folder()
        except Exception:
            return None
        want = _norm(torrent_name)
        for f in root.get("folders", []) or []:
            fid = f.get("id")
            if fid in before_ids:
                continue
            name = _norm(f.get("name", ""))
            if name and (name in want or want in name or
                         name.split()[0] == want.split()[0]):
                return fid
        return None

    def largest_video_file(self, folder_id):
        """Largest playable video file inside a folder (or None)."""
        try:
            data = self.list_folder(folder_id)
        except Exception:
            return None
        best = None
        for f in data.get("files", []) or []:
            name = f.get("name", "")
            if not name.lower().endswith(VIDEO_EXTS):
                continue
            size = f.get("size", 0) or 0
            if best is None or size > best[1]:
                best = (f.get("id"), size, name)
        return best  # (file_id, size, name) or None

    def resolve(self, magnet, torrent_name, deadline_seconds=25):
        """Add a magnet to Seedr and wait (bounded) for the file.

        Returns (file_id, file_name) once downloadable, else None.
        Already-downloaded torrents resolve instantly on repeat calls.
        """
        try:
            root = self.list_folder()
            before = {f.get("id") for f in root.get("folders", []) or []}
        except Exception:
            return None
        transfer_id = self.add_magnet(magnet)
        if transfer_id is None:
            return None
        deadline = time.time() + deadline_seconds
        while time.time() < deadline:
            folder_id = self.find_folder(torrent_name, before)
            if folder_id:
                best = self.largest_video_file(folder_id)
                if best:
                    return best[0], best[2]
            # If the transfer finished but no folder matched, re-check once
            # more after a short sleep; Seedr may name the folder slightly
            # differently, so also accept any brand-new folder.
            try:
                root = self.list_folder()
                for f in root.get("folders", []) or []:
                    if f.get("id") not in before:
                        best = self.largest_video_file(f.get("id"))
                        if best:
                            return best[0], best[2]
            except Exception:
                pass
            time.sleep(3)
        return None
