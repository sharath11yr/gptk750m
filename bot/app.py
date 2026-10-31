"""Telegram bot (runs on Render). Commands: /song /video /trends /status /help.

Memory lives in the GitHub repo: state/songs.json (written here), state/uploads.json
(written by the video workflow).
"""

import datetime as dt
import hashlib
import html
import logging
import os
import threading
import time

import requests
from flask import Flask, request

from common import ghstore, trends
from common.writer import write_song

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER = int(os.environ["TELEGRAM_USER_ID"])
TG = f"https://api.telegram.org/bot{TOKEN}"
HOOK_PATH = "/hook/" + hashlib.sha256(TOKEN.encode()).hexdigest()[:24]
QUEUE_SIZE = int(os.getenv("QUEUE_SIZE", "3"))
MONTHLY_DOWNLOADS = int(os.getenv("SUNO_MONTHLY_DOWNLOADS", "20"))
SONGS = "state/songs.json"

app = Flask(__name__)
lock = threading.Lock()  # one writer to songs.json at a time
_trend_cache: dict = {"at": 0.0, "songs": []}


def tg(method: str, **params):
    r = requests.post(f"{TG}/{method}", json=params, timeout=60)
    if not r.ok:
        log.warning("Telegram %s: %s", method, r.text[:300])
    return r.json().get("result")


def say(text: str, **extra):
    return tg("sendMessage", chat_id=OWNER, text=text, parse_mode="HTML",
              disable_web_page_preview=True, **extra)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------- songs ----------

def get_trends() -> list[dict]:
    if time.time() - _trend_cache["at"] > 3 * 3600 or not _trend_cache["songs"]:
        _trend_cache.update(at=time.time(), songs=trends.trending())
    return _trend_cache["songs"]


def make_one() -> dict | None:
    """Write a song from the top trending song not used yet, and save it as 'ready'."""
    songs, _ = ghstore.read(SONGS, [])
    used = {s["ref"]["key"] for s in songs}
    ref = next((t for t in get_trends() if t["key"] not in used), None)
    if not ref:
        return None
    song = write_song(ref)
    song.update(id=dt.datetime.now().strftime("%Y%m%d-%H%M%S"), status="ready", created=now(),
                ref={k: ref[k] for k in ("key", "title", "artist", "sources")})

    def add(data):
        if song["ref"]["key"] in {s["ref"]["key"] for s in data}:
            return None
        data.append(song)
        return song
    with lock:
        return ghstore.update(SONGS, [], add, f"Song ready: {song['title']}")


def refill():
    try:
        songs, _ = ghstore.read(SONGS, [])
        for _ in range(QUEUE_SIZE - sum(s["status"] == "ready" for s in songs)):
            if not make_one():
                break
    except Exception:
        log.exception("Refill failed")


def send_song(song: dict) -> int:
    e = html.escape
    ref = song["ref"]
    say(f"🎵 <b>{e(song['title'])}</b>  ({e(song.get('language_name') or song['language'])}, "
        f"{song['mood']})\n📌 Inspired by: {e(ref['title'])} - {e(ref['artist'])}\n"
        f"✨ {e(song.get('vibe', ''))}\n🆔 <code>{song['id']}</code>")
    say(f"🎨 <b>Style</b> (paste in Suno → Style of Music)\n<pre>{e(song['style'])}</pre>")
    msg = say(f"📝 <b>Lyrics</b> (paste in Suno → Lyrics)\n<pre>{e(song['lyrics'][:3800])}</pre>\n"
              "➡️ Make it in Suno, download the best version, then <b>reply to this message "
              "with the MP3</b> (or just send it).")
    return (msg or {}).get("message_id", 0)


def cmd_song():
    with lock:
        songs, _ = ghstore.read(SONGS, [])
    song = next((s for s in songs if s["status"] == "ready"), None)
    if not song:
        say("✍️ Writing a fresh song from the latest trends… (about 1 minute)")
        song = make_one()
        if not song:
            say("No new trending songs right now - all were used. Try again later.")
            return
    message_id = send_song(song)

    def mark(data):
        for s in data:
            if s["id"] == song["id"]:
                s.update(status="given", given=now(), message_id=message_id)
    with lock:
        ghstore.update(SONGS, [], mark, f"Song given: {song['title']}")
    refill()


