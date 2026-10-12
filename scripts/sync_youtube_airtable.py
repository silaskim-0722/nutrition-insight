#!/usr/bin/env python3
"""Sync recent @bubudiet YouTube uploads to Airtable. Standard library only."""
import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET

HANDLE = os.getenv("YOUTUBE_HANDLE", "bubudiet").lstrip("@")
BASE = os.getenv("AIRTABLE_BASE_ID", "appGwL6PPsIsZIS9a")
TABLE = os.getenv("AIRTABLE_TABLE_ID", "tbljINNeF0EmAMPJg")
TOKEN = os.environ["AIRTABLE_TOKEN"]
UA = "Mozilla/5.0 (compatible; YouTubeAirtableSync/1.0)"

def request(url, method="GET", payload=None, auth=False):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + TOKEN
    if payload is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=35) as resp:
        return resp.read().decode("utf-8")

def channel_id():
    url = "https://www.youtube.com/@" + urllib.parse.quote(HANDLE)
    html = request(url)
    for pattern in [r'<meta itemprop="channelId" content="(UC[A-Za-z0-9_-]{22})"',
                    r'"channelId":"(UC[A-Za-z0-9_-]{22})"',
                    r'www.youtube.com/channel/(UC[A-Za-z0-9_-]{22})']:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    raise RuntimeError("Could not determine YouTube channel ID for @" + HANDLE)

def feed(cid):
    xml = request("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid)
    root = ET.fromstring(xml)
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    videos = []
    for entry in root.findall("a:entry", ns):
        vid = entry.findtext("yt:videoId", namespaces=ns)
        if not vid: continue
        title = entry.findtext("a:title", default="", namespaces=ns)
        published = entry.findtext("a:published", default="", namespaces=ns)
        videos.append({"id": vid, "title": title, "published": published,
                       "url": "https://www.youtube.com/watch?v=" + vid})
    return videos


def uploads_via_api(cid):
    """Use the complete uploads playlist when a YouTube Data API key is configured."""
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        return []
    playlist = "UU" + cid[2:]
    videos, token = [], ""
    while True:
        params = {"part": "snippet", "playlistId": playlist, "maxResults": 50, "key": key}
        if token: params["pageToken"] = token
        data = json.loads(request("https://www.googleapis.com/youtube/v3/playlistItems?" +
                                  urllib.parse.urlencode(params)))
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            vid = sn.get("resourceId", {}).get("videoId")
            if vid and sn.get("title") not in ("Private video", "Deleted video"):
                videos.append({"id": vid, "title": sn.get("title", ""),
                               "published": sn.get("publishedAt", ""),
                               "url": "https://www.youtube.com/watch?v=" + vid})
        token = data.get("nextPageToken", "")
        if not token: break
    return videos

def known_video(cid):
    """One-time recovery for the user-supplied video missing from the 15-entry RSS feed."""
    vid = "tRNVu1W-IRs"
    url = "https://www.youtube.com/watch?v=" + vid
    info = json.loads(request("https://www.youtube.com/oembed?" +
                              urllib.parse.urlencode({"url": url, "format": "json"})))
    author = info.get("author_url", "")
    if not author or not (author.rstrip("/").endswith("/@" + HANDLE) or
                          author.rstrip("/").endswith("/channel/" + cid)):
        print("Skipping recovery video: channel ownership could not be verified")
        return None
    return {"id": vid, "title": info.get("title", ""), "published": "", "url": url}

def airtable_url(params=None):
    u = "https://api.airtable.com/v0/" + BASE + "/" + TABLE
    return u + ("?" + urllib.parse.urlencode(params) if params else "")

def existing(video_id):
    # Search URL field for exact YouTube ID. Avoid downloading whole table.
    formula = "FIND('" + video_id + "', {url}) > 0"
    data = json.loads(request(airtable_url({"filterByFormula": formula, "maxRecords": 1}), auth=True))
    return bool(data.get("records"))


def enrich(video):
    """Best-effort public metadata and captions; never overwrite existing data."""
    fields = {}
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": False, "ignoreerrors": False,
                "socket_timeout": 15, "retries": 1}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video["url"], download=False)
        if not info: return fields
        if info.get("channel_id") and info["channel_id"] != channel_id_cache:
            print("Skip metadata: channel mismatch", video["id"])
            return fields
        desc = info.get("description") or ""
        if desc: fields["설명"] = desc[:95000]
        tags = info.get("tags") or []
        hashes = re.findall(r"(?<!\\w)#[\\w가-힣]+", desc)
        if tags or hashes: fields["hashtags"] = ", ".join(dict.fromkeys(hashes + tags))[:95000]
        visibility = info.get("availability")
        if visibility in ("public", "unlisted", "private"):
            fields["privacyStatus"] = visibility
        if not video.get("published") and info.get("upload_date"):
            d = info["upload_date"]
            if re.fullmatch(r"\\d{8}", d):
                fields["업로드일"] = d[:4] + "-" + d[4:6] + "-" + d[6:] + "T00:00:00.000Z"
        captions = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        for sources in (captions, auto):
            for lang in ("ko", "ko-KR", "en"):
                tracks = sources.get(lang) or []
                track = next((x for x in tracks if x.get("ext") == "json3" and x.get("url")), None)
                if not track: continue
                try:
                    payload = json.loads(request(track["url"]))
                    parts = []
                    for event in payload.get("events", []):
                        line = "".join(seg.get("utf8", "") for seg in event.get("segs", []))
                        if line.strip(): parts.append(line.strip())
                    transcript = "\\n".join(parts).strip()
                    if transcript:
                        fields["transcript"] = transcript[:95000]
                        return fields
                except Exception as exc:
                    print("Caption unavailable:", video["id"], type(exc).__name__)
    except Exception as exc:
        print("Metadata unavailable:", video["id"], type(exc).__name__, str(exc)[:150])
    return fields

