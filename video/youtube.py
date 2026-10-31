"""Upload the video + thumbnail to YouTube (official YouTube Data API v3).

Needs YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN. The video is uploaded as private and
YouTube makes it public at publish_at.
"""

import datetime as dt
import logging
import os
import re
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]


def _client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(video: Path, thumbnail: Path | None, meta: dict, publish_at: dt.datetime) -> str:
    youtube = _client()
    clean = lambda s: re.sub(r"[<>]", "", s)
    body = {
        "snippet": {
            "title": clean(meta["title"])[:100],
            "description": clean(meta["description"]),
            "tags": meta.get("tags", []),
            "categoryId": os.getenv("YT_CATEGORY") or "10",  # Music
            "defaultLanguage": meta.get("language", "en"),
            "defaultAudioLanguage": meta.get("language", "en"),
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,  # YouTube's AI-content disclosure
        },
    }
    request = youtube.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024,
                                   resumable=True),
    )
    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("Upload %d%%", int(status.progress() * 100))
        except HttpError as exc:
            if exc.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                time.sleep(2 ** retries)
                continue
            raise
    video_id = response["id"]
    log.info("Uploaded: https://youtu.be/%s", video_id)

    if thumbnail and thumbnail.exists():
        try:
            youtube.thumbnails().set(videoId=video_id,
                                     media_body=MediaFileUpload(str(thumbnail),
                                                                mimetype="image/jpeg")).execute()
        except HttpError as exc:
            log.warning("Could not set thumbnail (is the channel phone-verified?): %s", exc)
    return video_id
