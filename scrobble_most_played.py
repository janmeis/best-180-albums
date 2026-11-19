"""Scrobble tracks from HiBy R3 Pro most_played.db to Last.fm."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

DEFAULT_LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("scrobble_most_played.json")
DEFAULT_STATE_PATH = Path("scrobbler_state.json")
DEFAULT_LOG_PATH = Path(__file__).with_name("scrobble_most_played.txt")
DEFAULT_DB_CANDIDATES = [
    Path(r"E:\.temp\most_played.db"),
    Path(__file__).with_name("most_played.db"),
]
MAX_SCROBBLE_BATCH_SIZE = 50
SCROBBLE_INTERVAL_SECONDS = 30


@dataclass
class TrackItem:
    order_no: int
    title: str
    artist: str
    album: str
    year: str | None
    path: str
    info_id: int
    duration_ms: int

    @property
    def key(self) -> str:
        return f"{self.path}|{self.title}|{self.info_id}"


def safe_console_text(value: str) -> str:
    """Ensure text can be printed without encoding errors on legacy consoles."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(encoding, errors="backslashreplace").decode(encoding)


def load_env_settings() -> dict[str, str]:
    """Read KEY=value pairs from .env if present."""
    env_path = Path(__file__).with_name(".env")
    settings: dict[str, str] = {}
    if not env_path.is_file():
        return settings
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            settings[key] = value
    return settings


