# GetYr_music bot

Telegram bot: `/song` gives you a new song (title, Suno style, lyrics) based on what is trending
right now in any language. You make it in Suno, send the MP3 to the bot, then `/video`: the video
(intro + mood background loop + karaoke lyrics) is made on GitHub Actions and scheduled on YouTube.

| Command | What it does |
|---|---|
| `/song` | Next song script (from the top trending song not used yet) |
| send the MP3 | Attaches it to the song (reply to the lyrics message to choose which) |
| `/video` | Makes + uploads the video, sends you the YouTube link |
| `/video <id>` | Retry a song by its id |
| `/trends`, `/status` | Trending list, counts and Suno downloads used this month |

## Files
- `assets/intro.mp4` – plays at the start
- `assets/backgrounds/{sad,energetic,happy}.mp4` – chosen by the song's mood, played forward + reversed as a loop
- `state/songs.json` (bot's memory), `state/uploads.json` (uploaded videos)

## Deploy the bot on Render (one time)
1. render.com → **New → Blueprint** → pick `gptk750m` → it reads `render.yaml`.
2. Fill in the env values (same as the GitHub secrets): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`,
   `GEMINI_API_KEY`, `NVIDIA_API_KEY`, `YT_API_KEY`, `LASTFM_API_KEY`, `GH_DISPATCH_TOKEN` → **Apply**.
3. When it shows **Live**, open your bot in Telegram and send `/song`.
   (Free plan sleeps after 15 min idle; the first message then takes ~1 minute.)

## Settings (GitHub → Settings → Secrets and variables → Actions → Variables)
- `YT_SLOTS` – publish times in IST, default `18:30` (e.g. `12:30,18:30` for two a day)
- `CHANNEL_NAME` – default `GetYr_music`

## Test without uploading
Actions → **Make video** → Run workflow → song id + tick **dry_run** → download the video from the run's artifacts.
