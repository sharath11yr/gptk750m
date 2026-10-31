"""Write one original song (title, Suno style, lyrics, YouTube text) inspired by a trending song."""

import json
import re

from .llm import ask_json

MOODS = ["sad", "energetic", "happy"]

FIELDS = """Return ONLY a JSON object with these keys:
"language": ISO 639-1 code of the lyrics language (e.g. "ta", "hi", "en", "ko"),
"language_name": e.g. "Tamil",
"mood": exactly one of "sad", "energetic", "happy" (romantic/calm/devotional count as "happy"; party/hype/race/angry as "energetic"),
"vibe": one line: what the reference song's feeling is and what you kept,
"title": the new song title (different from the reference), written in English letters,
"style": Suno "Style of Music" text, max 180 characters: genre, sub-genre, tempo/BPM, key instruments, vocal type, mood, language. NO artist or song names,
"lyrics": full lyrics written ONLY in English (Latin) letters - for non-English songs use natural romanized spelling as fans type it (e.g. Hindi "Tere bina dil nahi lagta", Tamil "Unnai ninaithu"), never the native script - with Suno tags on their own lines: [Intro] [Verse 1] [Pre-Chorus] [Chorus] [Verse 2] [Bridge] [Outro]. 2-3 minutes of singing, a strong repeated hook in the chorus,
"yt_title": catchy YouTube title, max 90 characters, no artist names, may mix the song title with a short English hint,
"yt_description": 3-5 lines about the song and its mood (no lyrics, no hashtags),
"tags": 12 YouTube search tags (language, genre, mood, "ai music" ...), no artist names"""


def write_song(ref: dict) -> dict:
    ref_text = json.dumps({k: ref.get(k) for k in ("title", "artist", "tags", "lang", "sources")},
                          ensure_ascii=False)
    draft = ask_json(f"""You are a hit songwriter. This song is trending right now:
{ref_text}

Use what you know about it (language, genre, mood, theme, story, hook style, rhyme pattern,
structure, tempo, energy). Write a NEW ORIGINAL song in the SAME language (lyrics written in English letters) with the same feeling,
so it fits on the same playlist - but with completely different words, title and story details.
Never reuse any line, phrase or hook of the reference. Never name any artist.
If you don't know the song, infer its feeling from its title, tags and the charts it is on.

{FIELDS}""")

    final = ask_json(f"""You are a strict music producer. Score this song draft 1-10 for hook strength,
catchiness, emotion, rhyme and flow, singability and how well it matches the trend's feeling.
Then rewrite it to score 9+: stronger chorus hook, tighter rhymes, natural phrasing for native
speakers of its language. Keep it original (nothing copied from the reference "{ref.get('title')}").

Draft:
{json.dumps(draft, ensure_ascii=False)}

{FIELDS}""")
    song = {**draft, **{k: v for k, v in final.items() if v}}
    return clean(song, ref)


def clean(song: dict, ref: dict) -> dict:
    strip = lambda s: re.sub(r"[<>]", "", str(s or "")).strip()
    artist_words = {w.lower() for w in re.findall(r"\w{4,}", ref.get("artist", ""))}
    style = strip(song.get("style"))
    style = " ".join(w for w in style.split() if w.lower().strip(",.") not in artist_words)
    mood = str(song.get("mood", "")).lower()
    tags, total = [], 0
    for t in song.get("tags", []):
        t = strip(t)[:30]
        if t and total + len(t) < 450 and not any(a in t.lower() for a in artist_words):
            tags.append(t)
            total += len(t) + 1
    return {
        "language": strip(song.get("language"))[:5].lower() or "en",
        "language_name": strip(song.get("language_name")),
        "mood": mood if mood in MOODS else "happy",
        "vibe": strip(song.get("vibe")),
        "title": strip(song.get("title"))[:80],
        "style": style[:200],
        "lyrics": strip(song.get("lyrics")),
        "yt_title": strip(song.get("yt_title") or song.get("title"))[:95],
        "yt_description": strip(song.get("yt_description")),
        "tags": tags,
    }
