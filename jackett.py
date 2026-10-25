"""Jackett Torznab client.

Jackett is a separate, user-configured search aggregator: the user runs
their own Jackett instance and configures whichever indexers they want
there. This addon only talks to Jackett's standard Torznab API - it
contains no indexer list of its own.

Torznab search:
  GET {jackett}/api/v2.0/indexers/all/results/torznab/
      ?apikey=KEY&t=search&q=QUERY&cat=2000,2010,2040
Categories: 2000 = movies, 5000 = TV.
"""

import hashlib
import re
from urllib.parse import quote
import requests
import xml.etree.ElementTree as ET

NS = {"torznab": "http://torznab.com/schemas/2015/feed"}
MOVIE_CATS = "2000,2010,2030,2040,2045,2050"
TV_CATS = "5000,5010,5020,5030,5040,5045"


def _attr(item, name):
    for a in item.findall("torznab:attr", NS):
        if a.get("name") == name:
            return a.get("value")
    return None


def _bdecode(data, i=0):
    """Minimal bencode decoder returning (value, next_index).
    Dict keys are bytes; the 'info' dict's raw span is what matters
    for the infohash, so dict parsing keeps start/end offsets."""
    if data[i:i+1] == b"i":
        j = data.index(b"e", i)
        return int(data[i+1:j]), j + 1
    if data[i:i+1] == b"l":
        out, i = [], i + 1
        while data[i:i+1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if data[i:i+1] == b"d":
        out, spans, i = {}, {}, i + 1
        while data[i:i+1] != b"e":
            k, i = _bdecode(data, i)
            start = i
            v, i = _bdecode(data, i)
            out[k] = v
            spans[k] = (start, i)
        out[b"__spans__"] = spans
        return out, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j+1:j+1+n], j + 1 + n


def torrent_to_magnet(data, title=""):
    """Convert .torrent bytes to a magnet link via the infohash
    (SHA-1 of the raw bencoded 'info' dict). Returns None if the
    file is not a valid torrent. Trackers from announce/announce-list
    are kept so Seedr can find peers."""
    try:
        top, _ = _bdecode(data)
        spans = top.get(b"__spans__", {})
        start, end = spans[b"info"]
        infohash = hashlib.sha1(data[start:end]).hexdigest()
        trackers = []
        if isinstance(top.get(b"announce"), bytes):
            trackers.append(top[b"announce"].decode("utf-8", "ignore"))
        al = top.get(b"announce-list")
        if isinstance(al, list):
            for tier in al:
                if isinstance(tier, list):
                    for t in tier:
                        if isinstance(t, bytes):
                            trackers.append(t.decode("utf-8", "ignore"))
        mag = f"magnet:?xt=urn:btih:{infohash}"
        if title:
            mag += f"&dn={quote(title)}"
        for t in dict.fromkeys(trackers):
            if t:
                mag += f"&tr={quote(t, safe='')}"
        return mag
    except Exception:
        return None


def _magnet_from_link(link, title):
    """Fetch a .torrent file link and convert it to a magnet.
    Returns None on any failure - the caller then skips the item."""
    try:
        r = requests.get(link, timeout=10,
                         headers={"User-Agent": "StreamHub"})
        r.raise_for_status()
        if len(r.content) > 2 * 1024 * 1024:
            return None
        return torrent_to_magnet(r.content, title)
    except Exception:
        return None


def search(jackett_url, api_key, query, media_type, timeout=30):
    """Returns list of dicts: title, magnet, size, seeders."""
    base = jackett_url.rstrip("/")
    params = {
        "apikey": api_key,
        "t": "search",
        "q": query,
        "cat": TV_CATS if media_type == "series" else MOVIE_CATS,
    }
    try:
        r = requests.get(f"{base}/api/v2.0/indexers/all/results/torznab/",
                         params=params, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return []
    results = []
    try:
        root = ET.fromstring(r.content)
    except Exception:
        return []
    for item in root.findall("./channel/item"):
        title_el = item.find("title")
        link_el = item.find("link")
        if title_el is None or link_el is None:
            continue
        title = title_el.text or ""
        link = (link_el.text or "").strip()
        magnet = link
        if not magnet.startswith("magnet:"):
            # Torznab 'magneturl' attr holds the magnet when link is a file
            magnet = _attr(item, "magneturl") or ""
        if not magnet.startswith("magnet:"):
            # Some indexers only give a .torrent file link - convert it
            # to a magnet via its infohash instead of dropping the item.
            magnet = _magnet_from_link(link, title) or ""
        if not magnet.startswith("magnet:"):
            continue
        try:
            seeders = int(_attr(item, "seeders") or 0)
        except ValueError:
            seeders = 0
        try:
            size = int(_attr(item, "size") or 0)
        except ValueError:
            size = 0
        results.append({
            "title": title,
            "magnet": magnet,
            "size": size,
            "seeders": seeders,
        })
    results.sort(key=lambda x: x["seeders"], reverse=True)
    return results


def parse_quality(title):
    """(quality, source) e.g. ('1080p', 'BluRay'). 2160p and 4K are
    normalised to '4k' so they match the config's quality filter,
    which offers '4k' (previously 2160p titles were silently dropped
    by the filter even when the user selected 4K)."""
    t = title or ""
    m = re.search(r"(2160p|4k|1080p|720p|480p|360p)", t, re.I)
    quality = m.group(1).lower() if m else "unknown"
    if quality == "2160p":
        quality = "4k"
    for src in ("BluRay", "WEB-DL", "WEBRip", "HDRip", "HDCAM", "CAM"):
        if re.search(src, t, re.I):
            return quality, src
    return quality, ""


def fmt_size(num):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"
