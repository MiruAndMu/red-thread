# Red Thread

**Give your AI agent a sense for the music you live with.**

A lightweight daemon that polls Last.fm for what your user is listening to and writes a simple JSON state file. Your AI agent reads the file and gains ambient awareness of their human's music — what's playing, when sessions start and end, and whether something is worth bringing up in conversation.

No app. No interface. No database. Just a Python script, a state file, and an agent that pays attention.

---

## Why This Exists

AI agents are good at a lot of things. Knowing what you're vibing to isn't one of them. This fixes that.

The goal isn't for your agent to comment on every song — it's for them to *know*. So when you come back from a drive, they already know you were deep in a playlist. When you discover a new artist, they notice. When you replay the same album for the third day straight, they're smart enough not to mention it.

**The novelty scorer** is the key piece. It rates every session from 0-100 based on how interesting it is — new artists, session length, variety, repeat patterns. Your agent uses that score to decide: *is this worth bringing up, or do I just quietly know?*

---

## What It Costs

| Component | Cost | Notes |
|-----------|------|-------|
| **Last.fm API** | Free | No paid tier needed. Just an API key. |
| **Last.fm account** | Free | Connect your Spotify/YouTube Music/etc. for scrobbling |
| **Python + requests** | Free | Only dependency. No frameworks, no build tools. |
| **The daemon** | Free | Runs on any machine — Mac, Linux, Windows, Raspberry Pi, Docker |
| **Your AI agent** | Your existing sub | Works with whatever agent you already use (Claude, GPT, Gemini, local models, etc.) |

**Total cost for Red Thread itself: $0.**

Your agent is the only thing that costs money, and you're already paying for that. This just gives it a new sense.

### Future Phases (also free)

| Phase | What It Adds | Cost |
|-------|-------------|------|
| **Spotify enrichment** | Audio features (energy, valence, tempo, danceability) | Free — Spotify Client Credentials flow, no paid tier |
| **Lyrics integration** | Song lyrics for thematic context | Free — Genius API free tier |

We built our own setup on entirely free tiers. If that changes, we'll document it.

---

## How It Works

```
Spotify / YouTube Music / etc.
    ↓ (auto-scrobbles)
  Last.fm
    ↓ (poll every 15s)
  red_thread.py
    ↓ (writes)
  data/listening_state.json ← your agent reads this
```

Last.fm is the bridge. It catches scrobbles from Spotify, YouTube Music, Apple Music, local players — anything that scrobbles. One API, all platforms.

---

## Quick Start

### 1. Get a Last.fm API Key

