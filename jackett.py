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

import re
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


def search(jackett_url, api_key, query, media_type, timeout=15):
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
    """(quality, source) e.g. ('1080p', 'BluRay')."""
    t = title or ""
    m = re.search(r"(2160p|1080p|720p|480p|360p)", t, re.I)
    quality = m.group(1).lower() if m else "unknown"
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
