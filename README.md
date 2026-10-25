# StreamHub — Stremio addon backend (Seedr edition)

A small Flask server that turns your Stremio into a fast streamer:

```
Stremio -> this server -> Jackett (search) -> Seedr.cc (download)
Seedr -> your device (video, direct — no bytes through this server)
```

## What you need

1. **A Jackett instance** — you run it and configure indexers there.
   (Docker image `lscr.io/linuxserver/jackett` works on any VPS/PC.)
2. **A Seedr.cc account** — email + password go into the addon config.
3. **Render** (free tier works) or any Python host.

## Deploy on Render

1. Push this folder to a GitHub repo.
2. Render Dashboard -> New -> Web Service -> connect the repo.
   `render.yaml` is included, so Render picks up the settings automatically.
3. Wait for deploy, open `https://<your-service>.onrender.com/configure`.
4. Fill in: Jackett URL + API key, Seedr email + password, quality prefs.
5. Copy the install link it generates into Stremio (Addons -> Add addon),
   or tap "Open in Stremio".

## How streaming works

- Stremio asks `/stream/movie/tt1234567.json`.
- The server resolves the title via Cinemeta (public, no key).
- Jackett is searched; results are filtered (quality, CAM exclusion, sort).
- Each magnet is added to YOUR Seedr account. If the file is already in
  Seedr, the link returns instantly. First-time torrents wait (bounded
  ~25s per stream request) while Seedr downloads; re-open the stream
  list after a minute if a torrent is still downloading.
- The stream URL is a redirect to Seedr (`/rest/file/<id>/hls`) — video
  goes Seedr -> device directly. This server never proxies video, so
  Render's bandwidth stays tiny.

## Files

- `app.py` — Flask app: manifest, configure page, stream, play redirect
- `seedr.py` — Seedr REST v1 client (Basic auth, magnet add, folder, HLS)
- `jackett.py` — Jackett Torznab search client
- `requirements.txt`, `render.yaml` — deploy files

## Jackett on Render (second service, no PC needed)

`render.yaml` deploys two services from this repo:

1. **streamhub** — the Stremio addon backend (Python).
2. **streamhub-jackett** — Jackett (Docker), the torrent search proxy.

After both are live:

1. Open the Jackett service URL (`https://streamhub-jackett.onrender.com`)
   — first open takes ~30–60 s because free services sleep when idle.
2. **Add indexers** (1337x, Pirate Bay, etc. — whichever work for you).
3. **Set an admin password** in Jackett (bottom of the settings page) so
   strangers can't open your Jackett dashboard. The Torznab API key
   still works for the backend.
4. Copy the **API key** (top-right of the Jackett dashboard).
5. Open the StreamHub `/configure` page and set:
   - **Jackett URL:** `https://streamhub-jackett.onrender.com`
   - **Jackett API key:** the key from step 4.

Caveats:

- Free Render services **sleep after ~15 min idle** — the first search
  after a sleep wakes both services and takes ~30–60 s.
- Free Render has **no persistent disk**: if the Jackett service is
  redeployed or restarted, its config (indexers, API key) can reset and
  you will need to re-add indexers and paste the new API key into
  `/configure`. The backend's own files are unaffected.

## Auto-cleanup (space management)

When you click a new movie, the backend checks Seedr space first:

- If there is room, the new torrent downloads normally.
- If space is short, it **automatically deletes the oldest files that
  this backend added** (oldest first) until there is room.
- It **never deletes your other Seedr files** — only files this addon
  added are eligible for auto-delete.

So back-to-back watching just works: old movies clear themselves out
when a new one needs the room. No manual deleting required.

## Caveats (read first)

- Seedr REST v1 uses Basic auth (email:password). Per Seedr's own docs
  this is only secure for personal use — run this addon for yourself
  only, and don't share your install link publicly (it contains your
  config, including your Seedr password).
- Seedr free accounts have small storage (~2 GB) — large movies may not
  fit; a paid Seedr plan removes that limit.
- Seedr downloads fresh torrents (no Real-Debrid-style instant cache),
  so first plays of unpopular torrents take time.
- Render free services sleep after ~15 min idle; the first request after
  sleep takes ~30-60s to wake.
