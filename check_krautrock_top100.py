"""Find albums from Discogs' Krautrock Top 100 missing from a music library."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from make_best_180_playlist import find_album, safe_console_text
from make_best_30_jazz_playlist import matching_artist


LIST_URL = "https://api.discogs.com/lists/795"
EXPECTED_ALBUMS = 100
ARTIST_OVERRIDES = {"et cetera - et cetera": "Wolfgang Dauner"}


def parse_items(data: dict) -> list[tuple[str, str, str]]:
    items = data.get("items")
    if not isinstance(items, list) or len(items) != EXPECTED_ALBUMS:
        raise ValueError(f"expected {EXPECTED_ALBUMS} list items, got {len(items) if isinstance(items, list) else 'none'}")
    albums = []
    for number, item in enumerate(items, start=1):
        title = item.get("display_title") if isinstance(item, dict) else None
        if not isinstance(title, str) or " - " not in title:
            raise ValueError(f"invalid display title for list item {number}")
        artist, album = title.split(" - ", 1)
        artist = re.sub(r"\s*\(\d+\)|\*", "", artist).strip()
        album = album.strip()
        if not artist or not album:
            raise ValueError(f"invalid display title for list item {number}")
        albums.append((str(number), artist, album))
    return albums


def fetch_albums() -> list[tuple[str, str, str]]:
    request = Request(LIST_URL, headers={"User-Agent": "AlbumLibraryChecker/1.0"})
    with urlopen(request, timeout=30) as response:
        return parse_items(json.load(response))


def find_missing(collections: list[Path], albums: list[tuple[str, str, str]]) -> list[str]:
    missing = []
    for number, artist, album in albums:
        titles = [album] + [part.strip() for part in album.split(" = ") if part.strip()]
        lookup_artist = matching_artist(artist, album, ARTIST_OVERRIDES)
        match = next(
            (found for collection in collections for title in titles
             if (found := find_album(collection, lookup_artist, title, None))),
            None,
        )
        if match is None:
            missing.append(f"{number}\t{artist}\t{album}\t(album not found)")
            print(f"MISSING: {safe_console_text(artist)} - {safe_console_text(album)}")
        else:
            print(f"FOUND: {safe_console_text(artist)} - {safe_console_text(album)}: {safe_console_text(str(match))}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", type=Path, action="append", help="Library root; repeat for multiple roots (default: M:/ and R:/)")
    parser.add_argument("--missing", type=Path, default=Path("krautrocktop100missing.txt"))
    args = parser.parse_args()
    try:
        collections = args.collection or [Path("M:/"), Path("R:/")]
        for collection in collections:
            if not collection.is_dir():
                raise ValueError(f"music library directory does not exist: {collection}")
        albums = fetch_albums()
        missing = find_missing(collections, albums)
        args.missing.write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
        print(f"Checked {len(albums)} albums; wrote {len(missing)} missing albums to {args.missing}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())