def main():
    global channel_id_cache
    cid = channel_id()
    channel_id_cache = cid
    videos = feed(cid)
    print("Channel:", cid, "Feed videos:", len(videos))
    api_videos = uploads_via_api(cid)
    if api_videos: print("Uploads playlist videos:", len(api_videos))
    by_id = {v["id"]: v for v in videos}
    by_id.update({v["id"]: v for v in api_videos})
    if "tRNVu1W-IRs" not in by_id:
        try:
            recovered = known_video(cid)
            if recovered: by_id[recovered["id"]] = recovered
        except Exception as exc:
            print("Recovery lookup unavailable:", exc)
    videos = list(by_id.values())
    if not videos: raise RuntimeError("YouTube feed returned no videos")
    added, updated = 0, 0
    for video in reversed(videos):
        formula = "FIND('" + video["id"] + "', {url}) > 0"
        matches = json.loads(request(airtable_url({"filterByFormula": formula, "maxRecords": 1}), auth=True)).get("records", [])
        old = matches[0] if matches else None
        fields = {"title": video["title"], "url": video["url"],
                  "썸네일": "https://i.ytimg.com/vi/" + video["id"] + "/hqdefault.jpg",
                  **({"업로드일": video["published"]} if video.get("published") else {})}
        if not old or any(not old.get("fields", {}).get(k) for k in ("설명", "hashtags", "transcript", "privacyStatus")):
            fields.update(enrich(video))
        if old:
            patch = {k: v for k, v in fields.items() if v and not old.get("fields", {}).get(k)}
            if patch:
                request(airtable_url() + "/" + old["id"], "PATCH", {"fields": patch}, auth=True)
                updated += 1
                print("Filled missing fields:", video["id"], list(patch))
            else:
                print("Exists:", video["id"])
        else:
            result = json.loads(request(airtable_url(), "POST", {"fields": fields}, auth=True))
            print("Created:", video["id"], result.get("id"))
            added += 1
        time.sleep(0.25)
    print("Done. Added", added, "videos; enriched", updated, "existing records.")

if __name__ == "__main__":
    try: main()
    except Exception as e:
        print("SYNC FAILED:", str(e), file=sys.stderr)
        sys.exit(1)
