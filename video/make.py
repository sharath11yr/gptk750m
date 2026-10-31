"""Make the lyric video for one song and upload it (runs on GitHub Actions).

    python -m video.make SONG_ID [--dry-run]

Steps: download the MP3 from Telegram -> time the lyrics to the singing (stable-ts) ->
background loop (forward + reversed, so it never jumps) with karaoke lyrics in the middle ->
intro at the start -> thumbnail -> schedule on YouTube -> tell Telegram.
"""

import datetime as dt
import json
import logging
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("video")

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
WORK = ROOT / "work"
W, H, FPS = 1280, 720, 30
CHANNEL = os.getenv("CHANNEL_NAME") or "GetYr_music"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
FONT = os.getenv("LYRICS_FONT") or "Bebas Neue"
FONTS_DIR = ASSETS / "fonts"


def run(*args):
    log.info("$ %s", " ".join(map(str, args))[:300])
    subprocess.run(list(map(str, args)), check=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def telegram(method: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params,
                         timeout=60).json()


def download_audio(file_id: str, dest: Path) -> Path:
    info = telegram("getFile", file_id=file_id)["result"]
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.get(f"https://api.telegram.org/file/bot{token}/{info['file_path']}", timeout=300)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


# ---------- lyrics timing ----------

def sung_lines(lyrics: str) -> list[str]:
    """Lyric lines without Suno tags like [Chorus]."""
    lines = []
    for line in lyrics.splitlines():
        line = re.sub(r"\[.*?\]", "", line).strip()
        if line:
            lines.append(line)
    return lines


def align(audio: Path, lines: list[str], language: str, length: float) -> list[dict]:
    """Returns [{text, start, end, words: [(text, start, end)]}]. Falls back to even spacing."""
    try:
        import stable_whisper
        model = stable_whisper.load_model(os.getenv("WHISPER_MODEL", "small"))
        text = "\n".join(lines)
        result = model.align(str(audio), text, language=language or None)
        words = [(w.word, float(w.start), float(w.end)) for w in result.all_words()]
        # Map each aligned word back to its line by character position in the text.
        bounds, pos = [], 0
        for line in lines:
            pos = text.find(line, pos)
            bounds.append((pos, pos + len(line)))
            pos += len(line)
        out = [{"text": l, "words": []} for l in lines]
        cursor = 0
        for word, start, end in words:
            w = word.strip()
            if not w:
                continue
            found = text.find(w, cursor)
            if found < 0:
                found = cursor
            cursor = found + len(w)
            idx = next((i for i, (a, b) in enumerate(bounds) if found < b), len(lines) - 1)
            out[idx]["words"].append((word, start, end))
        timed = [l for l in out if l["words"]]
        if len(timed) < max(2, len(lines) * 0.5):
            raise RuntimeError(f"only {len(timed)}/{len(lines)} lines aligned")
        for l in timed:
            l["start"], l["end"] = l["words"][0][1], l["words"][-1][2]
        return timed
    except Exception as exc:
        log.warning("Alignment failed (%s); spacing lyrics evenly", exc)
        start, end = length * 0.06, length * 0.95
        step = (end - start) / max(1, len(lines))
        return [{"text": l, "start": start + i * step, "end": start + (i + 1) * step - 0.2,
                 "words": []} for i, l in enumerate(lines)]


def analyze_own(audio: Path, song: dict) -> list[dict]:
    """Own song (no script from the bot): transcribe it, then let the AI choose the mood
    (background), title and YouTube text and write the lyrics in English letters."""
    import stable_whisper
    from common.llm import ask_json
    from common.writer import clean
    model = stable_whisper.load_model(os.getenv("WHISPER_MODEL", "small"))
    result = model.transcribe(str(audio))
    segs = [(s.text.strip(), float(s.start), float(s.end)) for s in result.segments
            if s.text.strip()]
    numbered = "\n".join(f"{i + 1}. {t}" for i, (t, _, _) in enumerate(segs)) or "(instrumental)"
    info = ask_json(f"""This is the transcript of a song (language detected: {result.language}),
one numbered line per sung phrase. Title hint from the owner: "{song.get('title_hint', '')}".

{numbered}

Return ONLY JSON with keys:
"mood": exactly one of "sad", "energetic", "happy" (romantic/calm/devotional = happy; party/hype/race/angry = energetic),
"language": ISO 639-1 code, "language_name": e.g. "Tamil",
"title": song title in English letters (use the hint if given),
"yt_title": catchy YouTube title, max 90 characters,
"yt_description": 3-5 lines about the song and its mood,
"tags": 12 YouTube search tags,
"lines": the SAME number of lines as above, in the same order, each corrected for obvious
transcription mistakes and written in English letters (romanized as fans type it).""")
    lines = [str(l).strip() for l in info.get("lines", [])]
    if len(lines) != len(segs):
        lines = [t for t, _, _ in segs]
    song.update(clean({**info, "lyrics": "\n".join(lines)}, {"artist": ""}))
    if song.get("title_hint"):
        song["title"] = song["title_hint"]
    timed = []
    for line, (_, start, end) in zip(lines, segs):
        words = line.split()
        if not words:
            continue
        step = (end - start) / len(words)
        timed.append({"text": line, "start": start, "end": end,
                      "words": [(" " + w, start + i * step, start + (i + 1) * step)
                                for i, w in enumerate(words)]})
    return timed


def ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


ASS_HEAD = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Glow,{FONT},96,&H40FFFFFF,&HFFFFFFFF,&H60FFFFFF,&HFF000000,0,0,0,0,100,100,2,0,1,6,0,5,90,90,40,1
Style: Lyric,{FONT},96,&H00FFFFFF,&HFFFFFFFF,&H00FFFFFF,&HFF000000,0,0,0,0,100,100,2,0,1,0,0,5,90,90,40,1
Style: ThumbGlow,{FONT},150,&H40FFFFFF,&H40FFFFFF,&H50FFFFFF,&HFF000000,0,0,0,0,100,100,3,0,1,8,0,5,60,60,40,1
Style: Thumb,{FONT},150,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&HFF000000,0,0,0,0,100,100,3,0,1,0,0,5,60,60,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(timed: list[dict], path: Path, length: float) -> None:
    """One line at a time in the centre, white and uppercase. Each word appears when it is sung,
    with a soft white glow behind it (a blurred copy on a lower layer)."""
    events = []
    for i, line in enumerate(timed):
        start = line["start"] - 0.2
        nxt = timed[i + 1]["start"] - 0.2 if i + 1 < len(timed) else length
        end = min(nxt, line["end"] + 2.5)
        if line["words"]:
            parts, t = [], start
            for word, ws, we in line["words"]:
                # \ko: the word (fill and glow) stays hidden until it is sung, then appears.
                # (libass shows a syllable when its time is over, so the wait sits before it)
                parts.append(f"{{\\ko{max(1, round((ws - t) * 100))}}}")
                parts.append(f"{{\\ko1}}{ass_escape(word.upper())}")
                t = ws + 0.01
            text = "".join(parts)
        else:
            text = ass_escape(line["text"].upper())
        for layer, style, tags in ((0, "Glow", "\\blur14"), (1, "Lyric", "\\blur0.6")):
            events.append(f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,"
                          f"{{\\fad(200,250){tags}}}{text}")
    path.write_text(ASS_HEAD + "\n".join(events) + "\n", encoding="utf-8")


# ---------- video ----------

def pick_background(mood: str) -> Path:
    folder = ASSETS / "backgrounds"
    match = folder / f"{mood}.mp4"
    if match.exists():
        return match
    return random.choice(sorted(folder.glob("*.mp4")))


def render(audio: Path, ass: Path, background: Path, out: Path, length: float) -> None:
    loop = WORK / "loop.mp4"
    # Forward + reversed = a seamless loop.
    run("ffmpeg", "-y", "-i", background, "-filter_complex",
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
        f"fps={FPS},format=yuv420p,split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1[v]",
        "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", loop)
    main = WORK / "main.mp4"
    ass_path = str(ass).replace(":", r"\:")
    run("ffmpeg", "-y", "-stream_loop", "-1", "-i", loop, "-i", audio, "-filter_complex",
        f"[0:v]ass='{ass_path}':fontsdir='{FONTS_DIR}'[v];"
        "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{length:.2f}", "-c:v", "libx264", "-preset",
        "veryfast", "-crf", "21", "-r", str(FPS), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a",
        "192k", main)
    intro = ASSETS / "intro.mp4"
    if not intro.exists():
        main.replace(out)
        return
    has_audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                "stream=index", "-of", "csv=p=0", str(intro)],
                               capture_output=True, text=True).stdout.strip()
    intro_audio = ("[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];" if has_audio else
                   f"anullsrc=r=48000:cl=stereo,atrim=0:{duration(intro):.2f}[a0];")
    run("ffmpeg", "-y", "-i", intro, "-i", main, "-filter_complex",
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={FPS},format=yuv420p[v0];{intro_audio}"
        "[1:v]setsar=1[v1];[1:a]anull[a1];[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out)


def thumbnail(background: Path, title: str, out: Path) -> None:
    ass = WORK / "thumb.ass"
    ass.write_text(ASS_HEAD +
                   f"Dialogue: 0,0:00:00.00,0:00:10.00,ThumbGlow,,0,0,0,,{{\\blur16}}"
                   f"{ass_escape(title.upper())}\n"
                   f"Dialogue: 1,0:00:00.00,0:00:10.00,Thumb,,0,0,0,,{ass_escape(title.upper())}\n",
                   encoding="utf-8")
    ass_path = str(ass).replace(":", r"\:")
    run("ffmpeg", "-y", "-ss", "1", "-i", background, "-vf",
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"ass='{ass_path}':fontsdir='{FONTS_DIR}'", "-frames:v", "1", "-q:v", "3", out)


# ---------- schedule + upload ----------

def next_slot(taken: list[str]) -> dt.datetime:
    """Next free publish time from YT_SLOTS (IST, e.g. "18:30" or "12:30,18:30")."""
    slots = [s.strip() for s in (os.getenv("YT_SLOTS") or "18:30").split(",") if s.strip()]
    taken_set = set(taken)
    earliest = dt.datetime.now(IST) + dt.timedelta(hours=1)
    day = earliest.date()
    while True:
        for s in sorted(slots):
            h, m = map(int, s.split(":"))
            t = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
            if t >= earliest and t.astimezone(dt.timezone.utc).isoformat() not in taken_set:
                return t
        day += dt.timedelta(days=1)


def description(song: dict) -> str:
    lyrics = "\n".join(sung_lines(song.get("lyrics", "")))
    tags = " ".join("#" + re.sub(r"\W", "", t) for t in song["tags"][:3] if re.sub(r"\W", "", t))
    return (f"{song['yt_description']}\n\n🎵 {song['title']} | {CHANNEL}\n\n"
            f"Lyrics:\n{lyrics}\n\n"
            "This song was created with AI (music made with Suno).\n"
            f"Subscribe to {CHANNEL} for new songs every day!\n\n{tags} #aimusic")[:4900]


def main() -> None:
    song_id = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    songs = json.loads((ROOT / "state/songs.json").read_text())
    uploads_path = ROOT / "state/uploads.json"
    uploads = json.loads(uploads_path.read_text())
    song = next(s for s in songs if s["id"] == song_id)
    if song_id in uploads and not dry_run:
        log.info("Already uploaded: %s", uploads[song_id])
        return
    WORK.mkdir(exist_ok=True)

    audio = download_audio(song["audio_file_id"], WORK / "song.mp3")
    length = duration(audio)
    if song.get("own"):
        timed = analyze_own(audio, song)
    else:
        timed = align(audio, sung_lines(song["lyrics"]), song["language"], length)
    ass = WORK / "lyrics.ass"
    write_ass(timed, ass, length)
    background = pick_background(song["mood"])
    video = WORK / "video.mp4"
    render(audio, ass, background, video, length)
    thumb = WORK / "thumb.jpg"
    thumbnail(background, song["title"], thumb)
    if dry_run:
        log.info("Dry run: video saved, not uploaded")
        return

    from video.youtube import upload
    publish_at = next_slot([u.get("publish_at", "") for u in uploads.values()])
    meta = {"title": song["yt_title"], "description": description(song), "tags": song["tags"],
            "language": song["language"]}
    video_id = upload(video, thumb, meta, publish_at)
    uploads[song_id] = {"video_id": video_id, "title": song["yt_title"],
                        "publish_at": publish_at.astimezone(dt.timezone.utc).isoformat(),
                        "uploaded": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    # Save at once so a later failure never causes a second upload.
    uploads_path.write_text(json.dumps(uploads, ensure_ascii=False, indent=1))
    telegram("sendMessage", chat_id=int(os.environ["TELEGRAM_USER_ID"]),
             text=f"✅ Uploaded: {song['yt_title']}\nhttps://youtu.be/{video_id}\n"
                  f"🕒 Scheduled for {publish_at:%a %d %b, %I:%M %p} IST\n"
                  "(If YouTube shows it as locked private, the Google API audit isn't done yet - "
                  "publish it from YouTube Studio.)")


if __name__ == "__main__":
    main()