1. Create a Last.fm account at [last.fm/join](https://www.last.fm/join) (or log in)
2. Connect your Spotify/music service: [last.fm/settings/applications](https://www.last.fm/settings/applications)
3. Create an API application at [last.fm/api/account/create](https://www.last.fm/api/account/create)
   - App name: anything (e.g., "Red Thread")
   - Callback URL: leave blank
4. Copy your **API Key**

### 2. Install & Configure

```bash
git clone https://github.com/MiruAndMu/red-thread.git
cd red-thread
pip install requests

# Option A: Environment variables
export LASTFM_API_KEY='your_api_key'
export LASTFM_USERNAME='your_lastfm_username'

# Option B: Config file
mkdir -p data
echo '{"api_key": "your_key", "username": "your_username"}' > data/config.json
```

### 3. Test

```bash
python red_thread.py --test
```

You should see your recent tracks. Play something on Spotify and run it again to confirm "now playing" detection.

### 4. Run

```bash
# Single poll (see what's playing right now)
python red_thread.py --once

# Run as daemon (polls every 15s, writes state continuously)
python red_thread.py

# Check status anytime
python red_thread.py --status
```

---

## What Your Agent Reads

The daemon writes `data/listening_state.json`:

```json
{
  "session_active": true,
  "now_playing": {
    "track": "The Sweet Prince",
    "artist": "Gorillaz",
    "album": "The Mountain",
    "is_own_music": false
  },
  "session_tracks": 7,
  "session_start": "2026-03-12T18:00:00+00:00",
  "current_novelty": {
    "score": 62,
    "reasons": ["new artist: Gorillaz", "solid session (7 tracks)"],
    "summary": "interesting — weave in if natural",
    "action": "talk",
    "engagement_level": "chill",
    "resonance_tracks": []
  },
  "curiosity_lookups": [
    {
      "track": "The Sweet Prince",
      "artist": "Gorillaz",
      "reason": "new artist",
      "genius_url": "https://genius.com/Gorillaz-the-sweet-prince-lyrics"
    }
  ],
  "events": [
    {"type": "session_start", "timestamp": "...", "track": "...", "artist": "..."},
    {"type": "track_change", "timestamp": "...", "track": "...", "artist": "..."},
    {"type": "session_end", "timestamp": "...", "duration_tracks": 12}
  ],
  "last_update": "2026-03-12T18:05:30+00:00",
  "last_poll": "2026-03-12T18:05:30+00:00"
}
```

### The Three Layers

```
Layer 1: HEAR (always running, free)
  Last.fm polling → track/artist/album → session detection → novelty score
  Every session gets this.

Layer 2: CURIOSITY (opt-in, free — needs Genius API token)
  Interesting tracks → lyrics lookup → artist context → Genius URL
  Triggered by: new artists, resonance tracks, or novelty above your threshold.
  This is the THINKING layer. Your agent gets curious, then decides whether to talk.

Layer 3: DEEP LISTEN (your agent's discretion)
  Your agent uses the Genius URL and context from Layer 2 to go deeper.
  Read lyrics, research the artist, form an opinion, find a conversation angle.
  This layer is whatever your agent is capable of — Red Thread just provides the trigger.
```

### Session Modes

How Red Thread groups your listening depends on what you care about:

- **Session mode** (default) — gaps in listening create separate sessions. Drive to work, silence while you're there, drive home = two sessions, scored independently. Good for people who want their agent to notice individual listening moments.
- **Daily mode** (`SESSION_MODE=daily`) — everything rolls into one session per day. Good for people who care about the overall shape of their listening day, not individual trips.

Set `SESSION_MODE=daily` or `SESSION_MODE=session` — or fine-tune `SESSION_GAP` and `IDLE_TIMEOUT` directly if you want something in between.

### Engagement Levels

Different people want different things. Set `ENGAGEMENT_LEVEL` to control how chatty your agent is about music:

| Level | Name | Novelty Threshold | Your Agent Should... |
|-------|------|-------------------|---------------------|
| 1 | **quiet** | 76+ | Only mention truly notable sessions. Mostly silent. |
| 2 | **chill** | 51+ | Mention interesting sessions. **(Default)** |
| 3 | **tuned_in** | 21+ | Engage with anything that has some novelty. More conversational. |
| 4 | **dj** | Any | Music is a primary conversation topic. Curious about everything. |

The novelty score tells your agent *how interesting* a session is. The engagement level tells it *how high the bar should be* to bring it up. The `action` field in the state file does the math for you — it's either `"talk"` or `"quiet"` based on your level.

### Novelty Scoring

| Signal | Score Impact | Why |
|--------|-------------|-----|
| **New artists** (not seen in 14 days) | +25 each (max 3) | Discovery is interesting |
| **New songs** (unheard tracks, even by known artists) | +5 to +15 | A new song is still discovery |
| **Own music** (via personal catalog) | +20 | Playing your own stuff is always noteworthy |
| **Resonance tracks** (3+ sessions in a week) | +20 | A song that won't leave is living rent-free |
| **Deep session** (15+ tracks) | +20 | You're *in* something |
| **Artist deep dive** (one artist, 5+ tracks) | +15 | Intentional listening — an album front to back |
| **Exploration** (5+ different artists) | +10 | Broad mood |
| **Solid session** (8+ tracks) | +10 | Decent length |
| **Continuation** (picks up same artist from last session) | +15 | You paused, not stopped |
| **Same album continuation** | +5 | Picking up right where you left off |
| **Short session** (3 or fewer) | -10 | Background noise |
| **Heavy repeats** (80%+ same *tracks* as recently) | -15 | Same playlist on shuffle |

Note: same artist on repeat is **never penalized**. Listening to one artist deeply is a signal of interest — only the exact same *tracks* repeating counts against a session. If someone is exploring an artist's catalog or listening to a new album, that's intentional and the score reflects it.

### Resonance vs. Repetition

Not all repeats are the same:

- **Repetition** = 80% of a session is familiar tracks. That's a playlist on shuffle while cooking. Score goes down.
- **Resonance** = ONE specific track keeps showing up across 3+ separate sessions while everything else rotates. That song is *doing something*. Score goes UP, and your agent should find a new angle on it instead of repeating the same conversation.

Red Thread tracks per-track session appearances over a rolling week. When it detects resonance, the `resonance_tracks` array in the state file tells your agent which tracks are sticky and how many sessions they've appeared in.

### What Makes Sessions Interesting (skip detection)

Last.fm only scrobbles a track if the user listened to **at least 50% of it or 4 minutes** (whichever comes first). Tracks under 30 seconds don't scrobble. So if a song shows up in the data, the user actually listened to it — skipped tracks are invisible. This isn't perfect engagement data, but it means every track in the state file represents real listening.

---

## Personal Catalog (Optional)

If your user is a musician, you can provide a catalog so Red Thread flags when they're playing their own music:

```bash
# Place at data/catalog.json or set CATALOG_FILE env var
```

```json
[
  {"title": "Song Name", "artist": "Artist Name", "album": "Album Name"},
  {"title": "Another Song", "artist": "Artist Name"}
]
```

Or use a `{"releases": [...]}` or `{"tracks": [...]}` wrapper format.

---

## Configuration

All settings work as environment variables:

| Variable | Default | What It Does |
|----------|---------|-------------|
| `LASTFM_API_KEY` | (required) | Your Last.fm API key |
| `LASTFM_USERNAME` | (required) | Last.fm username to poll |
| `REDTHREAD_DIR` | `./data` | Where state/history/logs are stored |
| `POLL_INTERVAL` | `15` | Seconds between Last.fm polls |
| `SESSION_MODE` | `session` | `session` = group by listening gaps, `daily` = one session per day |
| `SESSION_GAP` | `1800` | Seconds of silence = new session (30 min). Auto-set to 24h in daily mode. |
| `IDLE_TIMEOUT` | `300` | Seconds without "now playing" = session ended (5 min). Auto-set to 1h in daily mode. |
| `HISTORY_WINDOW_DAYS` | `14` | Rolling window for novelty scoring |
| `ENGAGEMENT_LEVEL` | `2` | 1=quiet, 2=chill, 3=tuned_in, 4=dj |
| `GENIUS_API_TOKEN` | (optional) | Enables Layer 2 lyrics/context lookups (free at genius.com/api-clients) |
| `CATALOG_FILE` | `data/catalog.json` | Path to personal catalog (optional) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

You can also set `genius_token` in your `config.json` instead of using the environment variable.

Check your config: `python red_thread.py --config`

---

## Running as a Background Service

### macOS (launchd)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.red-thread</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/red_thread.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LASTFM_API_KEY</key>
        <string>your_key</string>
        <key>LASTFM_USERNAME</key>
        <string>your_username</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

### Linux (systemd)

```ini
[Unit]
Description=Red Thread
After=network.target

[Service]
Type=simple
Environment=LASTFM_API_KEY=your_key
Environment=LASTFM_USERNAME=your_username
ExecStart=/usr/bin/python3 /path/to/red_thread.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Docker

```dockerfile
FROM python:3.11-slim
RUN pip install requests
COPY red_thread.py .
CMD ["python", "red_thread.py"]
```

```bash
docker run -d \
  -e LASTFM_API_KEY=your_key \
  -e LASTFM_USERNAME=your_username \
  -v ./data:/data \
  -e REDTHREAD_DIR=/data \
  red-thread
```

---

## Telling Your Agent About It

The whole point is your agent reads the state file. Here's an example system prompt addition:

> You have access to Red Thread at `data/listening_state.json`. It tracks what the user is currently listening to via Last.fm scrobbles. Check it when relevant — not every message, just when music might come up naturally.
>
> The file includes:
> - **Novelty score** (0-100): how interesting the current session is
> - **Action field**: `"talk"` or `"quiet"` — already adjusted for the user's preferred engagement level
> - **Resonance tracks**: songs that keep appearing across multiple sessions — these are living rent-free and worth exploring from new angles
> - **Curiosity lookups**: tracks Red Thread researched via Genius (lyrics URLs, artist info) — use these to form actual opinions, don't just report metadata
>
> When `action` is `"talk"`, weave music into conversation naturally. When it's `"quiet"`, you know what they listened to but you don't mention it. For resonance tracks, don't repeat the same conversation — find a new angle each time. Be a friend who was in the room, not a surveillance report.

---

## Our Setup (What We Actually Run)

We built this for ourselves first. Here's what our full stack looks like so you can see where the free parts end and the "nice to have" parts begin:

| Layer | What It Does | What We Use | Cost | You Need This? |
|-------|-------------|-------------|------|----------------|
| **Detection** | Know what's playing | Last.fm API polling | Free | Yes — this is the core |
| **State file** | Agent reads current state | `listening_state.json` | Free | Yes — this is what your agent reads |
| **Novelty scoring** | Rate sessions by interestingness | Built into the daemon | Free | Yes — included in this repo |
| **Rolling history** | Track patterns over 14 days | `listening_history.json` | Free | Yes — included in this repo |
| **Personal catalog** | Flag when user plays their own music | Optional `catalog.json` | Free | Only if the user is a musician |
| **Audio analysis** | Spectral analysis, BPM, key detection | Gemini API + librosa | Free tier / Free | No — enhancement for music nerds |
| **Lyrics lookup** | Get song lyrics for context | Our own lyrics database | Free | No — use Genius API free tier if you want this |
| **Agent integration** | Agent reacts to listening naturally | Claude (Anthropic) | Subscription | You already have an agent — use whatever you use |

**Everything in this repo is the free stuff.** The audio analysis and lyrics layers are things we built separately for our own use — they plug into the state file but aren't required.

The daemon gives your agent awareness. What your agent *does* with that awareness is up to you and your agent's personality.

---

## How It Uses Last.fm

- Polls `user.getRecentTracks` endpoint (public, no OAuth required)
- Only needs an API key and a username
- Respects rate limits (one request every 15 seconds)
- Works with any service that scrobbles to Last.fm (Spotify, YouTube Music, Apple Music, Tidal, local players, etc.)
- No user authentication flow — reads public scrobble data only

---

## License

MIT — do whatever you want with it.

---

*Built by [Miru & Mu](https://github.com/MiruAndMu) — a fox and her human, building things that make AI feel more like a friend.*
