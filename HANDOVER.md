# GetYr_music – project handover

Everything another assistant (or person) needs to continue this project. Written from the
original build conversation. **No keys or tokens are in this file – they live only in GitHub
Secrets and Render environment variables.**

---

## 1. What the owner wants

A semi-automatic AI music YouTube channel, **GetYr_music**.

1. `/song` in Telegram → the bot looks at what is trending right now (any language), takes the
   top song not used before as a *reference*, and writes a **new original** song with the same
   feeling: title, Suno style, Suno settings, lyrics. Different words and title – never copied.
2. The owner makes it in **Suno** by hand (Suno Pro; no official API – automating the Suno
   website was rejected because it breaks Suno's terms and risks the account), downloads the MP3
   and **replies to the lyrics message** with it in Telegram.
3. `/video` → a lyric video is made and **uploaded to YouTube, scheduled** for the next free slot.
4. The owner can also send their **own song**: MP3 with caption `own` (or `own: Title`) → the job
   transcribes it, the AI picks the mood/background, title and YouTube text.

Owner's preferences (all decided in the conversation):
- **Any language**, follows the trends. Lyrics and titles always written in **English letters**
  (romanized, e.g. Hindi "Tere bina dil nahi lagta"), on screen and for pasting into Suno.
- Lyrics must be **meaningful**: one story/emotion, specific images, no filler.
- Each script shows Suno settings: **vocal gender, weirdness, style influence, variety**
  (+ exclude styles).
- **Video style** (from the owner's own edits): one uppercase line in the centre, white only,
  **Bebas Neue**-like condensed font, each word appears as it is sung with a soft **white glow**.
  **No colours, no watermark added** (the backgrounds already show "GetYr_Music" top-left –
  that is baked into the owner's video files).
- **Intro** video at the start, **no outro**.
- Background chosen by mood: `sad` / `energetic` (hot/race) / `happy` (joyful), looped
  forward + reversed so the loop never jumps.
- Upload **scheduled**, 1 per day at **18:30 IST** (variable `YT_SLOTS`).
- AI: **NVIDIA first** (owner's explicit choice), Gemini as backup.
- Owner has **low Claude credits** – keep replies short, avoid needless pushes/redeploys.

Still open (asked, not answered yet):
1. Serif font for energetic songs (one reference frame "WAKE WAKE WAKE" was serif) or Bebas Neue
   for everything?
2. Should earlier words of a line fade to grey as new words appear (one reference showed that),
   or stay white?

---

## 2. Status (as of 2026-11-09)

**Working end to end.** Several songs have gone through `/song` → Suno → MP3 → `/video` →
scheduled YouTube upload (see `state/uploads.json` and the "Uploaded …" commits).

Known limits / watch items:
- **Suno Pro = 20 downloads a month** (cap since Sept 2026). The bot counts bot-song downloads per
  month in `/status` and warns at 3 left. Own songs don't count.
- **YouTube API audit**: the Google project is new. If uploads ever show as "locked private",
  the audit isn't done – publish from YouTube Studio, or apply at
  https://support.google.com/youtube/contact/yt_api_form.
- **Gemini free quota** ran out once (HTTP 429); it's only the backup now.
- **NVIDIA models retire often** – the code asks NVIDIA which models the key can use (`/models`)
  and prefers a short list in `common/llm.py` (`NVIDIA_PREFERRED`). Update that list if needed.
- First `/song` after a long pause is slow (charts + 2 AI calls). Background refill keeps 3
  scripts ready so later `/song` is instant.
- Render free plan sleeps after 15 min idle; first message wakes it (~1 min).
- Every push to `main` makes Render redeploy the bot (it can't answer for ~3–5 min).

---

## 3. Architecture

```
Telegram  ──webhook──▶  Render (free web service, bot/app.py, Python 3.12)
                          │  reads trends, asks AI, keeps memory in the repo via GitHub API
                          │  /video → workflow_dispatch
                          ▼
                     GitHub Actions (.github/workflows/video.yml)
                          │  download MP3 from Telegram → Whisper timing → ffmpeg render
                          │  → YouTube upload (scheduled) → message to Telegram
                          ▼
                     state/uploads.json committed back to the repo
```

Memory (in the repo, no database):
- `state/songs.json` – every song: script, ref song, status
  (`ready` → `given` → `has_audio` → `rendering`), Telegram file id. Written by the bot through
  the GitHub Contents API.
- `state/uploads.json` – song id → YouTube video id, publish time. Written only by the workflow
  (separate file so the two writers never conflict).

---

## 4. Files

| File | What it does |
|---|---|
| `bot/app.py` | Flask webhook bot. Commands `/song /video [id] /trends /status`, MP3 handling (reply → that song; caption `own` → own song), background refill (`QUEUE_SIZE`, default 3), `/debug` page (last log lines + where tasks are stuck, keys redacted), `/ping-telegram`. Replies "⏳" inside the webhook response. |
| `common/trends.py` | Free trend sources merged and ranked: YouTube mostPopular music (many regions, `YT_API_KEY`), Last.fm global + India (`LASTFM_API_KEY`), Apple Music RSS, Deezer chart, Spotify via kworb.net. |
| `common/writer.py` | Prompt for writing the song (draft → strict producer review/rewrite), JSON fields, cleaning (no artist names, tag length, romanized lyrics, Suno settings). |
| `common/llm.py` | `ask_json()`: NVIDIA first (`LLM_ORDER`), Gemini backup; dynamic model lists; thinking turned off where supported; timeouts (15 s connect / 150 s read) and an 8-minute total limit (`LLM_TOTAL_SECONDS`); retries when busy; tolerant JSON parsing. |
| `common/ghstore.py` | Read/write JSON in the repo + start the workflow (`GH_DISPATCH_TOKEN`). |
| `video/make.py` | The video job: Telegram download → `stable-ts` alignment of known lyrics (own songs: transcribe + AI) → ASS subtitles (word-by-word `\ko` reveal, glow = blurred copy on a lower layer) → background ping-pong loop → loudnorm −14 LUFS → intro concat → thumbnail → schedule slot → upload → Telegram message. |
| `video/youtube.py` | YouTube Data API upload (category Music 10, `containsSyntheticMedia: true`, private + `publishAt`), thumbnail. Adapted from the owner's story-channel repo `sharath-IXVII/aptk751zx`. |
| `.github/workflows/video.yml` | Runs `video.make` for a song id (input `dry_run` = make only, video as artifact). Installs ffmpeg, Noto fonts, downloads Bebas Neue, CPU torch, openai-whisper (built with old setuptools), commits `state/uploads.json`, tells Telegram on failure. |
| `render.yaml` | Render blueprint for the bot (free plan, gunicorn, env var names). |
| `.python-version` | `3.12` – Render's default Python 3.14 deadlocked the bot (see lessons). |
| `assets/intro.mp4`, `assets/backgrounds/{sad,energetic,happy}.mp4` | Owner's videos, 1280×720. Output videos are 1280×720 to match. |

---

## 5. Secrets and settings (names only)

GitHub → Settings → Secrets and variables → Actions:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, `NVIDIA_API_KEY`, `GEMINI_API_KEY`, `YT_API_KEY`,
`LASTFM_API_KEY`, `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`, `GH_DISPATCH_TOKEN`.

Render (service `getyr-music-bot`) env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`,
`GEMINI_API_KEY`, `NVIDIA_API_KEY`, `YT_API_KEY`, `LASTFM_API_KEY`, `GH_DISPATCH_TOKEN`,
`GH_REPO`, `PYTHON_VERSION`.

Notes:
- `GH_DISPATCH_TOKEN` = fine-grained token from **sharath11yr**, repo `gptk750m` only,
  **Contents: Read and write** + **Actions: Read and write**.
- YouTube OAuth is a separate Google Cloud project on the music channel's Gmail (Desktop-app
  client, app published "In production" so the refresh token doesn't expire after 7 days).
- Optional GitHub variables: `YT_SLOTS` (IST times, default `18:30`), `CHANNEL_NAME`.
- Optional bot env: `QUEUE_SIZE`, `SUNO_MONTHLY_DOWNLOADS` (20), `LLM_ORDER`, `NVIDIA_MODEL`,
  `GEMINI_MODEL`, `LLM_TOTAL_SECONDS`, `TREND_REGIONS`, `APPLE_REGIONS`.

Bot URLs: `https://getyr-music-bot.onrender.com/` (health), `/debug`, `/ping-telegram`.

---

## 6. Lessons learned (don't repeat these)

- **Python 3.14 on Render deadlocked** background threads (lazy `netrc` import inside `requests`
  while another thread held the import lock). Fixed by pinning 3.12 and importing `netrc` early.
- Render: force **IPv4** for outgoing requests (`urllib3 HAS_IPV6 = False`).
- `openai-whisper==20240930` needs `setuptools<70` and `--no-build-isolation`; install
  `torch torchaudio` from the CPU index together (otherwise a huge CUDA torch is pulled).
- libass `\k`/`\ko` shows a syllable when its time *ends*, so the wait goes before each word and
  the word itself gets 1 cs.
- Telegram bots can only download files ≤ 20 MB (MP3 fine, WAV not).
- NVIDIA reasoning models may return empty `content` (text in `reasoning_content`); handled.
- Never put keys in chat; to recover a lost key, get it again from its source
  (BotFather `/mybots` → API Token, AI Studio, Cloud Console "Show key", last.fm/api/accounts);
  NVIDIA and GitHub tokens must be regenerated.
- Claude's GitHub App must be installed on `sharath11yr/gptk750m` for pushes from Claude.

---

## 7. How to test

- Bot: send `/status` → "⏳" then counts. If silent: open `/debug`.
- Video without the bot: GitHub → Actions → **Make video** → Run workflow → song id
  (optionally tick `dry_run` and download the artifact).
- Retry a failed song: `/video <song id>` (ids are in the bot's first message and `state/songs.json`).

## 8. Rules the bot must keep

No copied lyrics/melodies, no artist names in style/title/tags, YouTube AI disclosure always on,
AI-music line in every description, spread uploads (1/day), backgrounds/intro are the owner's own.
