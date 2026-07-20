#!/usr/bin/env python3
"""
YTS -> Telegram new-release notifier.

What it does
------------
1. Polls the official YTS JSON API (list_movies), sorted by date_added,
   to find movies that weren't seen on the previous run.
2. For each new movie, pulls: title, year, genres, poster image, IMDb rating.
3. Fetches the movie's page on yts.gg and extracts the Rotten Tomatoes
   Tomatometer % and Audience % (these aren't reliably exposed by the
   JSON API, so this is the one place we lightly scrape HTML).
4. Sends a Telegram message with the poster as a photo. The title is
   sent inside a Markdown code span, so tapping it in Telegram copies
   it to the clipboard instantly (no long-press menu needed).
5. Remembers which movie IDs it already notified about in seen_ids.json
   so you don't get duplicate notifications.

Run it on a schedule (cron / systemd timer) - see README.md.
"""

import argparse
import html
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# --------------------------------------------------------------------------
# Configuration (env vars, with sane defaults)
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# YTS official API base (per https://yts.gg/api)
YTS_API_BASE = os.environ.get("YTS_API_BASE", "https://movies-api.accel.li/api/v2")
YTS_SITE_BASE = os.environ.get("YTS_SITE_BASE", "https://yts.gg")

# How many of the most-recently-added movies to check each run.
# The browse-movies "featured/latest" list is basically what this mirrors.
LIST_LIMIT = int(os.environ.get("YTS_LIST_LIMIT", "20"))

# Optional filters - leave blank/"all" for no filter.
FILTER_QUALITY = os.environ.get("YTS_FILTER_QUALITY", "")      # e.g. "1080p"
FILTER_GENRE = os.environ.get("YTS_FILTER_GENRE", "")          # e.g. "Horror"
FILTER_MIN_RATING = os.environ.get("YTS_FILTER_MIN_RATING", "")  # e.g. "6"

STATE_FILE = Path(os.environ.get("YTS_STATE_FILE", "/var/lib/yts-notifier/seen_ids.json"))

# Only used in --loop mode (continuous process). Ignored for single-shot runs.
POLL_INTERVAL_SECONDS = int(os.environ.get("YTS_POLL_INTERVAL_SECONDS", "900"))  # 15 min

REQUEST_TIMEOUT = 20
USER_AGENT = "yts-telegram-notifier/1.0 (personal use; contact via Telegram bot owner)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("yts-notifier")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


# --------------------------------------------------------------------------
# State (which movie IDs we've already notified about)
# --------------------------------------------------------------------------

def load_seen_ids() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not parse %s, starting fresh", STATE_FILE)
    return set()


def save_seen_ids(seen: set) -> None:
    # Keep the file from growing forever - cap at the most recent 2000 IDs.
    trimmed = list(seen)[-2000:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(trimmed))


# --------------------------------------------------------------------------
# YTS JSON API
# --------------------------------------------------------------------------

def fetch_latest_movies() -> list:
    """Fetch the most recently added movies from the official YTS API."""
    params = {
        "limit": LIST_LIMIT,
        "sort_by": "date_added",
        "order_by": "desc",
        "with_rt_ratings": "true",
    }
    if FILTER_QUALITY and FILTER_QUALITY.lower() != "all":
        params["quality"] = FILTER_QUALITY
    if FILTER_GENRE and FILTER_GENRE.lower() != "all":
        params["genre"] = FILTER_GENRE
    if FILTER_MIN_RATING:
        params["minimum_rating"] = FILTER_MIN_RATING

    resp = SESSION.get(f"{YTS_API_BASE}/list_movies.json", params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"YTS API error: {data.get('status_message')}")
    return data.get("data", {}).get("movies", []) or []


# --------------------------------------------------------------------------
# Rotten Tomatoes scores (scraped from the movie page - not in the JSON API)
# --------------------------------------------------------------------------

# Matched against the page's *visible text* (tags stripped), not raw HTML,
# so this keeps working even if YTS changes class names / attribute order.
# On the page, the RT block renders as e.g.:
#   68% TOMATOMETER · 72 reviews
#   72% AUDIENCE · 250 ratings
RT_TOMATOMETER_RE = re.compile(r"(\d{1,3})\s*%\s*TOMATOMETER", re.IGNORECASE)
RT_AUDIENCE_RE = re.compile(r"(\d{1,3})\s*%\s*AUDIENCE", re.IGNORECASE)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _visible_text(html_source: str) -> str:
    """Cheap HTML-to-text: strip tags, collapse whitespace, unescape entities.

    Good enough here because we only need to find short number+label patterns
    in document order - a full HTML parser (BeautifulSoup/lxml) would also
    work and is more robust; swap in if you hit edge cases.
    """
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_source, flags=re.IGNORECASE | re.DOTALL)
    stripped = _TAG_RE.sub(" ", no_scripts)
    return _WS_RE.sub(" ", html.unescape(stripped))