def ensure_config_file(config_path: Path) -> dict[str, Any]:
    """Load the JSON configuration, creating a template if missing."""
    if not config_path.exists():
        env_settings = load_env_settings()
        default_api_key = env_settings.get("LASTFM_API_KEY", "")
        default_secret = env_settings.get("LASTFM_API_SECRET", "")
        default_config: dict[str, Any] = {
            "lastfm": {
                "url": DEFAULT_LASTFM_URL,
                "username": "",
                "password": "",
                "api_key": default_api_key,
                "api_secret": default_secret,
                "session_key": "",
            },
            "scrobbler": {
                "database": str(DEFAULT_DB_CANDIDATES[0]),
                "state_file": str(DEFAULT_STATE_PATH),
                "log_file": str(DEFAULT_LOG_PATH),
            },
        }
        config_path.write_text(
            json.dumps(default_config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return default_config

    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def parse_folder_artist_album(folder_name: str) -> tuple[str, str, str | None]:
    """Extract (artist, album, year) from HiBy folder name pattern Artist-Year-Album."""
    match = re.match(r"^(?P<artist>.+?)-(?P<year>\d{4})-(?P<album>.+)$", folder_name)
    if match:
        return match.group("artist").strip(), match.group("album").strip(), match.group("year")
    if "-" in folder_name:
        artist, _, album = folder_name.partition("-")
        return artist.strip(), album.strip(), None
    return folder_name.strip(), "", None


def read_tracks_from_db(db_path: Path) -> list[TrackItem]:
    """Read all tracks from most_played.db in order of their SQLite rowid."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rowid, path, name, info_id, begin_time, end_time "
        "FROM most_played "
        "ORDER BY rowid ASC"
    )
    rows = cursor.fetchall()
    conn.close()

    tracks: list[TrackItem] = []
    for rowid, raw_path, raw_name, info_id, begin_time, end_time in rows:
        path = raw_path.rstrip("\x00").strip()
        title = raw_name.rstrip("\x00").strip()
        parts = path.replace("/", "\\").split("\\")
        folder = parts[-2] if len(parts) >= 2 else ""
        artist, album, year = parse_folder_artist_album(folder)
        if not title:
            file_name = parts[-1] if parts else f"Track {rowid}"
            title = Path(file_name).stem

        duration = (end_time - begin_time) if (begin_time >= 0 and end_time > begin_time) else 0
        tracks.append(
            TrackItem(
                order_no=rowid,
                title=title,
                artist=artist or "Unknown Artist",
                album=album,
                year=year,
                path=path,
                info_id=info_id,
                duration_ms=duration,
            )
        )
    return tracks


def check_daily_limit(state: dict[str, Any]) -> tuple[bool, str]:
    """Check if a mass upload already happened today."""
    last_uploaded_at = state.get("last_uploaded_at", "")
    if not last_uploaded_at:
        return True, "No prior uploads recorded"

    try:
        last_time = datetime.strptime(last_uploaded_at, "%Y-%m-%d %H:%M:%S")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        last_date = last_time.replace(hour=0, minute=0, second=0, microsecond=0)

        if last_date >= today:
            hours_ago = (datetime.now() - last_time).total_seconds() / 3600
            return False, f"Already uploaded today ({hours_ago:.1f} hours ago). Use --force to override."
        return True, f"Last upload was {(today - last_date).days} days ago"
    except ValueError:
        return True, "Could not parse last upload time"


def load_state(state_path: Path) -> dict[str, Any]:
    """Load previously saved upload state."""
    if not state_path.is_file():
        return {
            "last_order_no": 0,
            "last_title": "",
            "session_key": "",
        }
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data.pop("uploaded_keys", None)
        return data
    except (json.JSONDecodeError, OSError):
        return {
            "last_order_no": 0,
            "last_title": "",
            "session_key": "",
        }


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Save upload state to JSON."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp_path.replace(state_path)


def reset_state(state_path: Path) -> dict[str, Any]:
    """Reset upload progress in state file to 0."""
    state = load_state(state_path)
    state["last_order_no"] = 0
    state["last_title"] = ""
    save_state(state_path, state)
    return state


def generate_api_sig(params: dict[str, str], secret: str) -> str:
    """Generate Last.fm MD5 API method signature."""
    keys = sorted(k for k in params.keys() if k not in ("format", "callback"))
    sig_string = "".join(f"{k}{params[k]}" for k in keys) + secret
    return hashlib.md5(sig_string.encode("utf-8")).hexdigest()


class LastFmClient:
    """Client for Last.fm authentication and batch scrobbling."""

    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        username: str = "",
        password: str = "",
        session_key: str = "",
    ) -> None:
        self.url = url or DEFAULT_LASTFM_URL
        self.api_key = api_key
        self.api_secret = api_secret
        self.username = username
        self.password = password
        self.session_key = session_key

    def authenticate(self) -> str:
        """Obtain session key using username and password via auth.getMobileSession."""
        if self.session_key:
            return self.session_key

        if not self.username or not self.password:
            raise ValueError(
                "Last.fm username and password must be configured in scrobble_most_played.json"
            )
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "Last.fm API key and API secret must be configured in scrobble_most_played.json or .env"
            )

        params = {
            "method": "auth.getMobileSession",
            "username": self.username,
            "password": self.password,
            "api_key": self.api_key,
        }
        params["api_sig"] = generate_api_sig(params, self.api_secret)
        params["format"] = "json"

        response = requests.post(self.url, data=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"Last.fm auth failed: [{data.get('error')}] {data.get('message')}")

        session = data.get("session", {})
        self.session_key = session.get("key", "")
        if not self.session_key:
            raise RuntimeError("Last.fm returned response without session key")
        return self.session_key

    def scrobble_batch(self, tracks_with_timestamps: list[tuple[TrackItem, int]]) -> int:
        """Upload up to 50 tracks in a single track.scrobble call."""
        if not tracks_with_timestamps:
            return 0
        if len(tracks_with_timestamps) > MAX_SCROBBLE_BATCH_SIZE:
            raise ValueError(f"Batch size exceeds maximum of {MAX_SCROBBLE_BATCH_SIZE}")

        if not self.session_key:
            self.authenticate()

        params: dict[str, str] = {
            "method": "track.scrobble",
            "api_key": self.api_key,
            "sk": self.session_key,
        }

        for idx, (track, timestamp) in enumerate(tracks_with_timestamps):
            params[f"artist[{idx}]"] = track.artist
            params[f"track[{idx}]"] = track.title
            params[f"timestamp[{idx}]"] = str(timestamp)
            if track.album:
                params[f"album[{idx}]"] = track.album
            if track.duration_ms >= 1000:
                params[f"duration[{idx}]"] = str(track.duration_ms // 1000)

        params["api_sig"] = generate_api_sig(params, self.api_secret)
        params["format"] = "json"

        response = requests.post(self.url, data=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise RuntimeError(f"Last.fm scrobble failed: [{data.get('error')}] {data.get('message')}")

        scrobbles = data.get("scrobbles")
        if scrobbles is None:
            raise RuntimeError(f"Last.fm response missing 'scrobbles' key. Full response: {data}")

        if not isinstance(scrobbles, dict):
            raise RuntimeError(f"Last.fm 'scrobbles' is not a dict: {type(scrobbles)}. Response: {data}")

        attr = scrobbles.get("@attr", {})
        accepted = attr.get("accepted")
        if accepted is None:
            raise RuntimeError(f"Last.fm response missing 'accepted' count in @attr: {attr}. Full response: {data}")

        return int(accepted)


def resolve_db_path(args_db: Path | None, config: dict[str, Any]) -> Path:
    """Determine the database path from CLI args, JSON settings, or default locations."""
    if args_db:
        return args_db
    scrobbler_section = config.get("scrobbler", config)
    config_db = str(scrobbler_section.get("database", "")).strip()
    if config_db:
        path = Path(config_db)
        if path.is_file():
            return path
    for candidate in DEFAULT_DB_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not find most_played.db. Specify path with --db or in scrobble_most_played.json"
    )


def fmt_duration(ms: int) -> str:
    """Format milliseconds into mm:ss duration string."""
    if ms <= 0:
        return "N/A"
    total_seconds = ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def write_scrobble_log(
    log_path: Path,
    tracks_with_timestamps: list[tuple[TrackItem, int]],
    reset: bool = False,
    dry_run: bool = False,
) -> None:
    """Write or append upload log with track details, local timestamps, and track duration."""
    if not tracks_with_timestamps:
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    append_mode = log_path.is_file() and not reset and log_path.stat().st_size > 0

    if append_mode and dry_run:
        return

    new_rows: list[str] = []
    for track, ts in tracks_with_timestamps:
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        year_str = track.year or ""
        dur_str = fmt_duration(track.duration_ms)
        new_rows.append(
            f"| {track.order_no} | {time_str} | {track.artist} | {track.title} | {track.album} | {year_str} | {dur_str} | `{track.path}` |"
        )

    lines: list[str] = [
        f"# HiBy R3 Pro - Scrobble Log ({len(tracks_with_timestamps)} tracks)",
        f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}" + (" [DRY-RUN]" if dry_run else ""),
        "",
        "| Order | Timestamp (Local) | Artist | Track Name | Album | Year | Duration | Path |",
        "|:-----:|:-----------------:|--------|------------|-------|:----:|:--------:|------|",
    ]
    lines.extend(new_rows)

    if append_mode:
        existing = log_path.read_text(encoding="utf-8")
        prefix = ""
        if existing:
            prefix = "\n" if existing.endswith("\n\n") else ("\n\n" if existing.endswith("\n") else "\n\n\n")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(lines) + "\n")
    else:
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scrobble_database(
    db_path: Path,
    config: dict[str, Any],
    state_path: Path,
    log_path: Path | None = None,
    interval: int = 0,
    reset: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> tuple[int, int | None, str | None]:
    """Execute the upload and return (uploaded_count, last_order_no, last_title)."""
    tracks = read_tracks_from_db(db_path)
    state = load_state(state_path)

    if reset:
        state["last_order_no"] = 0
        state["last_title"] = ""

    last_order_no = int(state.get("last_order_no", 0))

    to_upload = [
        track
        for track in tracks
        if track.order_no > last_order_no
    ]

    if not to_upload:
        previous_last_order = state.get("last_order_no")
        previous_last_title = state.get("last_title")
        return 0, previous_last_order, previous_last_title

    # Assign current date and time (or staggered by interval seconds)
    now = int(time.time())
    total_count = len(to_upload)
    tracks_with_timestamps: list[tuple[TrackItem, int]] = []
    for idx, track in enumerate(to_upload):
        ts = now if interval <= 0 else now - (total_count - 1 - idx) * interval
        tracks_with_timestamps.append((track, ts))

    lastfm_section = config.get("lastfm", config)
    env_settings = load_env_settings()

    url = str(lastfm_section.get("url", DEFAULT_LASTFM_URL)).strip() or DEFAULT_LASTFM_URL
    username = str(lastfm_section.get("username", "")).strip()
    password = str(lastfm_section.get("password", "")).strip()
    api_key = (
        str(lastfm_section.get("api_key", "")).strip()
        or env_settings.get("LASTFM_API_KEY", "").strip()
    )
    api_secret = (
        str(lastfm_section.get("api_secret", "")).strip()
        or env_settings.get("LASTFM_API_SECRET", "").strip()
    )
    session_key = (
        str(lastfm_section.get("session_key", "")).strip()
        or state.get("session_key", "").strip()
    )

    client: LastFmClient | None = None
    if not dry_run:
        client = LastFmClient(
            url=url,
            api_key=api_key,
            api_secret=api_secret,
            username=username,
            password=password,
            session_key=session_key,
        )
        session_key = client.authenticate()
        state["session_key"] = session_key

    uploaded_count = 0
    new_last_order_no = last_order_no
    new_last_title = state.get("last_title", "")

    # Upload in chunks of up to 50 tracks
    for start in range(0, total_count, MAX_SCROBBLE_BATCH_SIZE):
        batch = tracks_with_timestamps[start : start + MAX_SCROBBLE_BATCH_SIZE]
        if dry_run:
            accepted = len(batch)
        else:
            assert client is not None
            try:
                accepted = client.scrobble_batch(batch)
            except Exception as exc:
                first_no = batch[0][0].order_no
                last_no = batch[-1][0].order_no
                raise RuntimeError(
                    f"Failed to scrobble batch #{first_no}-#{last_no}: {exc}"
                ) from exc

        uploaded_count += accepted
        for track, _ in batch:
            new_last_order_no = track.order_no
            new_last_title = track.title

        if verbose or accepted < len(batch):
            first_no = batch[0][0].order_no
            last_no = batch[-1][0].order_no
            prefix = "[DRY-RUN] " if dry_run else ""
            status = f"({accepted}/{len(batch)} accepted)"
            print(
                safe_console_text(
                    f"{prefix}Scrobbled batch #{first_no}-#{last_no} {status}"
                )
            )
            if accepted < len(batch) and not dry_run:
                print(
                    safe_console_text(
                        f"WARNING: Last.fm rejected {len(batch) - accepted} track(s) in batch"
                    ),
                    file=sys.stderr,
                )

    state["last_order_no"] = new_last_order_no
    state["last_title"] = new_last_title
    state.pop("uploaded_keys", None)
    state["last_uploaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    if not dry_run:
        save_state(state_path, state)

    if log_path is not None:
        write_scrobble_log(log_path, tracks_with_timestamps, reset=reset, dry_run=dry_run)

    return uploaded_count, new_last_order_no, new_last_title


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="backslashreplace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        help="Path to most_played.db (default: from scrobble_most_played.json or E:\\.temp\\most_played.db)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to scrobbler JSON configuration file (default: scrobble_most_played.json)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="Path to state JSON file (default: from scrobble_most_played.json or scrobbler_state.json)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="Path to scrobble log text file (default: from scrobble_most_played.json or scrobble_most_played.txt)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Seconds between track timestamps leading up to current time (default: 0 = all use current date and time)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset upload progress in state file without accessing database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate scrobbling without sending data to Last.fm",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed batch progress",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force upload even if already uploaded today",
    )
    args = parser.parse_args()

    try:
        config = ensure_config_file(args.config)
        scrobbler_section = config.get("scrobbler", config)
        state_path = (
            args.state
            or Path(str(scrobbler_section.get("state_file", DEFAULT_STATE_PATH)))
        )

        if args.reset:
            reset_state(state_path)
            print(safe_console_text(f"State reset: {state_path} (last uploaded item cleared)"))
            return 0

        db_path = resolve_db_path(args.db, config)
        log_path = (
            args.log
            or Path(str(scrobbler_section.get("log_file", DEFAULT_LOG_PATH)))
        )

        state = load_state(state_path)
        if not args.force and not args.dry_run:
            can_upload, reason = check_daily_limit(state)
            if not can_upload:
                print(safe_console_text(f"Daily upload limit: {reason}"), file=sys.stderr)
                return 1
            elif reason:
                print(safe_console_text(f"OK to upload: {reason}"))

        uploaded, last_no, last_title = scrobble_database(
            db_path=db_path,
            config=config,
            state_path=state_path,
            log_path=log_path,
            interval=args.interval,
            reset=False,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )

        if last_no is not None and last_title:
            output = f"{uploaded} items uploaded\nlast uploaded: {last_no} {last_title}"
        else:
            output = f"{uploaded} items uploaded"

        print(safe_console_text(output))
        return 0

    except (
        OSError,
        ValueError,
        RuntimeError,
        requests.RequestException,
        sqlite3.Error,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
