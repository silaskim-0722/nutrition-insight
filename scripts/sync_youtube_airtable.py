#!/usr/bin/env python3
"""Fast, idempotent official YouTube API -> Airtable synchronization."""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

CHANNEL_ID = "UCJ1SYnWqyBpnVT41ubFKN7Q"
BASE_ID = "appGwL6PPsIsZIS9a"
TABLE_ID = "tbljINNeF0EmAMPJg"
RETRYABLE = {429, 500, 502, 503, 504}
VIDEO_RE = re.compile(r"(?:youtu\.be/|[?&]v=|/shorts/)([A-Za-z0-9_-]{11})(?:\b|$)")
HASH_RE = re.compile(r"(?<![\w#])#[\w가-힣]+")

class SyncError(RuntimeError): pass

def required_env(name):
    value = os.getenv(name, "").strip()
    if not value: raise SyncError(f"Missing required secret: {name}")
    return value

def request_json(url, *, method="GET", headers=None, payload=None, service="API", retries=3):
    headers = {"Accept": "application/json", "User-Agent": "NutritionInsightSync/2.0", **(headers or {})}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE and attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8)); continue
            raise SyncError(f"{service} request failed with HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8)); continue
            raise SyncError(f"{service} request failed: {type(exc).__name__}") from None

def oauth_token():
    data = urllib.parse.urlencode({"client_id": required_env("YOUTUBE_CLIENT_ID"),
        "client_secret": required_env("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": required_env("YOUTUBE_REFRESH_TOKEN"), "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response: result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SyncError(f"YouTube OAuth authentication failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SyncError(f"YouTube OAuth authentication failed: {type(exc).__name__}") from None
    if not result.get("access_token"): raise SyncError("YouTube OAuth response has no access token")
    return result["access_token"]

class YouTube:
    def __init__(self, token): self.headers = {"Authorization": "Bearer " + token}
    def api(self, endpoint, params):
        url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urllib.parse.urlencode(params)
        return request_json(url, headers=self.headers, service="YouTube API")
    def uploads_playlist(self):
        data = self.api("channels", {"part": "contentDetails", "mine": "true"})
        items = data.get("items", [])
        if len(items) != 1 or items[0].get("id") != CHANNEL_ID:
            got = items[0].get("id") if items else "none"
            raise SyncError(f"Authenticated YouTube channel mismatch (received {got})")
        try: return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except KeyError: raise SyncError("Authenticated channel has no uploads playlist") from None
    def upload_ids(self, playlist, full=False):
        max_pages = 100 if full else 1; token = ""; ids = []
        for _ in range(max_pages):
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 50}
            if token: params["pageToken"] = token
            page = self.api("playlistItems", params)
            ids += [x.get("contentDetails", {}).get("videoId", "") for x in page.get("items", [])]
            token = page.get("nextPageToken", "")
            if not token: break
        else:
            # Recent mode intentionally stops after the newest page. Only a full
            # recovery hitting its hard ceiling indicates an incomplete scan.
            if full and token: raise SyncError(f"Uploads exceed safety limit ({max_pages * 50})")
        return dedupe(ids)
    def metadata(self, ids):
        result = {}
        for start in range(0, len(ids), 50):
            page = self.api("videos", {"part": "snippet,status", "id": ",".join(ids[start:start+50]), "maxResults": 50})
            for item in page.get("items", []):
                if item.get("snippet", {}).get("channelId") == CHANNEL_ID: result[item["id"]] = item
        return result

class Airtable:
    def __init__(self):
        base = os.getenv("AIRTABLE_BASE_ID", BASE_ID); table = os.getenv("AIRTABLE_TABLE_ID", TABLE_ID)
        self.url = f"https://api.airtable.com/v0/{base}/{table}"
        self.headers = {"Authorization": "Bearer " + required_env("AIRTABLE_TOKEN")}
    def index(self):
        result = {}; offset = ""
        while True:
            params = {"pageSize": 100}
            if offset: params["offset"] = offset
            page = request_json(self.url + "?" + urllib.parse.urlencode(params), headers=self.headers, service="Airtable API")
            for record in page.get("records", []):
                match = VIDEO_RE.search(str(record.get("fields", {}).get("url", "")))
                if match: result.setdefault(match.group(1), record)
            offset = page.get("offset", "")
            if not offset: return result
    def write_batches(self, creates, updates):
        for method, records in (("POST", [{"fields": x} for x in creates]),
                                ("PATCH", [{"id": rid, "fields": x} for rid, x in updates])):
            for start in range(0, len(records), 10):
                request_json(self.url, method=method, headers=self.headers,
                    payload={"records": records[start:start+10], "typecast": False}, service="Airtable API")

def dedupe(ids):
    return list(dict.fromkeys(x for x in ids if re.fullmatch(r"[A-Za-z0-9_-]{11}", x)))

def fields_for(video_id, item):
    sn, status = item.get("snippet", {}), item.get("status", {})
    description = sn.get("description", ""); thumbs = sn.get("thumbnails", {})
    thumb = next((thumbs[x].get("url", "") for x in ("maxres","standard","high","medium","default") if thumbs.get(x)),
                 f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
    tags = list(dict.fromkeys(HASH_RE.findall(description) + list(sn.get("tags", []))))
    result = {"title": sn.get("title", ""), "url": f"https://www.youtube.com/watch?v={video_id}",
        "썸네일": thumb, "설명": description[:95000], "업로드일": sn.get("publishedAt", ""),
        "privacyStatus": status.get("privacyStatus", ""), "hashtags": ", ".join(tags)[:95000]}
    return {k: v for k, v in result.items() if v not in (None, "", [])}

def sync(full=False, required_ids=()):
    youtube = YouTube(oauth_token())
    playlist = youtube.uploads_playlist()
    airtable = Airtable(); existing = airtable.index()
    ids = dedupe(youtube.upload_ids(playlist, full) + list(required_ids))
    metadata = youtube.metadata(ids)
    missing = [x for x in dedupe(required_ids) if x not in metadata]
    if missing: raise SyncError("Required video not returned for authenticated channel: " + ", ".join(missing))
    creates, updates = [], []
    for video_id in ids:
        if video_id not in metadata:
            print("Skipping unavailable upload:", video_id); continue
        official = fields_for(video_id, metadata[video_id]); old = existing.get(video_id)
        if old:
            old_fields = old.get("fields", {})
            fill = {k: v for k, v in official.items() if not old_fields.get(k)}
            if fill: updates.append((old["id"], fill))
        else:
            creates.append(official); existing[video_id] = {"fields": official}
    airtable.write_batches(creates, updates)
    print(f"Sync complete: mode={'full-backfill' if full else 'recent'}, checked={len(ids)}, created={len(creates)}, filled={len(updates)}, unchanged={len(ids)-len(creates)-len(updates)}")
    return len(creates), len(updates)

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-backfill", action="store_true")
    parser.add_argument("--require-video-id", action="append", default=[])
    args = parser.parse_args(argv)
    required = [x.strip() for x in os.getenv("REQUIRED_VIDEO_IDS", "").split(",") if x.strip()] + args.require_video_id
    try: sync(args.full_backfill, required); return 0
    except SyncError as exc: print("SYNC FAILED:", exc, file=sys.stderr); return 1

if __name__ == "__main__": sys.exit(main())