def cmd_audio(msg: dict):
    f = msg.get("audio") or msg.get("document") or msg.get("voice")
    if f.get("file_size", 0) > 20 * 1024 * 1024:
        say("⚠️ That file is over 20 MB (Telegram's limit for bots). Send the MP3 version.")
        return
    reply_id = (msg.get("reply_to_message") or {}).get("message_id")
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    result = {}

    def attach(data):
        target = None
        if reply_id:
            target = next((s for s in data if s.get("message_id") == reply_id), None)
        if not target:
            waiting = [s for s in data if s["status"] == "given" and not s.get("audio_file_id")]
            target = waiting[-1] if waiting else None
        if target:
            target.update(audio_file_id=f["file_id"], audio_at=now(), status="has_audio")
            result["song"] = target
        result["used"] = sum(1 for s in data if s.get("audio_at", "").startswith(month))
    with lock:
        ghstore.update(SONGS, [], attach, "Song audio received")
    song = result.get("song")
    if not song:
        say("I don't have a song waiting for audio. Send /song first.")
        return
    left = MONTHLY_DOWNLOADS - result["used"]
    say(f"✅ Song received for <b>{html.escape(song['title'])}</b>.\nSend /video to make and "
        f"upload it.\n📥 Suno downloads this month: {result['used']}/{MONTHLY_DOWNLOADS}"
        + ("\n⚠️ Only {} left this month!".format(left) if left <= 3 else ""))


def cmd_video(arg: str):
    with lock:
        songs, _ = ghstore.read(SONGS, [])
    # With an id, a failed render can be retried.
    ready = [s for s in songs if (s["id"] == arg and s.get("audio_file_id")) if arg] or \
        [s for s in songs if s["status"] == "has_audio" and not arg]
    if not ready:
        say("No song with audio is waiting. Send the MP3 first (or /status).")
        return
    song = ready[-1]
    ghstore.dispatch("video.yml", {"song_id": song["id"]})

    def mark(data):
        for s in data:
            if s["id"] == song["id"]:
                s.update(status="rendering", render_at=now())
    with lock:
        ghstore.update(SONGS, [], mark, f"Rendering: {song['title']}")
    say(f"🎬 Making the video for <b>{html.escape(song['title'])}</b>…\nI'll send the YouTube "
        "link when it's uploaded (about 10-15 minutes).")


def cmd_trends():
    lines = [f"{i + 1}. {html.escape(t['title'])} - {html.escape(t['artist'])}"
             for i, t in enumerate(get_trends()[:15])]
    say("🔥 <b>Trending now</b>\n" + "\n".join(lines))


def cmd_status():
    songs, _ = ghstore.read(SONGS, [])
    uploads, _ = ghstore.read("state/uploads.json", {})
    for s in songs:
        if s["id"] in uploads:
            s["status"] = "uploaded"
    count = lambda st: sum(s["status"] == st for s in songs)
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    used = sum(1 for s in songs if s.get("audio_at", "").startswith(month))
    say(f"📊 Ready scripts: {count('ready')}\nWaiting for MP3: {count('given')}\n"
        f"Waiting for /video: {count('has_audio')}\nRendering: {count('rendering')}\n"
        f"Uploaded: {count('uploaded')}\n📥 Suno downloads this month: {used}/{MONTHLY_DOWNLOADS}")


HELP = ("/song - get the next song (title, style, lyrics)\n"
        "send the MP3 - attach it to the song\n"
        "/video - make the video and upload it to YouTube\n"
        "/trends - see what's trending\n/status - counts")


def handle(update: dict):
    msg = update.get("message") or {}
    if (msg.get("from") or {}).get("id") != OWNER:
        return
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    try:
        if msg.get("audio") or msg.get("document") or msg.get("voice"):
            cmd_audio(msg)
        elif cmd in ("/song", "/songs"):
            cmd_song()
        elif cmd == "/video":
            cmd_video(arg.strip())
        elif cmd == "/trends":
            cmd_trends()
        elif cmd == "/status":
            cmd_status()
        else:
            say(HELP)
    except Exception as exc:
        log.exception("Command failed")
        say(f"❌ Something went wrong: {html.escape(str(exc)[:300])}")


@app.post(HOOK_PATH)
def hook():
    threading.Thread(target=handle, args=(request.get_json(force=True),), daemon=True).start()
    return "ok"


@app.get("/")
def health():
    return "GetYr_music bot is running"


def _set_webhook():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if url:
        tg("setWebhook", url=url + HOOK_PATH, allowed_updates=["message"])
        tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in (
            ("song", "Get the next song script"), ("video", "Make + upload the video"),
            ("trends", "What's trending"), ("status", "Counts"))])


threading.Thread(target=_set_webhook, daemon=True).start()
