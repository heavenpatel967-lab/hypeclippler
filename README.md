# HypeClipper

Free, unlimited, local auto-clipper for **Twitch, Kick and YouTube** live streams.

It reads each streamer's live chat. When chat suddenly gets much faster than normal (spam, `KEKW`,
`POG`, `W`, `CLIP IT`, subs, bits, super chats), it saves a clip from **20 s before to 20 s after
the chat peak** and turns it into a ready-to-post **9:16 Short/Reel** with:

- word-by-word captions (yellow highlight, bold outline) from local Whisper speech-to-text
- a hook line burned at the top ("Nobody expected this")
- title, description and hashtags (local AI via Ollama, or a template if Ollama isn't installed)
- a 16:9 copy of the same clip

Everything runs on your PC. No API keys, no subscriptions, no usage limits.

## Setup (Windows)

1. Install **Python 3.12** from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"**.
2. Double-click **`start.bat`**.
   - The first run installs ffmpeg (through winget). Close the window and run `start.bat` again.
   - The second run installs the Python packages, which takes a few minutes. Then the dashboard
     opens at http://localhost:8000.
3. *(Optional, recommended)* Better titles with free local AI:
   - Install Ollama from https://ollama.com/download
   - Open a terminal and run `ollama pull llama3.2:3b`
   - The dashboard header changes to "Ollama ✓".

The first clip downloads the Whisper `large-v3-turbo` model (~1.6 GB), one time only.
On an RTX 3070 it captions a 40 s clip in a few seconds.

Linux/macOS: install `ffmpeg`, then run `./start.sh`.

## Using it

- Type a channel and pick the platform, or paste a full URL: `xqc`, `https://kick.com/adinross`,
  `@handle` or a YouTube live URL. Add as many streamers as you want.
- Each card shows the live chat speed, the streamer's normal speed and a hype meter. The red
  dashed line on the graph is the trigger level.
- For the first 60 s the app learns each chat's normal speed. After that, clips are made
  automatically.
- **✂ Clip now** saves the last 30 s plus the next 10 s, for when you see something yourself.
- Finished clips appear under **Clips**: preview, copy the title/description/hashtags,
  download 9:16 or 16:9.
- Files are saved in `data/clips/<id>/` (`clip_9x16.mp4`, `clip_16x9.mp4`, `meta.json`).

## Tuning

| Setting | What it does |
|---|---|
| Sensitivity (2.5) | Chat must be this many times faster than normal. Lower = more clips. |
| Min chat speed (1.0/s) | Ignores "spikes" in tiny chats. |
| Cooldown (90 s) | Minimum gap between auto clips of one streamer. |
| Seconds before/after (20/20) | Clip window around the peak. |
| Layout | `blur` = full 16:9 video on a blurred background, `crop` = fill the screen (cuts the sides). |
| Recording quality | `720p60,720p,720p30,best` keeps bandwidth low. Use `1080p60,best` for sharper crop layouts. |
| Whisper model | `large-v3-turbo` (best), `medium`, `small` (fastest). |

## How many streamers can I run?

Recording only copies the stream (no re-encoding), so CPU use per streamer is tiny. The limit is
your **internet download speed**: about **3–4 Mbps per streamer at 720p60** (6–8 Mbps at 1080p).
For example, 15 streamers at 720p need about 50–60 Mbps. Each streamer keeps about 3 minutes of
video on disk (~100 MB).

Clips are processed one at a time on the GPU (about 10–20 s each on a 3070), so with many
streamers they wait in the **Processing** list. Video is cut out of the buffer immediately, so
nothing gets lost while clips wait.

## How hype detection works

Every chat message gets a score: 1 for normal chat, extra for hype words and emotes, CAPS,
"LMAOOOO"-style spam and emojis, and 3 for subs, bits and super chats. Each second the app adds
up the last 5 seconds and compares that to a slow moving average of the streamer's normal chat.
When chat runs `sensitivity` times faster than normal, a hype moment starts. When chat calms down,
the loudest second is the peak, and the clip is cut around it. Because the threshold is relative,
the same settings work for a 50-viewer stream and a 100k-viewer GTA 6 launch.

## Troubleshooting

- **"offline, checking every 30s"**: the streamer isn't live. The app keeps checking and starts
  recording when they go live.
- **Kick chat keeps reconnecting**: Kick sometimes changes its chat server. Open kick.com in
  Chrome, press F12, go to Network > WS, find `ws-<cluster>.pusher.com/app/<key>`, and paste the
  key and cluster into the settings. If there's no pusher.com connection at all, Kick has moved
  to a new chat system and `app/chat.py` needs an update.
- **Captions say "CPU"/are slow**: the GPU libraries didn't load. Make sure your NVIDIA driver is
  up to date. The app falls back to CPU automatically, which is slower but still works.
- **encoder: libx264** in the header: NVENC isn't available. Update the NVIDIA driver.

## Copyright note

Clips belong to the streamers. Many streamers allow or pay for clips (clipping programs), but get
permission before posting someone else's content, especially if you monetize it.
