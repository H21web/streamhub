"""StreamHub - Stremio addon backend (Flask).

Flow:
  Stremio -> /<config>/stream/<type>/<id>.json
    1. Title/year resolved via Cinemeta (public, no key).
    2. Jackett (user's own instance) searched for torrents.
    3. Results filtered by the user's config (quality, CAM, sort).
    4. Each magnet is added to the user's own Seedr.cc account;
       once downloaded, the Seedr file link is returned as the stream.
    5. Playback goes Seedr -> user's device DIRECTLY. This server only
       returns links and 302 redirects - no video bytes pass through it,
       so Render's bandwidth stays tiny.

Config is passed by Stremio inside the addon URL as base64url JSON:
  https://<host>/<config>/manifest.json
"""

import base64
import hashlib
import json
import os
import re
import time

import requests
from flask import Flask, jsonify, redirect, request

import jackett
from seedr import Seedr

app = Flask(__name__)

# Stremio Web runs in a browser, so its manifest/stream/catalog fetches
# are subject to CORS - a plain browser visit is not. Without these
# headers Stremio Web reports "failed to fetch manifest" even though
# the same URL loads fine when opened directly.
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

ADDON_ID = "com.hari.streamhub"
ADDON_NAME = "StreamHub"
ADDON_VERSION = "1.0.0"

# magnet btih -> (seedr_file_id, file_name). Repeat requests are instant.
SEEDR_CACHE = {}

# Files THIS backend added, oldest first. Only these are ever auto-deleted
# - the user's other Seedr files are never touched.
# Best-effort JSON on disk (Render free disk is ephemeral; the in-memory
# cache above is the fast path, this file survives restarts within one
# instance lifetime).
TRACKED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "seedr_tracked.json")


