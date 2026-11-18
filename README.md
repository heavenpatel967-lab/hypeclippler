# HypeClipper

Free, unlimited, local auto-clipper for **Twitch, Kick and YouTube** live streams.

It reads each streamer's live chat. When chat suddenly gets much faster than normal (spam, `KEKW`,
`POG`, `W`, `CLIP IT`, subs, bits, super chats), it saves a clip from **20 s before to 20 s after
the chat peak** and turns it into a ready-to-post **9:16 Short/Reel** with:

- word-by-word captions (yellow highlight, bold outline) from local Whisper speech-to-text
- **timed story captions at the top** that change with the action ("He had no idea..." →
  "NO WAY" → "Chat lost it"), written by local AI from the clip's timeline
- **sound effects placed on the key moments** (boom, airhorn, laugh, fail, bruh, wow, suspense riser)
- **optional live chat box under the video**, for streams that don't show chat on screen
- title, description and hashtags (local AI via Ollama, or a template if Ollama isn't installed)
- a 16:9 copy of the same clip (no effects)

Everything runs on your PC. No API keys, no subscriptions, no usage limits.

## Setup (Windows)

1. Install **Python 3.12** from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"**.
2. Double-click **`start.bat`**.
   - The first run installs ffmpeg (through winget). Close the window and run `start.bat` again.
   - The second run installs the Python packages, which takes a few minutes. Then the dashboard
     opens at http://localhost:8000.
3. *(Recommended)* Free local AI that **watches the clip** and writes specific captions, titles
   and sound choices:
   - Install Ollama from https://ollama.com/download
   - Open a terminal and run `ollama pull qwen3-vl:4b` (3.3 GB, a vision model that fits an
     8 GB card next to Whisper). `gemma4:e4b` is a good alternative.
   - The dashboard header shows **AI: qwen3-vl:4b 👁 sees video**.
   - The AI model setting is `auto`: the app uses the best model you have installed (vision
     models first), so a wrong model name can't switch the AI off.

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

## Top captions, sound effects and chat box

For every clip the app builds a timeline of what happens when:
- **chat reactions**: bursts of laughing (`KEKW`, `LMAO`), hype (`POG`, `W`), shock (`NO WAY`, `WTF`),
  fails (`L`, `RIP`), scared (`monkaS`) and "bruh" messages
- **what the streamer says**: "no way", "let's go", "oh no", "bruh", laughing
- **loud moments in the audio**: screams, crashes, explosions

Chat reacts about 4 seconds after the action, so chat bursts are moved back and snapped to the
nearest loud moment in the audio.

- **Top captions:** Ollama gets the timeline and writes 4–7 short captions with times. If Ollama is
  off or returns something unusable, a built-in set of captions is placed on the same moments.
- **Sound effects:** up to 3 per clip (setting), at least 5 s apart, on the strongest moments, plus a
  whoosh at the start. Laughing gets a laugh sound, hype gets an airhorn, a fail gets a sad
  trombone, a shock gets a boom, and so on.
- **Layout:** the video stays in the center. Story captions are at the top, word captions sit
  just above the video (setting: above / on_video / below), and the chat box goes under the video.
- **Chat box:** tick **show chat in clips** when adding a streamer, or the **chat** box on their card.
  The real chat from that moment scrolls in a box under the video with users' name colors. Huge chats are thinned to about 3–4 lines per second so it
  stays readable.

### Sound effects library

The **Sound effects** section of the dashboard has one folder per sound type:

| Folder | Used for |
|---|---|
| `fahh` (FAHHH), `emotional_damage`, `sad_violin`, `oh_no`, `error`, `fail` | fails, things going wrong |
| `boom` (vine boom), `taco_bell`, `metal_pipe`, `huh`, `wow` | shocks, "NO WAY" moments |
| `laugh`, `goofy`, `oh_no` | chat laughing |
| `airhorn`, `rizz`, `wow` | hype, W moments |
| `bruh`, `huh`, `sus`, `goofy` | bruh / confused moments |
| `suspense`, `oh_no`, `sus` | scary, tense moments |
| `metal_pipe`, `boom`, `taco_bell` | very loud moments (screams, crashes) |
| `whoosh` | start of every clip |

For each moment the app picks a random folder that fits, never the same kind twice in one clip,
and prefers folders that contain your own sounds. Sounds longer than 4 s are cut and faded.

**Adding sounds:** on a folder's card, paste a link and click **Add**, or click **Upload**.
A link can be a direct `.mp3` link or a sound page such as a myinstants.com page (search
"fahhh", "vine boom", "emotional damage", "metal pipe"...); the app finds the sound on the page.
▶ previews a sound, ✕ deletes it.

Some folders have a simple built-in sound so the app works offline (boom, airhorn, wow, fail,
suspense, whoosh, goofy, metal pipe, error). Meme voice sounds like FAHHH, bruh, emotional
damage or rizz can't be generated, so those folders stay empty until you add a file. Meme sounds
on sites like myinstants are uploaded by users and may be copyrighted; free licensed sounds are
on https://pixabay.com/sound-effects/.

### 🎬 Reels style (one click)

The look of popular GTA clip pages: the streamer's webcam fills the top, gameplay fills the
bottom, and one headline sits on the line between them ("TypicalGamer sneaks into GTA 6's new
military base"). No chat box, no word captions, no sound effects.
- The webcam is **found automatically** (a face that stays in the same place). Draw it yourself
  with **🎥 webcam** on the card if auto picks the wrong spot. With no webcam, gameplay fills the
  screen.
- Caption style `tiktok` = white box with black text, `impact` = big white outlined text.
- Clips are rendered at 30 fps by default (smoother on a busy PC, smaller files).

### Auto download and auto delete

Quick switches **⬇ Auto download** and **🗑 Delete after download**:
- Auto download copies every finished clip to `Videos\HypeClipper` (change it in Settings >
  Download folder): the 9:16 mp4 and a `.txt` with the title, caption, hashtags and upload links.
- Delete after download then removes it from the dashboard (after any auto uploads finish).
- **💾 Save** on a clip card saves one clip by hand.
- **Change the folder** in Downloads & uploads > Downloads: **📁 Change folder** opens a normal
  Windows folder window, or type a path (e.g. `D:\Clips\GTA6`) and click **Save path**.
  **Reset** goes back to `Videos\HypeClipper`. The app checks it can write there first; if the
  folder later disappears (unplugged drive), it saves to the default folder instead.
- **Sort into subfolders**: one per streamer, one per day, or streamer \ day.

### Uploading to YouTube (one-time setup, ~10 minutes, free)

1. Go to https://console.cloud.google.com, create a project (e.g. "HypeClipper").
2. **APIs & Services > Library**: enable **YouTube Data API v3**.
3. **APIs & Services > OAuth consent screen** (Google Auth Platform): type **External**, fill in
   the app name and your email. Under **Audience** add your Google account as a test user,
   then click **Publish app** ("In production"). Otherwise the login stops working every 7 days.
   For your own use you don't need Google's verification; you'll just see an "unverified app"
   warning when connecting (click Advanced > Go to HypeClipper).
4. **Credentials > Create credentials > OAuth client ID > Desktop app**. Download the JSON.
5. In the dashboard (Downloads & uploads > YouTube): **Upload client_secret.json**, then
   **Connect YouTube** and sign in with the account that owns the channel.

Uploads use the title + `#shorts`, the caption, hashtags, category Gaming, and the visibility
from Settings (public / unlisted / private). Google's default quota allows 100 uploads a day.

### Uploading to Instagram (one-time setup, ~15 minutes, free)

1. Your Instagram account must be **Professional** (Creator or Business).
2. Go to https://developers.facebook.com > **My Apps > Create app**, use case
   **"Manage messaging & content on Instagram"**.
3. In the app: **Instagram > API setup with Instagram login**. Under "Generate access tokens"
   click **Add account** and log in with your Instagram account (add it as an Instagram tester
   in **App roles** if asked, then accept the invite in Instagram: Settings > Website
   permissions > Apps and websites > Tester invites).
4. Click **Generate token**, copy it, and paste it in the dashboard (Downloads & uploads >
   Instagram > Connect). The app refreshes the token automatically (it lasts 60 days and is
   renewed weekly while the app runs).

The app doesn't need Meta's app review for your own account. Instagram allows 50 API posts per
24 hours. Uploads go directly from your PC (resumable upload); if Instagram rejects one, the
error shows on the clip card and you can post it by hand from the download folder.

### Automatic uploads

**▶ Auto YouTube** / **📷 Auto Instagram** upload new clips automatically when:
- the AI scored them at least **7/10** (Settings > Auto-upload only clips scored at least),
- the platform is connected,
- at least **20 minutes** passed since the last auto upload there (Settings), so the channel
  doesn't look like spam. ✂ Clip now clips are never auto-uploaded; use the buttons on the card.

Watch the first few uploads before leaving it alone. Uploading other streamers' content without
permission can get copyright strikes; see the Copyright note.

### Starting, stopping, restarting

- **Stop**: click **⏻ Stop app** in the dashboard header, or double-click `stop.bat` (also frees
  the AI model's memory).
- **Start**: double-click `start.bat`. It first stops an old HypeClipper that is still running, so
  updating is: download, replace the folder, double-click `start.bat`.

### Quick switches (top of the dashboard)

One click, applies to the next clips:
- **🎯 Clean mode**: no sound effects, no intro whoosh, no zoom punch. Just the real moment and
  the streamer's reaction (captions stay; switch them off with their own buttons if you want).
- **✨ Full edit**: sound effects, whoosh and zoom punches back on.
- Separate switches: 🔊 sound effects, 💨 intro whoosh, 🔍 zoom punch, 📝 top captions,
  💬 word captions, ✂ AI best cut.

### Only the best moments

A chat spike alone isn't enough anymore. A moment has to pass four checks:
1. **Different people react**: at least 6 different chatters at the peak (a few people
   spamming doesn't count), and moments where many different people react score higher.
2. **Strong enough**: hype score at least 3x, and with **Best moments only** it must beat 60% of
   this stream's recent hype moments, so a busy stream doesn't clip every little spike.
3. **The AI judges it**: after reading the chat, the speech and what's on screen, the AI explains
   what happens, gives the clip a 1-10 virality score with a reason, and clips under **Min AI clip
   score** (5) are thrown away. They show as "skipped" with the reason in Processing.
   Without AI, the app's own score (hype, number of chatters, speech, loud moments) is used.
4. **Best cut**: the app records 15 s more before and 10 s more after, and the AI picks the part
   worth posting, keeping 10-15 s of build-up and the whole reaction. Clips are 30-58 s
   (settings: Shortest / Longest clip).
   **✂ Clip now** clips are never skipped or trimmed.

### When good clips get skipped

- **Skipped moments** on the dashboard lists every moment that wasn't clipped and why (too few
  different chatters, weaker than usual, queue full...). **✂ Clip anyway** makes the clip while
  that part of the video is still in the 5-minute buffer.
- Clips the AI scores low are **kept for review** (dashed border, "⚠ Review") instead of being
  deleted. **✓ Keep it** approves one, and only then is it auto-downloaded/uploaded.
- A clip is kept even with a low AI score when chat hype is x8 or more, or the app's own score
  is clearly good (small AI models are often too strict).
- When more than 3 clips are waiting, the AI skips looking at the video for the next ones so the
  queue catches up instead of filling up.
- Softer defaults: min AI score 4, min hype x2.5, "best moments" must beat 35% of the stream's
  recent hype (was 60%), queue of 15. Small chats need fewer different chatters. Saved settings
  that still had the old defaults are updated automatically.

### Layouts and video quality

- **facecam** (default): click **🎥 webcam** on a streamer's card, drag a box around the webcam
  on the snapshot and save. Clips then show the webcam on top and gameplay below, split by a
  thin yellow line. Streamers without a webcam box use **blur**.
- **blur**: the 16:9 video zoomed 1.2x (setting) on a blurred background.
- **Zoom punch**: a quick zoom-in on the strongest moments.
- Sharper output: lanczos scaling, light sharpening, higher NVENC quality. Record at
  **1080p60** (Settings > Recording quality: `1080p60,1080p,720p60,best`) for the best result;
  it needs about 6-8 Mbps per streamer.

### How the AI writes each clip

1. Frames are taken at the start, at each key moment and near the end.
2. The vision model describes each frame ("police cars surround the bank, red BUSTED text").
3. The chat is analyzed: speed over time, how many different people reacted, the most repeated
   messages, and what chatters actually wrote right after each moment.
4. The text model gets all of it, first explains what happens and why chat reacted, scores it,
   picks the best cut, and writes the title, 4–7 timed captions, the description, hashtags
   and which sound effects go where (and why).
5. Generic lines ("what just happened", "chat went crazy"...) and captions/titles used in the
   last 12 clips are banned, so clips don't repeat each other.

Without Ollama, template captions are used. They quote the streamer's own words at the key
moments, use a larger set of lines, and also avoid the last clips' lines and sounds.
Each clip's details show what the AI saw, the captions and why each sound was picked. Every
clip folder also has `ai_debug.txt` with the AI's raw answers, for troubleshooting.

### Caption looks

- **Top caption style** (Settings): `pop` (default) is big white text with a thick black outline,
  a small tilt, a bounce-in animation and the key word coloured by the mood of the moment:
  yellow = funny/setup, green = hype, orange = shock, red = fail, purple = scary, cyan = bruh.
  `box` puts the text in a box with the same mood colours. `classic` is the old black box.
- **Word highlight colour**: colour of the word being spoken (yellow, green, red, cyan, pink). The
  active word also bounces.

## Updating

After downloading a new version, **close the black HypeClipper window and run `start.bat` again**.
Refreshing the browser alone loads the new dashboard but keeps the old engine running; the
dashboard shows a red bar when that happens.

## Tuning

| Setting | What it does |
|---|---|
| Sensitivity (2.5) | Chat must be this many times faster than normal. Lower = more clips. |
| Min chat speed (1.0/s) | Ignores "spikes" in tiny chats. |
| Cooldown (90 s) | Minimum gap between auto clips of one streamer. |
| Seconds before/after (20/20) | Clip window around the peak. |
| Max sound effects per clip (3) | Not counting the intro whoosh. Set 0 or turn sound effects off to disable. |
| Sound effect volume (0.8) | Volume of effects compared to the stream. |
| Layout | `blur` = full 16:9 video on a blurred background, `crop` = fill the screen (cuts the sides). |
| Recording quality | `720p60,720p,720p30,best` keeps bandwidth low. Use `1080p60,best` for sharper crop layouts. |
| Whisper model | `large-v3-turbo` (best), `medium`, `small` (fastest). |

## Memory and "WinError 8"

`[WinError 8] Not enough memory resources` means Windows ran out of memory (RAM + page file).
With many streamers, Chrome, Whisper and the AI model all at once, 24 GB fills up. The app now:
- waits until at least 3 GB is free before working on a clip (shown as "waiting for free memory"),
- retries a step that hit WinError 8 instead of failing the clip,
- in **Low memory mode** (on by default) lets Whisper and the AI model take turns on the GPU and
  unloads them after each clip, uses a smaller AI context and fewer, smaller frames,
- keeps at most 6 clips waiting; weaker new moments are skipped while it's full,
- records at 1080p only with up to 4 streamers (`auto` quality), 720p above that.

The header shows free RAM and the internet speed all recordings need. If a card says
"⚠ video dropped", that recording is missing pieces (choppy clips): your internet or PC can't keep
up, so watch fewer streamers.

If it still happens: let Windows manage the page file size (Settings > System > About > Advanced
system settings > Performance > Advanced > Virtual memory > "Automatically manage"), close
other heavy apps, or set Whisper model to `small`.

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
- **Kick chat keeps reconnecting**: Kick moved its chat to a new system (Centrifugo) on
  13 Oct 2026. The app now connects the same way the kick.com website does: it asks Kick which chat
  server to use, gets an anonymous guest token and subscribes to the chatroom. If it breaks
  again, the status on the streamer card shows Kick's error; send it in an issue.
- **Captions say "CPU"/are slow**: the GPU libraries didn't load. Make sure your NVIDIA driver is
  up to date. The app falls back to CPU automatically, which is slower but still works.
- **No word captions at the bottom**: they only appear while the streamer is talking. Open the
  clip's "Captions, sounds, transcript & chat" section: if Speech says "no speech detected",
  Whisper heard nothing (music, game audio only). If it's empty on every clip, check the console
  window for a `captions failed` line.
- **encoder: libx264** in the header: NVENC isn't available. Update the NVIDIA driver.

## Copyright note

Clips belong to the streamers. Many streamers allow or pay for clips (clipping programs), but get
permission before posting someone else's content, especially if you monetize it.
