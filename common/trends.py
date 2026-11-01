"""Trending songs from free sources, merged into one ranked list (any language)."""

import html
import logging
import os
import re

import requests
import urllib3.util.connection as _conn

_conn.HAS_IPV6 = False  # IPv4 only (IPv6 can hang on Render)

log = logging.getLogger(__name__)

YT_REGIONS = os.getenv("TREND_REGIONS", "IN,US,GB,KR,JP,BR,MX,ES,NG,ID,PH,PK,TR,FR,DE,AE").split(",")
APPLE_REGIONS = os.getenv("APPLE_REGIONS", "in,us,gb,kr,br,mx").split(",")
UA = {"User-Agent": "Mozilla/5.0 (trend-song-bot)"}

NOISE = re.compile(r"\(.*?\)|\[.*?\]|official|video|lyrical|lyrics?|audio|full song|music video"
                   r"|4k|hd|\bft\.?.*|\bfeat\.?.*", re.I)


def key(title: str) -> str:
    """A rough id for a song, so the same song from different charts merges."""
    t = title.split("|")[0].split(" - ")[-1] if " - " in title.split("|")[0] else title.split("|")[0]
    t = NOISE.sub(" ", t.lower())
    return re.sub(r"[^\w]+", " ", t).strip()[:40]


def _youtube() -> list[list[dict]]:
    out = []
    k = os.getenv("YT_API_KEY")
    if not k:
        return out
    for region in YT_REGIONS:
        try:
            r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20, params={
                "part": "snippet", "chart": "mostPopular", "videoCategoryId": "10",
                "regionCode": region, "maxResults": 25, "key": k})
            r.raise_for_status()
            out.append([{"title": i["snippet"]["title"], "artist": i["snippet"]["channelTitle"],
                         "lang": i["snippet"].get("defaultAudioLanguage", ""),
                         "tags": i["snippet"].get("tags", [])[:8],
                         "source": f"YouTube {region}"} for i in r.json().get("items", [])])
        except Exception as exc:
            log.info("YouTube %s: %s", region, exc)
    return out


def _lastfm() -> list[list[dict]]:
    out = []
    k = os.getenv("LASTFM_API_KEY")
    if not k:
        return out
    for params, name in (({"method": "chart.gettoptracks"}, "Last.fm global"),
                         ({"method": "geo.gettoptracks", "country": "India"}, "Last.fm India")):
        try:
            r = requests.get("https://ws.audioscrobbler.com/2.0/", timeout=20,
                             params={**params, "api_key": k, "format": "json", "limit": 50})
            r.raise_for_status()
            tracks = r.json().get("tracks", {}).get("track", [])
            out.append([{"title": t["name"], "artist": t["artist"]["name"], "source": name}
                        for t in tracks])
        except Exception as exc:
            log.info("%s: %s", name, exc)
    return out


def _apple() -> list[list[dict]]:
    out = []
    for cc in APPLE_REGIONS:
        try:
            r = requests.get(f"https://rss.marketingtools.apple.com/api/v2/{cc}/music/most-played/"
                             "50/songs.json", timeout=20, headers=UA)
            r.raise_for_status()
            out.append([{"title": s["name"], "artist": s["artistName"],
                         "tags": [g["name"] for g in s.get("genres", [])][:3],
                         "source": f"Apple Music {cc.upper()}"}
                        for s in r.json()["feed"]["results"]])
        except Exception as exc:
            log.info("Apple %s: %s", cc, exc)
    return out


def _deezer() -> list[list[dict]]:
    try:
        r = requests.get("https://api.deezer.com/chart/0/tracks?limit=50", timeout=20)
        r.raise_for_status()
        return [[{"title": t["title"], "artist": t["artist"]["name"], "source": "Deezer"}
                 for t in r.json()["data"]]]
    except Exception as exc:
        log.info("Deezer: %s", exc)
        return []


def _kworb() -> list[list[dict]]:
    out = []
    pat = re.compile(r'<a href="\.\./artist/[^"]+">([^<]+)</a>\s*-\s*<a href="\.\./track/[^"]+">'
                     r'([^<]+)</a>')
    for page, name in (("global_daily", "Spotify global"), ("in_daily", "Spotify India")):
        try:
            r = requests.get(f"https://kworb.net/spotify/country/{page}.html", timeout=20,
                             headers=UA)
            r.raise_for_status()
            rows = pat.findall(r.text)[:50]
            out.append([{"title": html.unescape(t), "artist": html.unescape(a), "source": name}
                        for a, t in rows])
        except Exception as exc:
            log.info("%s: %s", name, exc)
    return out


def trending() -> list[dict]:
    """All sources merged; best first. Each song: title, artist, sources, tags, score."""
    merged: dict[str, dict] = {}
    for fetch in (_youtube, _lastfm, _apple, _deezer, _kworb):
        for chart in fetch():
            n = len(chart) or 1
            for rank, song in enumerate(chart):
                k = key(song["title"])
                if not k:
                    continue
                item = merged.setdefault(k, {"key": k, "title": song["title"],
                                             "artist": song["artist"], "sources": [],
                                             "tags": [], "lang": "", "score": 0.0})
                item["score"] += 1 - rank / n
                item["sources"].append(f"{song['source']} #{rank + 1}")
                item["tags"] = list(dict.fromkeys(item["tags"] + song.get("tags", [])))[:10]
                item["lang"] = item["lang"] or song.get("lang", "")
    songs = sorted(merged.values(), key=lambda s: s["score"], reverse=True)
    log.info("Trends: %d songs", len(songs))
    return songs