def load_tracked():
    try:
        with open(TRACKED_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_tracked(items):
    try:
        with open(TRACKED_PATH, "w") as f:
            json.dump(items, f)
    except Exception:
        pass


def ensure_space(seedr, needed_bytes, exclude_btih=None):
    """Free space for a new download by deleting the OLDEST backend-added
    files first. Only runs when Seedr reports (or we can compute) that
    free space is short. Returns True if there should now be room."""
    if not needed_bytes:
        return True
    tracked = load_tracked()
    if not tracked:
        return True  # nothing of ours to delete; let Seedr decide
    used, total = seedr.user_space()
    if total:
        free = total - used
        if free >= needed_bytes:
            return True
    else:
        # Total unknown: use tracked sizes as the used estimate and only
        # act when tracked usage alone already exceeds 3.5 GB (a 4 GB
        # plan's practical ceiling) - otherwise stay out of the way.
        used = sum(t.get("size", 0) for t in tracked)
        if used + needed_bytes <= 3.5 * 1024 ** 3:
            return True
        total = None  # unknown; free recomputed below per deletion
    # Delete oldest first until there is room (or nothing left to delete).
    kept = []
    for entry in tracked:
        if exclude_btih and entry.get("btih") == exclude_btih:
            kept.append(entry)
            continue
        used_now, total_now = seedr.user_space()
        if total_now and (total_now - used_now) >= needed_bytes:
            kept.append(entry)
            continue
        if seedr.delete_file(entry["file_id"]):
            SEEDR_CACHE.pop(entry.get("btih"), None)
            continue  # deleted: do not keep
        kept.append(entry)  # delete failed: keep the entry
    save_tracked(kept)
    return True


def btih_of(magnet):
    m = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", magnet or "")
    return m.group(1).lower() if m else hashlib.sha1(
        magnet.encode()).hexdigest()


def encode_config(cfg):
    raw = json.dumps(cfg, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_config(s):
    try:
        s = s + "=" * (-len(s) % 4)
        return json.loads(base64.urlsafe_b64decode(s))
    except Exception:
        return {}


def build_manifest(configured=False):
    # The plain manifest needs configuration, but a manifest fetched
    # through an already-configured URL must NOT say so - otherwise
    # Stremio sends the user back to configure again, forever:
    # configure -> install link -> configure -> install link...
    return {
        "id": ADDON_ID,
        "version": ADDON_VERSION,
        "name": ADDON_NAME,
        "description": "Fast streaming addon - torrent results via your Jackett, "
                       "downloads via your Seedr.cc account.",
        "types": ["movie", "series"],
        "resources": [
            {"name": "stream", "types": ["movie", "series"],
             "idPrefixes": ["tt"]},
            {"name": "catalog", "types": ["movie", "series"],
             "idPrefixes": ["streamhub"]},
        ],
        "catalogs": [
            {"type": "movie", "id": "streamhub-trending",
             "name": "StreamHub Trending"},
            {"type": "series", "id": "streamhub-trending",
             "name": "StreamHub Trending"},
        ],
        "behaviorHints": {"configurable": True,
                          "configurationRequired": not configured},
        "config": [
            {"key": "jackettUrl", "type": "text", "required": True,
             "title": "Jackett URL (e.g. http://your-jackett:9117)"},
            {"key": "jackettApiKey", "type": "text", "required": True,
             "title": "Jackett API key"},
            {"key": "seedrEmail", "type": "text", "required": True,
             "title": "Seedr.cc email"},
            {"key": "seedrPassword", "type": "password", "required": True,
             "title": "Seedr.cc password"},
            {"key": "maxResultsPerQuality", "type": "number", "default": 5,
             "title": "Max results per quality"},
            {"key": "qualityFilter", "type": "multiselect",
             "options": ["4k", "1080p", "720p", "480p"],
             "default": ["4k", "1080p", "720p"],
             "title": "Qualities to include"},
            {"key": "excludeCam", "type": "checkbox", "default": True,
             "title": "Exclude CAM / screener releases"},
            {"key": "sortBy", "type": "select",
             "options": ["quality", "seeders", "size"], "default": "quality",
             "title": "Sort streams by"},
        ],
    }


def cinemeta_title(media_type, imdb_id):
    """Resolve an IMDb id to (title, year, season, episode) via Cinemeta."""
    base_id = imdb_id.split(":")[0]
    try:
        r = requests.get(
            f"https://v3-cinemeta.strem.io/meta/{media_type}/{base_id}.json",
            timeout=10)
        meta = r.json().get("meta", {})
        season = episode = None
        parts = imdb_id.split(":")
        if len(parts) >= 3:
            season, episode = int(parts[1]), int(parts[2])
        return meta.get("name"), meta.get("year"), season, episode
    except Exception:
        return None, None, None, None


@app.route("/")
def index():
    return ("<h2>StreamHub</h2><p>Stremio addon backend is running.</p>"
            "<p><a href='/configure'>Configure &amp; install</a></p>")


@app.route("/manifest.json")
def manifest_plain():
    return jsonify(build_manifest())


@app.route("/<config>/manifest.json")
def manifest_cfg(config):
    return jsonify(build_manifest(configured=True))


@app.route("/configure", methods=["GET", "POST"])
@app.route("/<config>/configure", methods=["GET", "POST"])
def configure(config=None):
    import html as _html
    # Pre-fill from the installed config, so fixing one field (e.g. the
    # Jackett URL) does not wipe the rest - notably the Seedr password.
    defaults = decode_config(config) if config else {}
    if request.method == "POST":
        cfg = {
            "jackettUrl": request.form.get("jackettUrl", "").strip(),
            "jackettApiKey": request.form.get("jackettApiKey", "").strip(),
            "seedrEmail": request.form.get("seedrEmail", "").strip(),
            "seedrPassword": request.form.get("seedrPassword", ""),
            "maxResultsPerQuality": int(
                request.form.get("maxResultsPerQuality", 5) or 5),
            "qualityFilter": request.form.getlist("qualityFilter") or
                             ["4k", "1080p", "720p"],
            "excludeCam": bool(request.form.get("excludeCam")),
            "sortBy": request.form.get("sortBy", "quality"),
        }
        host = request.host_url.rstrip("/")
        encoded = encode_config(cfg)
        install_url = f"{host}/{encoded}/manifest.json"
        return (f"<h2>Done!</h2><p>Install this URL in Stremio "
                f"(Addons &gt; Add addon):</p><p><code>{install_url}</code></p>"
                f"<p><a href='stremio://{request.host}/{encoded}/manifest.json'>"
                f"Open in Stremio</a></p>")
    q = defaults.get("qualityFilter") or ["4k", "1080p", "720p"]
    def chk(v):
        return "checked" if v in q else ""
    return f"""
    <h2>StreamHub - Configure</h2>
    <form method="post">
      Jackett URL: <input name="jackettUrl" size="50" required value="{_html.escape(defaults.get('jackettUrl', ''), quote=True)}"><br><br>
      Jackett API key: <input name="jackettApiKey" size="50" required value="{_html.escape(defaults.get('jackettApiKey', ''), quote=True)}"><br><br>
      Seedr email: <input name="seedrEmail" size="40" required value="{_html.escape(defaults.get('seedrEmail', ''), quote=True)}"><br><br>
      Seedr password: <input type="password" name="seedrPassword" size="40" required value="{_html.escape(defaults.get('seedrPassword', ''), quote=True)}"><br><br>
      Max results per quality: <input name="maxResultsPerQuality" value="{defaults.get('maxResultsPerQuality', 5)}" size="5"><br><br>
      Qualities:
        <label><input type="checkbox" name="qualityFilter" value="4k" {chk('4k')}> 4K</label>
        <label><input type="checkbox" name="qualityFilter" value="1080p" {chk('1080p')}> 1080p</label>
        <label><input type="checkbox" name="qualityFilter" value="720p" {chk('720p')}> 720p</label>
        <label><input type="checkbox" name="qualityFilter" value="480p" {chk('480p')}> 480p</label><br><br>
      <label><input type="checkbox" name="excludeCam" {'checked' if defaults.get('excludeCam', True) else ''}> Exclude CAM / screener</label><br><br>
      Sort by:
        <select name="sortBy">
          <option value="quality" {'selected' if defaults.get('sortBy', 'quality') == 'quality' else ''}>Quality</option>
          <option value="seeders" {'selected' if defaults.get('sortBy') == 'seeders' else ''}>Seeders</option>
          <option value="size" {'selected' if defaults.get('sortBy') == 'size' else ''}>Size</option>
        </select><br><br>
      <button type="submit">Generate install link</button>
    </form>
    <p><small>Your Seedr password is only used to talk to Seedr's API from
    this server, and is stored only inside your Stremio install link.
    Use this addon for your own account only.</small></p>
    """


@app.route("/<config>/manifest.json/configure")
def configure_manifest_alias(config):
    """Some Stremio clients append /configure to the full manifest URL
    instead of replacing manifest.json with it - send them to the real
    configure page for this config."""
    return redirect(f"/{config}/configure", code=302)


@app.route("/<config>/catalog/<media_type>/<catalog_id>.json")
@app.route("/<config>/catalog/<media_type>/<catalog_id>/<extra>.json")
def catalog(config, media_type, catalog_id, extra=None):
    """The manifest declares a Trending catalog; there is no catalog
    data behind it, so return an empty list instead of a 404 (a 404
    here breaks the addon view in some Stremio clients)."""
    return jsonify({"metas": []})


@app.route("/<config>/stream/<media_type>/<media_id>.json")
def stream(config, media_type, media_id):
    cfg = decode_config(config)
    if not cfg.get("jackettUrl") or not cfg.get("seedrEmail"):
        return jsonify({"streams": []})
    title, year, season, episode = cinemeta_title(media_type, media_id)
    if not title:
        return jsonify({"streams": []})
    if media_type == "series" and season is not None:
        query = f"{title} S{season:02d}E{episode:02d}"
    else:
        query = f"{title} {year}" if year else title

    results = jackett.search(cfg["jackettUrl"], cfg.get("jackettApiKey", ""),
                             query, media_type)
    qualities = set(q.lower() for q in cfg.get("qualityFilter", [])) or None
    filtered = []
    for r in results:
        q, src = jackett.parse_quality(r["title"])
        if qualities and q not in qualities:
            continue
        if cfg.get("excludeCam") and src.upper() in ("CAM", "HDCAM"):
            continue
        r["quality"] = q
        r["source"] = src
        filtered.append(r)
    sort_by = cfg.get("sortBy", "quality")
    if sort_by == "seeders":
        filtered.sort(key=lambda x: x["seeders"], reverse=True)
    elif sort_by == "size":
        filtered.sort(key=lambda x: x["size"], reverse=True)
    else:
        order = {"4k": 0, "2160p": 0, "1080p": 1, "720p": 2, "480p": 3}
        filtered.sort(key=lambda x: (order.get(x["quality"], 9),
                                     -x["seeders"]))
    per_quality = int(cfg.get("maxResultsPerQuality", 5) or 5)
    counts, picked = {}, []
    for r in filtered:
        if counts.get(r["quality"], 0) >= per_quality:
            continue
        counts[r["quality"]] = counts.get(r["quality"], 0) + 1
        picked.append(r)

    seedr = Seedr(cfg["seedrEmail"], cfg.get("seedrPassword", ""))
    host = request.host_url.rstrip("/")
    streams = []
    budget_end = time.time() + 25  # shared Seedr wait budget per request
    for r in picked:
        btih = btih_of(r["magnet"])
        cached = SEEDR_CACHE.get(btih)
        if cached:
            file_id, fname = cached
        else:
            # Re-verify a previously tracked file (survives restarts).
            tracked = [t for t in load_tracked() if t.get("btih") == btih]
            if tracked and seedr.find_file(tracked[0]["file_id"]):
                file_id, fname = (tracked[0]["file_id"],
                                  tracked[0].get("name", ""))
                SEEDR_CACHE[btih] = (file_id, fname)
            else:
                # New download: if space is short, auto-delete the oldest
                # backend-added files first (user's other files untouched).
                ensure_space(seedr, r["size"], exclude_btih=btih)
                remaining = budget_end - time.time()
                if remaining < 4:
                    break
                resolved = seedr.resolve(r["magnet"], r["title"],
                                         deadline_seconds=remaining)
                if not resolved:
                    continue
                file_id, fname = resolved
                SEEDR_CACHE[btih] = (file_id, fname)
                tracked_all = [t for t in load_tracked()
                               if t.get("btih") != btih]
                tracked_all.append({"btih": btih, "file_id": file_id,
                                    "name": fname, "size": r["size"],
                                    "added_at": time.time()})
                save_tracked(tracked_all)
        play = f"{host}/{config}/play/{file_id}/hls"
        streams.append({
            "name": f"StreamHub {r['quality']}",
            "title": (f"{r['title']}\n"
                      f"{jackett.fmt_size(r['size'])} | "
                      f"{r['seeders']} seeders | via Seedr"),
            "url": play,
            "behaviorHints": {"bingeGroup": f"streamhub-{r['quality']}"},
        })
    return jsonify({"streams": streams})


@app.route("/<config>/play/<file_id>")
@app.route("/<config>/play/<file_id>/hls")
def play(config, file_id):
    """302 redirect to Seedr - no video bytes pass through this server."""
    from urllib.parse import quote
    cfg = decode_config(config)
    email = quote(cfg.get("seedrEmail", ""), safe="")
    password = quote(cfg.get("seedrPassword", ""), safe="")
    path = f"/rest/file/{file_id}"
    if request.path.endswith("/hls"):
        path += "/hls"
    return redirect(f"https://{email}:{password}@www.seedr.cc{path}", code=302)


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