def fetch_rt_scores(movie_url: str) -> tuple[Optional[str], Optional[str]]:
    """Scrape Tomatometer % and Audience % off a YTS movie page.

    Returns (tomatometer, audience) as strings like "68%" / "72%",
    or (None, None) if not found (e.g. movie has no RT data yet).
    """
    if not movie_url:
        return None, None
    try:
        resp = SESSION.get(movie_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Could not fetch %s for RT scores: %s", movie_url, exc)
        return None, None

    text = _visible_text(resp.text)

    tomato_match = RT_TOMATOMETER_RE.search(text)
    audience_match = RT_AUDIENCE_RE.search(text)

    tomatometer = f"{tomato_match.group(1)}%" if tomato_match else None
    audience = f"{audience_match.group(1)}%" if audience_match else None
    return tomatometer, audience


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def escape_markdown_v2(text: str) -> str:
    """Escape special chars for Telegram's MarkdownV2 parse mode."""
    specials = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{ch}" if ch in specials else ch for ch in text)


def build_caption(movie: dict, tomatometer: Optional[str], audience: Optional[str]) -> str:
    title = html.unescape(movie.get("title", "Unknown title"))
    year = movie.get("year", "")
    genres = ", ".join(movie.get("genres", []) or [])
    imdb = movie.get("rating")
    imdb_line = f"⭐ IMDb: *{imdb}/10*" if imdb else "⭐ IMDb: N/A"
    rt_line = f"🍅 Tomatometer: *{tomatometer or 'N/A'}*  |  🍿 Audience: *{audience or 'N/A'}*"
    genre_line = f"🎭 {escape_markdown_v2(genres)}" if genres else ""

    # Title is wrapped in a code span -> tap-to-copy in Telegram clients.
    title_line = f"`{escape_markdown_v2(title)}`"

    lines = [
        title_line,
        f"📅 {year}" + (f"   {genre_line}" if genre_line else ""),
        imdb_line,
        rt_line,
        "",
        f"[View on YTS]({escape_markdown_v2(movie.get('url', ''))})",
    ]
    return "\n".join(lines)


def send_telegram_photo(poster_url: str, caption: str) -> None:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": poster_url,
        "caption": caption,
        "parse_mode": "MarkdownV2",
    }
    resp = SESSION.post(api_url, data=payload, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        log.error("Telegram API error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_once(seed_only: bool = False) -> None:
    if not seed_only and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables first "
            "(see README.md)."
        )

    seen = load_seen_ids()
    movies = fetch_latest_movies()
    log.info("Fetched %d movies from YTS API", len(movies))

    if seed_only:
        # Mark everything currently listed as "already seen" without
        # sending any Telegram messages. Use this once on first setup
        # so you only get notified about movies added AFTER today.
        for movie in movies:
            seen.add(str(movie.get("id")))
        save_seen_ids(seen)
        log.info("Seeded %d movie IDs as already-seen. No notifications sent.", len(movies))
        return

    new_movies = [m for m in movies if str(m.get("id")) not in seen]
    # Notify oldest-first so the chat reads in chronological order.
    new_movies.reverse()

    if not new_movies:
        log.info("No new movies this run.")
        return

    log.info("Found %d new movie(s)", len(new_movies))

    for movie in new_movies:
        movie_id = str(movie.get("id"))
        title = movie.get("title", "Unknown")
        poster = (
            movie.get("large_cover_image")
            or movie.get("medium_cover_image")
            or movie.get("small_cover_image")
        )

        tomatometer, audience = fetch_rt_scores(movie.get("url", ""))
        caption = build_caption(movie, tomatometer, audience)

        try:
            if poster:
                send_telegram_photo(poster, caption)
            else:
                # No poster available - fall back to a plain text message.
                api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                SESSION.post(
                    api_url,
                    data={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "MarkdownV2"},
                    timeout=REQUEST_TIMEOUT,
                ).raise_for_status()
            log.info("Notified: %s (%s)", title, movie_id)
        except requests.RequestException as exc:
            log.error("Failed to notify for %s (%s): %s", title, movie_id, exc)
            continue  # don't mark as seen if the send failed - retry next run

        seen.add(movie_id)
        save_seen_ids(seen)
        time.sleep(1.5)  # be gentle with Telegram's rate limits


_shutdown_requested = False


def _handle_shutdown_signal(signum, frame):  # noqa: ARG001 - signal handler signature
    global _shutdown_requested
    log.info("Received signal %s, shutting down after current cycle...", signum)
    _shutdown_requested = True


def run_forever(seed_only_first: bool = False) -> None:
    """Run continuously, polling every POLL_INTERVAL_SECONDS.

    This is the mode you want on a web server / VPS: start it once as a
    background service (systemd, Docker, supervisord, pm2, ...) and it
    keeps itself scheduled - no external cron needed.
    """
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    log.info(
        "Starting continuous polling: every %ds, state file at %s",
        POLL_INTERVAL_SECONDS,
        STATE_FILE,
    )

    first_iteration = True
    while not _shutdown_requested:
        try:
            if first_iteration and seed_only_first:
                run_once(seed_only=True)
            else:
                run_once()
        except Exception:  # noqa: BLE001 - one bad cycle shouldn't kill the service
            log.exception("Error during poll cycle; will retry next interval")
        first_iteration = False

        # Sleep in small increments so a shutdown signal is honored promptly
        # instead of waiting out the full interval.
        slept = 0
        while slept < POLL_INTERVAL_SECONDS and not _shutdown_requested:
            time.sleep(min(5, POLL_INTERVAL_SECONDS - slept))
            slept += 5

    log.info("Stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YTS -> Telegram new-release notifier")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="First-run mode: record current listings as 'seen' without sending any messages, then exit.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously as a long-lived process, polling every YTS_POLL_INTERVAL_SECONDS "
        "(default 900s / 15min). Use this on a server instead of cron.",
    )
    args = parser.parse_args()

    if args.loop:
        # In loop mode, --seed just means "don't notify on the very first cycle".
        run_forever(seed_only_first=args.seed)
    else:
        run_once(seed_only=args.seed)
        sys.exit(0)
