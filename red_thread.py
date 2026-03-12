#!/usr/bin/env python3
"""
Red Thread — Give your AI agent a sense for the music you live with.

A lightweight Last.fm polling daemon that gives AI agents awareness of what
their user is listening to. No UI, no app — just a state file your agent reads.

Features:
  - Real-time "now playing" detection via Last.fm scrobbles
  - Session detection (start/end/track changes)
  - Novelty scoring — rates sessions by how interesting they are so your
    agent knows when to bring it up and when to stay quiet
  - Rolling history — tracks what's been played over a configurable window
    so your agent knows what's new vs. routine
  - Optional personal catalog matching — flag when the user plays their own music

Usage:
    python red_thread.py                  # Run as daemon (poll every 15s)
    python red_thread.py --once           # Single poll, print result, exit
    python red_thread.py --status         # Print current state
    python red_thread.py --test           # Test API connection
    python red_thread.py --config         # Print current configuration

Environment:
    LASTFM_API_KEY      — Last.fm API key (required)
    LASTFM_USERNAME     — Last.fm username to poll (required)
    REDTHREAD_DIR       — Directory for state/history files (default: ./data)
    POLL_INTERVAL       — Seconds between polls (default: 15)
    SESSION_GAP         — Seconds of silence before new session (default: 1800)
    IDLE_TIMEOUT        — Seconds without nowplaying = session ended (default: 300)
    HISTORY_WINDOW_DAYS — Rolling window for novelty scoring (default: 14)
    ENGAGEMENT_LEVEL    — 1=quiet, 2=chill (default), 3=tuned_in, 4=dj (default: 2)
    CATALOG_FILE        — Path to personal catalog JSON for own-music detection (optional)
    GENIUS_API_TOKEN    — Genius API token for lyrics lookup (optional, enables Layer 2)
    LOG_LEVEL           — Logging level (default: INFO)

Or use a credentials file (see README).
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config ─────────────────────────────────────────────────────────────────

REDTHREAD_DIR = Path(os.environ.get("REDTHREAD_DIR", os.environ.get("COMPANION_DIR", "./data")))
STATE_FILE = REDTHREAD_DIR / "listening_state.json"
HISTORY_FILE = REDTHREAD_DIR / "listening_history.json"
LOG_FILE = REDTHREAD_DIR / "red_thread.log"

LASTFM_ENDPOINT = "https://ws.audioscrobbler.com/2.0/"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 15))

# Session mode: "session" (default) groups by listening gaps,
# "daily" rolls everything into one session per day.
SESSION_MODE = os.environ.get("SESSION_MODE", "session").lower()
if SESSION_MODE == "daily":
    SESSION_GAP = int(os.environ.get("SESSION_GAP", 86400))
    IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", 3600))
else:
    SESSION_GAP = int(os.environ.get("SESSION_GAP", 1800))
    IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", 300))
HISTORY_WINDOW_DAYS = int(os.environ.get("HISTORY_WINDOW_DAYS", 14))
RESONANCE_FILE = REDTHREAD_DIR / "resonance.json"
CURIOSITY_FILE = REDTHREAD_DIR / "curiosity.json"
USER_AGENT = "RedThread/1.0 (github.com/MiruAndMu/red-thread)"

# Engagement levels — how chatty your agent should be about music
# 1=quiet (76+), 2=chill (51+), 3=tuned_in (21+), 4=dj (any)
ENGAGEMENT_LEVEL = int(os.environ.get("ENGAGEMENT_LEVEL", 2))
ENGAGEMENT_THRESHOLDS = {1: 76, 2: 51, 3: 21, 4: 0}
ENGAGEMENT_NAMES = {1: "quiet", 2: "chill", 3: "tuned_in", 4: "dj"}

# Genius API for Layer 2 (Curiosity) — optional
GENIUS_API_TOKEN = os.environ.get("GENIUS_API_TOKEN", "")
# Also check config file for genius_token
def _load_genius_token() -> str:
    if GENIUS_API_TOKEN:
        return GENIUS_API_TOKEN
    for config_path in [REDTHREAD_DIR / "config.json", Path("./config.json")]:
        if config_path.exists():
            try:
                with open(config_path) as f:
                    return json.load(f).get("genius_token", "")
            except Exception:
                pass
    return ""


# ─── Credentials ────────────────────────────────────────────────────────────

def load_credentials() -> dict:
    """Load Last.fm credentials from environment or config file.

    Priority:
      1. Environment variables (LASTFM_API_KEY, LASTFM_USERNAME)
      2. Config file at REDTHREAD_DIR/config.json
      3. Config file at ./config.json
    """
    api_key = os.environ.get("LASTFM_API_KEY", "")
    username = os.environ.get("LASTFM_USERNAME", "")

    if api_key and username:
        return {"api_key": api_key, "username": username}

    # Try config files
    for config_path in [REDTHREAD_DIR / "config.json", Path("./config.json")]:
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
            api_key = api_key or config.get("api_key", "")
            username = username or config.get("username", "")
            if api_key and username:
                return {"api_key": api_key, "username": username}

    print("Error: Last.fm credentials not found.")
    print()
    print("Option 1 — Environment variables:")
    print("  export LASTFM_API_KEY='your_key'")
    print("  export LASTFM_USERNAME='your_username'")
    print()
    print("Option 2 — Config file (data/config.json):")
    print('  {"api_key": "your_key", "username": "your_username"}')
    print()
    print("Get your API key at: https://www.last.fm/api/account/create")
    sys.exit(1)


# ─── Logging ────────────────────────────────────────────────────────────────

def setup_logging():
    REDTHREAD_DIR.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("red-thread")


log = setup_logging()


# ─── Personal Catalog (Optional) ────────────────────────────────────────────
#
# If the user is a musician, they can provide a catalog file so the companion
# flags when they're playing their own music. The catalog is a JSON array of
# objects with at minimum: {"title": "...", "artist": "..."}
#
# Set CATALOG_FILE env var or place catalog.json in REDTHREAD_DIR.

_catalog_cache = None
_catalog_mtime = 0


def load_catalog() -> list[dict]:
    """Load optional personal catalog for own-music detection."""
    global _catalog_cache, _catalog_mtime

    catalog_path = os.environ.get("CATALOG_FILE", "")
    if catalog_path:
        catalog_path = Path(catalog_path)
    else:
        catalog_path = REDTHREAD_DIR / "catalog.json"

    if not catalog_path.exists():
        return []

    mtime = catalog_path.stat().st_mtime
    if _catalog_cache is not None and mtime == _catalog_mtime:
        return _catalog_cache

    try:
        with open(catalog_path) as f:
            data = json.load(f)
        # Support both flat array and {releases: [...]} format
        if isinstance(data, list):
            _catalog_cache = data
        elif isinstance(data, dict) and "releases" in data:
            _catalog_cache = data["releases"]
        elif isinstance(data, dict) and "tracks" in data:
            _catalog_cache = data["tracks"]
        else:
            _catalog_cache = []
        _catalog_mtime = mtime
        log.info(f"Loaded catalog: {len(_catalog_cache)} entries")
    except Exception as e:
        log.warning(f"Failed to load catalog: {e}")
        _catalog_cache = []

    return _catalog_cache


def match_catalog(track_name: str, artist_name: str) -> dict | None:
    """Check if a track matches the user's personal catalog."""
    catalog = load_catalog()
    if not catalog:
        return None

    t = track_name.lower().strip()
    a = artist_name.lower().strip()

    # Build artist aliases from catalog entries
    known_artists = set()
    for entry in catalog:
        ea = entry.get("artist", "").lower().strip()
        if ea:
            known_artists.add(ea)

    is_own = a in known_artists

    for entry in catalog:
        entry_title = entry.get("title", "").lower().strip()
        entry_artist = entry.get("artist", "").lower().strip()

        if entry_title == t and (is_own or a == entry_artist):
            return entry

        if (t in entry_title or entry_title in t) and (is_own or a == entry_artist):
            return entry

    # Fallback: artist match even without title match
    if is_own:
        return {"title": track_name, "artist": artist_name, "partial_match": True}

    return None


