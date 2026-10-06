"""Look up MusicBrainz genres for an artist and album or a tab-separated list.

Usage:
    python musicbrainz_genres.py "Peter Evans" "Extra"
    python musicbrainz_genres.py --file "best-180-albums 3.txt"
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
from typing import Any

import requests

MB_URL = "https://musicbrainz.org/ws/2"
DISCOGS_URL = "https://api.discogs.com"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "AlbumGenre/1.0 (https://github.com/your-name/album-genre)"
REQUEST_DELAY_SECONDS = 1.1
TOP_GENRE_COUNT = 3
RETRY_COUNT = 3
SOURCE_WEIGHTS = {"Discogs": 5, "MusicBrainz": 4, "Last.fm": 2}


def load_settings() -> None:
    """Load non-empty KEY=value settings from the local .env file."""
    settings_path = Path(__file__).with_name(".env")
    if not settings_path.exists():
        return
    for raw_line in settings_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


load_settings()


def request_json(
    url: str,
    params: dict[str, str | int],
    headers: dict[str, str] | None = None,
    delay: float = 0,
) -> dict[str, Any]:
    """Request JSON with retries for temporary API failures."""
    if delay:
        time.sleep(delay)
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    last_error: requests.RequestException | None = None
    for attempt in range(RETRY_COUNT):
        try:
            response = requests.get(url, params=params, headers=request_headers, timeout=20)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < RETRY_COUNT - 1:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as error:
            last_error = error
            if attempt < RETRY_COUNT - 1:
                time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def get_musicbrainz_genres(artist: str, album: str) -> dict[str, Any] | None:
    """Return the best matching release group and its ranked genre tags."""
    search_data = request_json(
        f"{MB_URL}/release-group/",
        {
            "query": f'artist:"{artist}" AND releasegroup:"{album}"',
            "fmt": "json",
            "limit": 5,
        },
    )
    results = search_data.get("release-groups", [])
    if not results:
        return None

    release_group = results[0]
    time.sleep(REQUEST_DELAY_SECONDS)
    detail = request_json(
        f"{MB_URL}/release-group/{release_group['id']}",
        {"inc": "genres+tags", "fmt": "json"},
        delay=REQUEST_DELAY_SECONDS,
    )
    genres = detail.get("genres", [])
    tags = detail.get("tags", [])
    return {"release_group": detail, "genres": genres, "tags": tags}


def get_discogs_genres(artist: str, album: str) -> list[str]:
    """Find album genres and styles from Discogs when a token is configured."""
    token = os.getenv("DISCOGS_TOKEN")
    if not token:
        return []
    search = request_json(
        f"{DISCOGS_URL}/database/search",
        {"artist": artist, "release_title": album, "type": "release", "per_page": 5},
        headers={"Authorization": f"Discogs token={token}"},
    )
    result = search.get("results", [])[0] if search.get("results") else None
    if not result:
        return []
    genres = result.get("genre", []) + result.get("style", [])
    return [str(genre) for genre in genres]


def get_lastfm_genres(artist: str, album: str) -> list[str]:
    """Find album tags from Last.fm when an API key is configured."""
    api_key = os.getenv("LASTFM_API_KEY")
    if not api_key:
        return []
    data = request_json(
        LASTFM_URL,
        {"method": "album.getinfo", "artist": artist, "album": album, "api_key": api_key, "format": "json"},
    )
    album_data = data.get("album")
    if not isinstance(album_data, dict):
        return []

    tags_data = album_data.get("tags")
    if not isinstance(tags_data, dict):
        return []

    tags = tags_data.get("tag", [])
    if isinstance(tags, dict):
        tags = [tags]
    if not isinstance(tags, list):
        return []
    return [tag["name"] for tag in tags if isinstance(tag, dict) and tag.get("name")]


def normalize_genre(name: str) -> str:
    """Normalize minor spelling differences before combining source votes."""
    return " ".join(name.lower().replace("_", " ").replace("-", " ").split())


def get_genres(artist: str, album: str) -> dict[str, Any]:
    """Combine weighted genre evidence from MusicBrainz, Discogs, and Last.fm."""
    scores: dict[str, dict[str, Any]] = {}
    sources: dict[str, list[str]] = {source: [] for source in SOURCE_WEIGHTS}

    musicbrainz = get_musicbrainz_genres(artist, album)
    if musicbrainz:
        for genre in musicbrainz["genres"]:
            name = genre.get("name", "")
            sources["MusicBrainz"].append(name)
            scores.setdefault(normalize_genre(name), {"name": name, "score": 0, "sources": []})
            scores[normalize_genre(name)]["score"] += SOURCE_WEIGHTS["MusicBrainz"] * max(1, genre.get("count", 0))
        for tag in musicbrainz["tags"]:
            name = tag.get("name", "")
            if name:
                sources["MusicBrainz"].append(name)
                scores.setdefault(normalize_genre(name), {"name": name, "score": 0, "sources": []})
                scores[normalize_genre(name)]["score"] += SOURCE_WEIGHTS["MusicBrainz"]

    for source, getter in (("Discogs", get_discogs_genres), ("Last.fm", get_lastfm_genres)):
        try:
            names = getter(artist, album)
        except requests.HTTPError as error:
            if source == "Last.fm" and error.response is not None and error.response.status_code == 403:
                print("  Last.fm ERROR: API key rejected; check LASTFM_API_KEY in .env")
            else:
                print(f"  {source} ERROR: {error}")
            continue
        except requests.RequestException as error:
            print(f"  {source} ERROR: {error}")
            continue
        sources[source].extend(names)
        for name in names:
            key = normalize_genre(name)
            scores.setdefault(key, {"name": name, "score": 0, "sources": []})
            scores[key]["score"] += SOURCE_WEIGHTS[source]

    ranked = sorted(scores.values(), key=lambda item: item["score"], reverse=True)
    for item in ranked:
        item["sources"] = [source for source, names in sources.items() if any(normalize_genre(name) == normalize_genre(item["name"]) for name in names)]
    return {"release_group": musicbrainz["release_group"] if musicbrainz else None, "genres": ranked, "sources": sources}


def print_result(result: dict[str, Any], artist: str, album: str) -> None:
    release_group = result["release_group"] or {}
    genres = result["genres"]
    year = (release_group.get("first-release-date") or "")[:4] or "unknown year"

    print(f"\nAlbum:  {release_group.get('title', album)}")
    print(f"Artist: {artist}")
    print(f"Year:   {year}")
    print(f"MBID:   {release_group.get('id', 'unknown')}\n")

    if not genres:
        print("Genres: No genre tags found.")
        return

    print("Genres:")
    for genre in genres[:TOP_GENRE_COUNT]:
        source_text = ", ".join(genre["sources"])
        print(f"  {genre['name']} ({genre['score']} points; {source_text})")
    print(f"\nSuggested genre: {genres[0]['name']}")


def process_file(input_path: Path, output_path: Path, resume: bool = False) -> int:
    """Copy a tab-separated album list and append its top three genres."""
    lines = input_path.read_text(encoding="utf-8").splitlines()
    output_lines = output_path.read_text(encoding="utf-8").splitlines() if resume and output_path.exists() else []
    total = len([line for line in lines if line.strip()])
    processed = 0

    print(f"Reading {input_path} ({total} albums)")
    for line_number, line in enumerate(lines, start=1):
        if line_number <= len(output_lines):
            print(f"[{line_number}/{total}] resumed")
            continue

        if not line.strip():
            output_lines.append(line)
            output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
            continue

        fields = line.split("\t")
        if len(fields) < 4:
            print(f"[{line_number}/{total}] skipped: expected 4 tab-separated fields")
            output_lines.append(line)
            output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
            continue

        order_number, artist, album = fields[:3]
        processed += 1
        print(f"[{processed}/{total}] {artist} - {album}")
        try:
            result = get_genres(artist, album)
        except requests.RequestException as error:
            print(f"  ERROR: {error}")
            output_lines.append(f"{line}\t")
            output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
            continue
        except (KeyError, ValueError) as error:
            print(f"  ERROR: invalid MusicBrainz response ({error})")
            output_lines.append(f"{line}\t")
            output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
            continue

        if not result["genres"]:
            genres_text = ""
            print("  no genres found")
        else:
            genres = result["genres"][:TOP_GENRE_COUNT]
            genres_text = ", ".join(genre["name"] for genre in genres)
            print(f"  genres: {genres_text or 'none found'}")
        output_lines.append(f"{line}\t{genres_text}")
        output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    print(f"Finished: wrote {processed} albums to {output_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find ranked MusicBrainz genres for an album or tab-separated list."
    )
    parser.add_argument("artist", nargs="?", help="Artist name, for example: Peter Evans")
    parser.add_argument("album", nargs="?", help="Album title, for example: Extra")
    parser.add_argument("--file", type=Path, help="Tab-separated album list to copy and enrich")
    parser.add_argument("--output", type=Path, help="Output path for --file mode")
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted batch from its existing output")
    args = parser.parse_args()

    if args.file:
        output_path = args.output or args.file.with_name(f"{args.file.stem}_with_genres{args.file.suffix}")
        try:
            return process_file(args.file, output_path, args.resume)
        except OSError as error:
            print(f"File error: {error}", file=sys.stderr)
            return 1

    if not args.artist or not args.album:
        parser.error("provide artist and album, or use --file")

    try:
        result = get_genres(args.artist, args.album)
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        print(f"MusicBrainz returned HTTP {status}.", file=sys.stderr)
        return 1
    except requests.RequestException as error:
        print(f"Request failed: {error}", file=sys.stderr)
        return 1
    except (KeyError, ValueError) as error:
        print(f"Could not read MusicBrainz response: {error}", file=sys.stderr)
        return 1

    if not result["genres"]:
        print(f'No genre data found for "{args.artist}" / "{args.album}".')
        return 0

    print_result(result, args.artist, args.album)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
