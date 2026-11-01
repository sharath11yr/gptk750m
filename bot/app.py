"""Telegram bot (runs on Render). Commands: /song /video /trends /status /help.

Memory lives in the GitHub repo: state/songs.json (written here), state/uploads.json
(written by the video workflow).
"""

import collections
import netrc  # noqa: F401  (load now: a lazy import inside requests deadlocked threads on Python 3.14)
import datetime as dt
import hashlib
import html
import logging
import os
import re
import sys
import threading
import traceback
import time

import requests
import requests.utils  # noqa: F401
import urllib3.util.connection as _conn
from flask import Flask, request

# Render's free servers can hang on IPv6; use IPv4 only.
_conn.HAS_IPV6 = False

from common import ghstore, trends
from common.writer import write_song

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
RECENT: collections.deque = collections.deque(maxlen=60)  # shown at /debug


class _Recent(logging.Handler):
    def emit(self, record):
        text = self.format(record)
        text = re.sub(r"(key=|bot)[A-Za-z0-9_:\-]{20,}", r"\1***", text)
        RECENT.append(text[:1500])


_h = _Recent()
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_h)

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
    try:
        started = time.time()
        r = requests.post(f"{TG}/{method}", json=params, timeout=(10, 60))
        log.info("Telegram %s: HTTP %s in %.1fs", method, r.status_code, time.time() - started)
    except Exception as exc:
        log.warning("Telegram %s failed: %s", method, exc)
        return None
    if not r.ok:
        log.warning("Telegram %s: %s", method, r.text[:300])
        return None
    return r.json().get("result")


def say(text: str, **extra):
    sent = tg("sendMessage", chat_id=OWNER, text=text, parse_mode="HTML",
              disable_web_page_preview=True, **extra)
    if sent is None:  # formatting problem: send it as plain text instead
        sent = tg("sendMessage", chat_id=OWNER, text=re.sub(r"<[^>]+>", "", html.unescape(text))[:4000])
    return sent


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
    say(f"🎨 <b>Style</b> (paste in Suno → Style of Music)\n<pre>{e(song['style'])}</pre>\n"
        f"⚙️ <b>Suno settings</b>\n"
        f"🎤 Vocal Gender: <b>{e(song.get('vocal_gender', '-'))}</b>\n"
        f"🌀 Weirdness: <b>{song.get('weirdness', '-')}%</b>\n"
        f"🎯 Style Influence: <b>{song.get('style_influence', '-')}%</b>\n"
        f"🎲 Variety: <b>{song.get('variety', '-')}%</b>"
        + (f"\n🚫 Exclude styles: <code>{e(song['exclude_styles'])}</code>"
           if song.get("exclude_styles") else ""))
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
    caption = (msg.get("caption") or "").strip()
    own = caption.lower().startswith("own")
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    result = {}

    def attach(data):
        target = None
        if reply_id:
            target = next((s for s in data if s.get("message_id") == reply_id), None)
        if not target and not own:
            waiting = [s for s in data if s["status"] == "given" and not s.get("audio_file_id")]
            target = waiting[-1] if waiting else None
        if not target:
            # Your own song: the video job listens to it to find the lyrics, mood and title.
            target = {"id": dt.datetime.now().strftime("%Y%m%d-%H%M%S"), "own": True,
                      "title_hint": caption[3:].strip(" :-") if own else caption,
                      "title": caption[3:].strip(" :-") if own else (caption or "your song"),
                      "created": now(), "ref": {"key": "own-" + f["file_id"][-12:]}}
            data.append(target)
        target.update(audio_file_id=f["file_id"], audio_at=now(), status="has_audio")
        result["song"] = target
        result["used"] = sum(1 for s in data if s.get("audio_at", "").startswith(month)
                             and not s.get("own"))
    with lock:
        ghstore.update(SONGS, [], attach, "Song audio received")
    song = result["song"]
    left = MONTHLY_DOWNLOADS - result["used"]
    kind = ("🎧 Your own song - I'll work out the lyrics, mood and title myself."
            if song.get("own") else f"✅ Song received for <b>{html.escape(song['title'])}</b>.")
    say(f"{kind}\nSend /video to make and upload it.\n"
        f"📥 Suno downloads this month (bot songs): {result['used']}/{MONTHLY_DOWNLOADS}"
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
        "send the MP3 - attach it to the song (reply to the lyrics message)\n"
        "send an MP3 with caption <code>own</code> (or <code>own: Title</code>) - your own song\n"
        "/video - make the video and upload it to YouTube\n"
        "/trends - see what's trending\n/status - counts")


def handle(update: dict):
    msg = update.get("message") or {}
    sender = (msg.get("from") or {}).get("id")
    log.info("Update from %s: %s", sender, (msg.get("text") or msg.get("caption") or
                                           ("[audio]" if msg.get("audio") or msg.get("document")
                                            else "[other]"))[:60])
    if sender != OWNER:
        log.warning("Ignored: sender %s is not TELEGRAM_USER_ID %s", sender, OWNER)
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
    update = request.get_json(force=True)
    threading.Thread(target=handle, args=(update,), daemon=True, name="handle").start()
    msg = update.get("message") or {}
    if (msg.get("from") or {}).get("id") == OWNER and (msg.get("text") or "").startswith("/"):
        # Reply inside the webhook answer itself: needs no outgoing connection from this server.
        return {"method": "sendMessage", "chat_id": OWNER, "text": "⏳"}
    return "ok"


@app.get("/debug")
def debug():
    stacks = []
    for t in threading.enumerate():
        if t.name.startswith("handle"):
            frame = sys._current_frames().get(t.ident)
            stacks.append("--- stuck task ---\n" + "".join(traceback.format_stack(frame)[-6:]))
    text = "\n".join(RECENT) + "\n\n" + ("\n".join(stacks) or "no tasks running")
    return "<pre>" + html.escape(re.sub(r"(key=|bot)[A-Za-z0-9_:\-]{20,}", r"\1***", text)) + "</pre>"


@app.get("/ping-telegram")
def ping_telegram():
    """Open in a browser: tests this server's own connection to Telegram."""
    started = time.time()
    try:
        r = requests.get(f"{TG}/getMe", timeout=(10, 20))
        return f"Telegram answered HTTP {r.status_code} in {time.time() - started:.1f}s"
    except Exception as exc:
        return f"FAILED after {time.time() - started:.1f}s: {type(exc).__name__}"


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