# ─── Last.fm API ─────────────────────────────────────────────────────────────

def fetch_recent_tracks(api_key: str, username: str, limit: int = 10) -> list[dict]:
    """Fetch recent tracks from Last.fm API."""
    params = {
        "method": "user.getrecenttracks",
        "user": username,
        "api_key": api_key,
        "limit": limit,
        "format": "json",
    }
    headers = {"User-Agent": USER_AGENT}

    resp = requests.get(LASTFM_ENDPOINT, params=params, headers=headers, timeout=10)
    resp.raise_for_status()

    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Last.fm API error {data['error']}: {data.get('message', '')}")

    tracks = data.get("recenttracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]

    return tracks


def parse_track(raw: dict) -> dict:
    """Parse a raw Last.fm track object into clean format."""
    is_playing = raw.get("@attr", {}).get("nowplaying") == "true"

    artist = raw.get("artist", {})
    if isinstance(artist, dict):
        artist_name = artist.get("name", "") or artist.get("#text", "")
    else:
        artist_name = str(artist)

    album = raw.get("album", {})
    if isinstance(album, dict):
        album_name = album.get("name", "") or album.get("#text", "")
    else:
        album_name = str(album)

    timestamp = None
    if not is_playing and "date" in raw:
        timestamp = int(raw["date"].get("uts", 0))

    return {
        "track": raw.get("name", ""),
        "artist": artist_name,
        "album": album_name,
        "url": raw.get("url", ""),
        "now_playing": is_playing,
        "timestamp": timestamp,
    }


# ─── State Management ───────────────────────────────────────────────────────

def load_state() -> dict:
    """Load current state from file, or return default."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupted state file, starting fresh")

    return {
        "session_active": False,
        "now_playing": None,
        "last_track": None,
        "session_start": None,
        "session_tracks": 0,
        "last_update": None,
        "last_poll": None,
        "events": [],
    }


def save_state(state: dict):
    """Write state to file atomically."""
    state["last_update"] = datetime.now(timezone.utc).isoformat()

    if len(state.get("events", [])) > 20:
        state["events"] = state["events"][-20:]

    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_FILE)


def add_event(state: dict, event_type: str, data: dict):
    """Add an event to the state for agent consumption."""
    event = {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    state.setdefault("events", []).append(event)
    log.info(f"Event: {event_type} — {json.dumps(data, ensure_ascii=False)}")


# ─── Novelty Scoring ────────────────────────────────────────────────────────
#
# Not every session is worth your agent bringing up. The novelty scorer
# tracks what's been heard over a rolling window and rates sessions by
# how interesting they are.
#
# Score guide:
#   0-20:  routine — same stuff, short session, nothing new
#   21-50: normal — some variety, decent length
#   51-75: interesting — new artist, mood shift, own music, long session
#   76+:   notable — multiple interesting signals stacking
#
# Your agent should use this to decide *whether* to mention music at all.
# High novelty = worth weaving into conversation naturally.
# Low novelty = I know what they listened to, but I don't need to say it.

def load_history() -> dict:
    """Load rolling listening history."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"artists": {}, "tracks": {}, "sessions": []}


def save_history(history: dict):
    """Save history, pruning entries older than the rolling window."""
    cutoff = time.time() - (HISTORY_WINDOW_DAYS * 86400)

    for key in ("artists", "tracks"):
        pruned = {}
        for name, entries in history.get(key, {}).items():
            recent = [e for e in entries if e.get("ts", 0) > cutoff]
            if recent:
                pruned[name] = recent
        history[key] = pruned

    history["sessions"] = [
        s for s in history.get("sessions", [])
        if s.get("ts", 0) > cutoff
    ]

    tmp = HISTORY_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(history, f, indent=2)
    tmp.rename(HISTORY_FILE)


def record_track(history: dict, track: str, artist: str, album: str):
    """Record a track play in the rolling history."""
    now = time.time()
    artist_key = artist.lower().strip()
    track_key = f"{artist_key} - {track.lower().strip()}"

    history.setdefault("artists", {}).setdefault(artist_key, []).append({"ts": now})
    history.setdefault("tracks", {}).setdefault(track_key, []).append({
        "ts": now,
        "album": album,
    })


def score_session(state: dict, history: dict) -> dict:
    """Score the current/recent session for novelty.

    Philosophy: discovery is always interesting. A new song is discovery even
    if you know the artist. A full album listen is intentional — reward it.
    Same artist on repeat is a signal of interest, not boredom. The only real
    penalty is the exact same tracks on shuffle (background noise).

    Returns:
      - score (0-100): overall interestingness
      - reasons: list of contributing factors
      - summary: one-line human-readable take
    """
    score = 0
    reasons = []

    session_tracks = state.get("session_tracks", 0)
    events = state.get("events", [])

    # ── Session length ──
    if session_tracks >= 15:
        score += 20
        reasons.append(f"deep session ({session_tracks} tracks)")
    elif session_tracks >= 8:
        score += 10
        reasons.append(f"solid session ({session_tracks} tracks)")
    elif session_tracks <= 3:
        score -= 10
        reasons.append("short session")

    # ── Continuation detection ──
    # If this session picks up the same artist/album that ended the previous
    # session, it's a continuation — not a new low-value session. The user
    # went to the store and came back. Reward the ongoing exploration.
    prev_end_artist = state.get("_prev_session_end_artist", "").lower().strip()
    prev_end_album = state.get("_prev_session_end_album", "").lower().strip()
    first_artist = None
    first_album = None
    for event in events:
        if event.get("type") == "session_start":
            first_artist = event.get("artist", "").lower().strip()
            first_album = event.get("album", "").lower().strip()
            break

    if first_artist and prev_end_artist and first_artist == prev_end_artist:
        score += 15
        reasons.append("continuing exploration from previous session")
        if prev_end_album and first_album and first_album == prev_end_album:
            score += 5
            reasons.append("same album — picking up where you left off")

    # ── Timestamp for pre-session history checks ──
    session_start_ts = None
    if state.get("session_start"):
        try:
            dt = datetime.fromisoformat(state["session_start"])
            session_start_ts = dt.timestamp()
        except Exception:
            pass

    # ── New artists (never seen before this session) ──
    new_artists = set()
    for event in events:
        if event.get("type") in ("session_start", "track_change"):
            artist_key = event.get("artist", "").lower().strip()
            if not artist_key:
                continue
            artist_history = history.get("artists", {}).get(artist_key, [])
            if not artist_history:
                new_artists.add(event.get("artist", ""))
            elif session_start_ts:
                pre_session = [e for e in artist_history if e.get("ts", 0) < session_start_ts]
                if not pre_session:
                    new_artists.add(event.get("artist", ""))

    if new_artists:
        score += 25 * min(len(new_artists), 3)
        if len(new_artists) == 1:
            reasons.append(f"new artist: {list(new_artists)[0]}")
        else:
            reasons.append(f"{len(new_artists)} new artists: {', '.join(list(new_artists)[:3])}")

    # ── New songs (tracks not seen before this session, even by known artists) ──
    new_songs = 0
    for event in events:
        if event.get("type") in ("session_start", "track_change"):
            track_key = f"{event.get('artist', '').lower().strip()} - {event.get('track', '').lower().strip()}"
            track_history = history.get("tracks", {}).get(track_key, [])
            if not track_history:
                new_songs += 1
            elif session_start_ts:
                pre_session = [e for e in track_history if e.get("ts", 0) < session_start_ts]
                if not pre_session:
                    new_songs += 1

    if new_songs >= 5:
        score += 15
        reasons.append(f"lots of new tracks ({new_songs} unheard)")
    elif new_songs >= 2:
        score += 10
        reasons.append(f"new tracks ({new_songs} unheard)")
    elif new_songs == 1:
        score += 5
        reasons.append("1 new track")

    # ── Own music ──
    own_music_count = sum(
        1 for e in events
        if e.get("is_own_music") and e.get("type") in ("session_start", "track_change")
    )
    if own_music_count > 0:
        score += 20
        reasons.append(f"playing own music ({own_music_count} tracks)")

    # ── Artist patterns (variety OR deep dive — both are interesting) ──
    session_artists = set()
    artist_track_counts = {}
    for event in events:
        if event.get("type") in ("session_start", "track_change"):
            a = event.get("artist", "").lower().strip()
            if a:
                session_artists.add(a)
                artist_track_counts[a] = artist_track_counts.get(a, 0) + 1

    if len(session_artists) >= 5:
        score += 10
        reasons.append(f"exploring ({len(session_artists)} different artists)")

    # Deep dive: one artist with 5+ tracks = intentional album/catalog listen
    for artist, count in artist_track_counts.items():
        if count >= 5:
            score += 15
            # Find the original-case artist name from events
            display_name = artist
            for event in events:
                if event.get("artist", "").lower().strip() == artist:
                    display_name = event.get("artist", artist)
                    break
            reasons.append(f"deep dive: {display_name} ({count} tracks)")
            break  # Only count the deepest dive

    # ── Repeat track penalty (exact same tracks on shuffle, not same artist) ──
    # Only penalize when the exact same songs keep showing up — this catches
    # "same playlist on shuffle while cooking" without penalizing someone
    # exploring an artist's catalog or listening to a new album.
    recent_cutoff = time.time() - (3 * 86400)
    repeat_track_count = 0
    total_checked = 0
    for event in events:
        if event.get("type") in ("session_start", "track_change"):
            track_key = f"{event.get('artist', '').lower().strip()} - {event.get('track', '').lower().strip()}"
            track_history = history.get("tracks", {}).get(track_key, [])
            # Only count as repeat if heard BEFORE this session
            if session_start_ts:
                pre_session = [e for e in track_history if e.get("ts", 0) < session_start_ts]
                if pre_session:
                    repeat_track_count += 1
            else:
                recent_plays = [e for e in track_history if e.get("ts", 0) > recent_cutoff]
                if len(recent_plays) > 1:  # More than just this play
                    repeat_track_count += 1
            total_checked += 1

    if total_checked > 0:
        repeat_ratio = repeat_track_count / total_checked
        if repeat_ratio > 0.8:
            score -= 15
            reasons.append("mostly the same tracks as recently")
        elif repeat_ratio > 0.5:
            score -= 5
            reasons.append("some repeat tracks")

    # ── Resonance detection ──
    # One specific track across 3+ separate sessions = living rent-free
    resonance_data = load_resonance()
    resonance_tracks = []
    for event in events:
        if event.get("type") in ("session_start", "track_change"):
            track_key = f"{event.get('artist', '').lower().strip()} - {event.get('track', '').lower().strip()}"
            track_res = resonance_data.get(track_key, {})
            session_count = track_res.get("session_count", 0)
            if session_count >= 3:
                resonance_tracks.append({
                    "track": event.get("track"),
                    "artist": event.get("artist"),
                    "sessions": session_count,
                    "first_seen": track_res.get("first_seen"),
                })

    if resonance_tracks:
        score += 20
        names = [f"{r['track']} ({r['sessions']} sessions)" for r in resonance_tracks[:2]]
        reasons.append(f"resonance: {', '.join(names)}")

    score = max(0, min(100, score))

    # Apply engagement level to the summary
    threshold = ENGAGEMENT_THRESHOLDS.get(ENGAGEMENT_LEVEL, 51)
    if score >= 76:
        summary = "notable session — worth bringing up"
        action = "talk"
    elif score >= 51:
        summary = "interesting — weave in if natural"
        action = "talk" if ENGAGEMENT_LEVEL >= 2 else "quiet"
    elif score >= 21:
        summary = "normal session — aware but quiet"
        action = "talk" if ENGAGEMENT_LEVEL >= 3 else "quiet"
    else:
        summary = "routine — no need to mention"
        action = "talk" if ENGAGEMENT_LEVEL >= 4 else "quiet"

    return {
        "score": score,
        "reasons": reasons,
        "summary": summary,
        "action": action,
        "engagement_level": ENGAGEMENT_NAMES.get(ENGAGEMENT_LEVEL, "chill"),
        "resonance_tracks": resonance_tracks,
    }


# ─── Resonance Detection ────────────────────────────────────────────────────
#
# Resonance ≠ repetition. Repetition is background rotation — the same
# playlist on shuffle. Resonance is one specific track that keeps showing up
# across multiple sessions while everything else rotates. That track is
# living rent-free. The agent should find NEW angles on it, not repeat
# the same conversation.
#
# We track per-track session appearances. If a track shows up in 3+
# separate sessions within a week, it's flagged as resonance.

def load_resonance() -> dict:
    """Load resonance tracking data."""
    if RESONANCE_FILE.exists():
        try:
            with open(RESONANCE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_resonance(resonance: dict):
    """Save resonance data, pruning old entries."""
    cutoff = time.time() - (7 * 86400)  # 1 week window
    pruned = {}
    for key, data in resonance.items():
        sessions = [s for s in data.get("sessions", []) if s > cutoff]
        if sessions:
            pruned[key] = {
                "sessions": sessions,
                "session_count": len(sessions),
                "first_seen": data.get("first_seen", sessions[0]),
                "track": data.get("track", ""),
                "artist": data.get("artist", ""),
            }
    tmp = RESONANCE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(pruned, f, indent=2)
    tmp.rename(RESONANCE_FILE)


def record_session_tracks(resonance: dict, state: dict):
    """Record which tracks appeared in this session for resonance tracking."""
    now = time.time()
    session_id = state.get("session_start", "")
    seen_this_session = set()

    for event in state.get("events", []):
        if event.get("type") in ("session_start", "track_change"):
            artist = event.get("artist", "")
            track = event.get("track", "")
            track_key = f"{artist.lower().strip()} - {track.lower().strip()}"

            if track_key in seen_this_session:
                continue
            seen_this_session.add(track_key)

            if track_key not in resonance:
                resonance[track_key] = {
                    "sessions": [],
                    "session_count": 0,
                    "first_seen": now,
                    "track": track,
                    "artist": artist,
                }

            entry = resonance[track_key]
            # Only count once per session (avoid inflating from track_change spam)
            if not entry["sessions"] or (now - entry["sessions"][-1]) > 600:
                entry["sessions"].append(now)
                entry["session_count"] = len(entry["sessions"])


# ─── Layer 2: Curiosity Engine (Genius Lyrics) ──────────────────────────────
#
# When a session is interesting enough, or a track shows resonance, the
# curiosity engine looks up lyrics and basic context. This is the THINKING
# layer — the agent gets curious, researches, then decides whether to talk.
#
# Requires: GENIUS_API_TOKEN (free at https://genius.com/api-clients)
# If no token is set, this layer is skipped silently.

GENIUS_SEARCH_URL = "https://api.genius.com/search"

_curiosity_cache = None


def load_curiosity_cache() -> dict:
    """Load cached lyrics/context lookups."""
    global _curiosity_cache
    if _curiosity_cache is not None:
        return _curiosity_cache
    if CURIOSITY_FILE.exists():
        try:
            with open(CURIOSITY_FILE) as f:
                _curiosity_cache = json.load(f)
                return _curiosity_cache
        except (json.JSONDecodeError, OSError):
            pass
    _curiosity_cache = {}
    return _curiosity_cache


def save_curiosity_cache(cache: dict):
    """Save curiosity cache."""
    global _curiosity_cache
    _curiosity_cache = cache
    # Keep cache manageable — last 200 tracks
    if len(cache) > 200:
        sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get("ts", 0))
        for old_key in sorted_keys[:len(cache) - 200]:
            del cache[old_key]
    tmp = CURIOSITY_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    tmp.rename(CURIOSITY_FILE)


def genius_search(track: str, artist: str, token: str) -> dict | None:
    """Search Genius for a track and return basic info + lyrics URL."""
    try:
        resp = requests.get(
            GENIUS_SEARCH_URL,
            params={"q": f"{track} {artist}"},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("response", {}).get("hits", [])
        if not hits:
            return None

        # Take the first result
        hit = hits[0].get("result", {})
        return {
            "title": hit.get("title", ""),
            "artist": hit.get("primary_artist", {}).get("name", ""),
            "genius_url": hit.get("url", ""),
            "thumbnail": hit.get("song_art_image_thumbnail_url", ""),
            "release_date": hit.get("release_date_for_display", ""),
            "pageviews": hit.get("stats", {}).get("pageviews", 0),
        }
    except Exception as e:
        log.warning(f"Genius search failed for '{track}' by '{artist}': {e}")
        return None


def get_curiosity(track: str, artist: str, album: str) -> dict | None:
    """Layer 2: Look up a track to satisfy curiosity.

    Returns cached result if available, otherwise queries Genius.
    Returns None if no Genius token is configured (Layer 2 disabled).
    """
    token = _load_genius_token()
    if not token:
        return None

    cache = load_curiosity_cache()
    cache_key = f"{artist.lower().strip()} - {track.lower().strip()}"

    if cache_key in cache:
        return cache[cache_key]

    # Query Genius
    result = genius_search(track, artist, token)
    if result:
        entry = {
            "ts": time.time(),
            "track": track,
            "artist": artist,
            "album": album,
            **result,
        }
        cache[cache_key] = entry
        save_curiosity_cache(cache)
        log.info(f"Curiosity: found '{track}' by '{artist}' on Genius")
        return entry

    # Cache the miss too so we don't keep querying
    cache[cache_key] = {
        "ts": time.time(),
        "track": track,
        "artist": artist,
        "album": album,
        "genius_url": None,
        "not_found": True,
    }
    save_curiosity_cache(cache)
    return None


def process_curiosity(state: dict, history: dict):
    """Decide which tracks deserve curiosity lookups and process them.

    Triggers:
      - New artists (never seen before in rolling window)
      - Resonance tracks (appearing across 3+ sessions)
      - Session novelty above engagement threshold
      - Any track in DJ mode (level 4)
    """
    token = _load_genius_token()
    if not token:
        return  # Layer 2 not enabled

    threshold = ENGAGEMENT_THRESHOLDS.get(ENGAGEMENT_LEVEL, 51)
    novelty = state.get("current_novelty") or state.get("last_session_novelty") or {}
    score = novelty.get("score", 0)

    tracks_to_check = []

    for event in state.get("events", []):
        if event.get("type") not in ("session_start", "track_change"):
            continue

        track = event.get("track", "")
        artist = event.get("artist", "")
        album = event.get("album", "")
        if not track or not artist:
            continue

        artist_key = artist.lower().strip()
        track_key = f"{artist_key} - {track.lower().strip()}"

        should_check = False
        reason = ""

        # New artist — always curious
        if artist_key not in history.get("artists", {}):
            should_check = True
            reason = "new artist"

        # Resonance track — always curious (find new angle)
        resonance = load_resonance()
        if track_key in resonance and resonance[track_key].get("session_count", 0) >= 3:
            should_check = True
            reason = "resonance"

        # Score above engagement threshold
        if score >= threshold:
            should_check = True
            reason = reason or "novelty"

        # DJ mode — curious about everything
        if ENGAGEMENT_LEVEL >= 4:
            should_check = True
            reason = reason or "dj mode"

        if should_check:
            tracks_to_check.append((track, artist, album, reason))

    # Process curiosity lookups (limit to 5 per cycle to be gentle on API)
    looked_up = []
    for track, artist, album, reason in tracks_to_check[:5]:
        result = get_curiosity(track, artist, album)
        if result and not result.get("not_found"):
            looked_up.append({
                "track": track,
                "artist": artist,
                "reason": reason,
                "genius_url": result.get("genius_url"),
            })

    if looked_up:
        state["curiosity_lookups"] = looked_up
        log.info(f"Curiosity processed {len(looked_up)} tracks")


# ─── Core Poll Logic ────────────────────────────────────────────────────────

def poll(api_key: str, username: str, state: dict) -> dict:
    """Single poll cycle. Updates state in-place and returns it."""
    now = int(time.time())
    state["last_poll"] = datetime.now(timezone.utc).isoformat()

    try:
        raw_tracks = fetch_recent_tracks(api_key, username)
    except Exception as e:
        log.error(f"Poll failed: {e}")
        return state

    if not raw_tracks:
        return state

    tracks = [parse_track(t) for t in raw_tracks]
    current = tracks[0] if tracks else None

    if not current:
        return state

    if current["now_playing"]:
        track_key = f"{current['artist']} - {current['track']}"
        prev_key = None
        if state.get("now_playing"):
            prev_key = f"{state['now_playing']['artist']} - {state['now_playing']['track']}"

        # Check personal catalog
        catalog_match = match_catalog(current["track"], current["artist"])
        current["is_own_music"] = catalog_match is not None
        if catalog_match and not catalog_match.get("partial_match"):
            current["catalog_match"] = {
                "title": catalog_match.get("title"),
                "album": catalog_match.get("album"),
            }

        if not state["session_active"]:
            state["session_active"] = True
            state["session_start"] = datetime.now(timezone.utc).isoformat()
            state["session_tracks"] = 1
            state["_last_scored_track_count"] = 0
            state["now_playing"] = current
            add_event(state, "session_start", {
                "track": current["track"],
                "artist": current["artist"],
                "album": current["album"],
                "is_own_music": current.get("is_own_music", False),
            })

        elif track_key != prev_key:
            state["session_tracks"] = state.get("session_tracks", 0) + 1
            state["last_track"] = state.get("now_playing")
            state["now_playing"] = current
            add_event(state, "track_change", {
                "track": current["track"],
                "artist": current["artist"],
                "album": current["album"],
                "is_own_music": current.get("is_own_music", False),
                "session_track_number": state["session_tracks"],
            })

        else:
            state["now_playing"] = current

    else:
        if state["session_active"]:
            last_track_time = current.get("timestamp", 0)
            if last_track_time and (now - last_track_time) > IDLE_TIMEOUT:
                state["session_active"] = False
                add_event(state, "session_end", {
                    "duration_tracks": state.get("session_tracks", 0),
                    "last_track": current["track"],
                    "last_artist": current["artist"],
                })
                # Save ending context for continuation detection
                state["_prev_session_end_artist"] = current["artist"]
                state["_prev_session_end_album"] = current.get("album", "")
                state["now_playing"] = None
                state["last_track"] = current

    return state


# ─── Daemon Loop ─────────────────────────────────────────────────────────────

_running = True


def handle_signal(signum, frame):
    global _running
    log.info(f"Received signal {signum}, shutting down...")
    _running = False


def run_daemon(api_key: str, username: str):
    """Main polling loop."""
    global _running
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    level_name = ENGAGEMENT_NAMES.get(ENGAGEMENT_LEVEL, "chill")
    log.info(f"Red Thread started — polling {username} every {POLL_INTERVAL}s (engagement: {level_name})")
    if _load_genius_token():
        log.info("Layer 2 (Curiosity) enabled — Genius API token found")
    else:
        log.info("Layer 2 (Curiosity) disabled — no Genius API token. Set GENIUS_API_TOKEN to enable.")

    state = load_state()
    history = load_history()
    resonance = load_resonance()
    backoff = 0
    last_history_save = time.time()

    while _running:
        prev_session = state.get("session_active", False)
        prev_track = None
        if state.get("now_playing"):
            prev_track = f"{state['now_playing']['artist']} - {state['now_playing']['track']}"

        try:
            state = poll(api_key, username, state)
        except Exception as e:
            backoff = min(backoff + 1, 5)
            delay = 2 ** backoff
            log.error(f"Poll error: {e} — backing off {delay}s")
            time.sleep(delay)
            continue

        backoff = 0

        # Record new tracks in history
        if state.get("now_playing"):
            curr_track = f"{state['now_playing']['artist']} - {state['now_playing']['track']}"
            if curr_track != prev_track:
                np = state["now_playing"]
                record_track(history, np["track"], np["artist"], np["album"])

        # Score session on end or periodically during long sessions
        if not state.get("session_active") and prev_session:
            # Session just ended
            record_session_tracks(resonance, state)
            save_resonance(resonance)

            novelty = score_session(state, history)
            state["last_session_novelty"] = novelty
            log.info(f"Session ended — novelty {novelty['score']}/100: {novelty['summary']} (action: {novelty['action']})")

            # Layer 2: Curiosity processing on session end
            process_curiosity(state, history)

            save_history(history)
            last_history_save = time.time()

        elif state.get("session_active") and state.get("session_tracks", 0) % 5 == 0:
            # Only score/process once per track count milestone
            last_scored_at = state.get("_last_scored_track_count", 0)
            current_count = state.get("session_tracks", 0)
            if current_count != last_scored_at:
                state["_last_scored_track_count"] = current_count
                novelty = score_session(state, history)
                state["current_novelty"] = novelty

                # Layer 2: Mid-session curiosity for interesting sessions
                if novelty.get("score", 0) >= ENGAGEMENT_THRESHOLDS.get(ENGAGEMENT_LEVEL, 51):
                    process_curiosity(state, history)

        save_state(state)

        if time.time() - last_history_save > 300:
            save_history(history)
            last_history_save = time.time()

        time.sleep(POLL_INTERVAL)

    log.info("Daemon stopped cleanly")
    save_resonance(resonance)
    save_history(history)
    save_state(state)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Red Thread — Give your AI agent a sense for the music you live with",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python red_thread.py --test       Test your Last.fm connection
  python red_thread.py --once       See what's playing right now
  python red_thread.py              Run as background daemon
  python red_thread.py --status     Check session state

Your agent reads data/listening_state.json to know what's playing.
        """,
    )
    parser.add_argument("--once", action="store_true", help="Single poll, print result, exit")
    parser.add_argument("--status", action="store_true", help="Print current listening state")
    parser.add_argument("--test", action="store_true", help="Test Last.fm API connection")
    parser.add_argument("--config", action="store_true", help="Print current configuration")
    args = parser.parse_args()

    if args.config:
        level_name = ENGAGEMENT_NAMES.get(ENGAGEMENT_LEVEL, "chill")
        threshold = ENGAGEMENT_THRESHOLDS.get(ENGAGEMENT_LEVEL, 51)
        has_genius = bool(_load_genius_token())
        catalog_path = os.environ.get("CATALOG_FILE", str(REDTHREAD_DIR / "catalog.json"))

        print("Red Thread — Configuration")
        print()
        print("  Core:")
        print(f"    Data directory:     {REDTHREAD_DIR}")
        print(f"    Poll interval:      {POLL_INTERVAL}s")
        print(f"    Session mode:       {SESSION_MODE}")
        print(f"    Session gap:        {SESSION_GAP}s ({SESSION_GAP // 60} min)")
        print(f"    Idle timeout:       {IDLE_TIMEOUT}s ({IDLE_TIMEOUT // 60} min)")
        print(f"    History window:     {HISTORY_WINDOW_DAYS} days")
        print()
        print("  Engagement:")
        print(f"    Level:              {ENGAGEMENT_LEVEL} ({level_name})")
        print(f"    Talk threshold:     {threshold}+ novelty score")
        print(f"    Levels: 1=quiet (76+), 2=chill (51+), 3=tuned_in (21+), 4=dj (any)")
        print()
        print("  Layer 2 — Curiosity:")
        print(f"    Genius API:         {'enabled' if has_genius else 'disabled (set GENIUS_API_TOKEN)'}")
        print(f"    Lyrics lookups:     {'active' if has_genius else 'off'}")
        print()
        print("  Optional:")
        print(f"    Catalog file:       {catalog_path} ({'found' if Path(catalog_path).exists() else 'not found'})")
        return

    if args.status:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                state = json.load(f)
            level_name = ENGAGEMENT_NAMES.get(ENGAGEMENT_LEVEL, "chill")

            if state.get("session_active") and state.get("now_playing"):
                np = state["now_playing"]
                own = " (own music!)" if np.get("is_own_music") else ""
                print(f"Now playing: {np['track']} — {np['artist']}{own}")
                print(f"  Album: {np['album']}")
                print(f"  Session: {state['session_tracks']} tracks since {state['session_start']}")
                nov = state.get("current_novelty")
                if nov:
                    print(f"  Novelty: {nov['score']}/100 — {nov['summary']}")
                    print(f"  Action: {nov.get('action', '?')} (engagement: {level_name})")
                    for r in nov.get("reasons", []):
                        print(f"    - {r}")
                    # Show resonance tracks
                    res = nov.get("resonance_tracks", [])
                    if res:
                        print(f"  Resonance:")
                        for rt in res:
                            print(f"    ~ {rt['track']} — {rt['artist']} ({rt['sessions']} sessions)")
            elif state.get("session_active"):
                print("Session active but nothing playing right now")
            else:
                lt = state.get("last_track")
                if lt:
                    print(f"Not listening. Last track: {lt.get('track', '?')} — {lt.get('artist', '?')}")
                else:
                    print("Not listening. No recent tracks.")
                nov = state.get("last_session_novelty")
                if nov:
                    print(f"  Last session: {nov['score']}/100 — {nov['summary']} (action: {nov.get('action', '?')})")

            # Show curiosity lookups if any
            lookups = state.get("curiosity_lookups", [])
            if lookups:
                print(f"\n  Curiosity ({len(lookups)} lookups):")
                for lu in lookups:
                    print(f"    ? {lu['track']} — {lu['artist']} ({lu['reason']}) -> {lu.get('genius_url', 'n/a')}")

            print(f"\nEngagement: {level_name} | Last poll: {state.get('last_poll', 'never')}")
        else:
            print("No state file yet. Run the daemon first.")
        return

    creds = load_credentials()

    if args.test:
        print(f"Testing Last.fm API for user: {creds['username']}...")
        try:
            tracks = fetch_recent_tracks(creds["api_key"], creds["username"], limit=3)
            print(f"Connected! Found {len(tracks)} recent tracks:")
            for t in tracks:
                parsed = parse_track(t)
                status = "> NOW" if parsed["now_playing"] else "  "
                print(f"  {status} {parsed['track']} — {parsed['artist']} ({parsed['album']})")
            print("\nReady to run. Start the daemon with: python red_thread.py")
        except Exception as e:
            print(f"Failed: {e}")
            print("\nCheck your API key and username.")
        return

    if args.once:
        state = load_state()
        state = poll(creds["api_key"], creds["username"], state)
        save_state(state)
        if state.get("now_playing"):
            np = state["now_playing"]
            own = " *" if np.get("is_own_music") else ""
            print(f"> {np['track']} — {np['artist']} [{np['album']}]{own}")
        else:
            print("Nothing playing right now")
        return

    run_daemon(creds["api_key"], creds["username"])


if __name__ == "__main__":
    main()
