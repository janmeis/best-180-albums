"""Build a JazzTrail Best 30 playlist from the configured page and music library."""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from make_best_180_playlist import artist_key, find_album, normalize, safe_console_text, split_artist_names, tracks_in


PAGE_HEADING = re.compile(r"^THE BEST 30 JAZZ ALBUMS OF (?P<year>\d{4})$", re.IGNORECASE)
MISSING_MARKER = "# {heading}"
MISSING_SECTION = re.compile(r"^# (?:The Best 30 Jazz Albums Of|Best Jazz) \d{4}(?: Reviews)?$", re.IGNORECASE)


class HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[tuple[int, str]] = []
        self.current_level: int | None = None
        self.current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        match = re.fullmatch(r"h([1-6])", tag.casefold())
        if match:
            self.current_level = int(match.group(1))
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_level is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        match = re.fullmatch(r"h([1-6])", tag.casefold())
        if match and self.current_level is not None and self.current_level == int(match.group(1)):
            text = re.sub(r"\s+", " ", "".join(self.current_text)).strip()
            if text:
                self.headings.append((self.current_level, text))
            self.current_level = None
            self.current_text = []


class BestOfJazzParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.heading_parts: list[str] = []
        self.reading_heading = False
        self.reading_entry_title = False
        self.in_target_section = False
        self.heading = ""
        self.year = ""
        self.item_texts: list[list[str]] = []
        self.item_has_children: list[bool] = []
        self.albums: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "h2":
            if self.in_target_section:
                self.in_target_section = False
                self.item_texts.clear()
                self.item_has_children.clear()
            self.reading_heading = True
            self.reading_entry_title = any(
                name.casefold() == "class" and value and "entry-title" in value.split()
                for name, value in attrs
            )
            self.heading_parts = []
        elif tag == "li" and self.in_target_section:
            if self.item_has_children:
                self.item_has_children[-1] = True
            self.item_texts.append([])
            self.item_has_children.append(False)

    def handle_data(self, data: str) -> None:
        if self.reading_heading:
            self.heading_parts.append(data)
        if self.in_target_section and self.item_texts:
            self.item_texts[-1].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "h2" and self.reading_heading:
            heading = re.sub(r"\s+", " ", "".join(self.heading_parts)).strip()
            match = re.fullmatch(r"Best Jazz (?P<year>\d{4})", heading, re.IGNORECASE)
            if match:
                self.heading = heading
                self.year = match.group("year")
                self.in_target_section = True
            elif self.reading_entry_title:
                album = parse_bestofjazz_item(heading)
                if album:
                    self.albums.append(album)
            self.reading_heading = False
            self.reading_entry_title = False
            self.heading_parts = []
        elif tag == "li" and self.in_target_section and self.item_texts:
            text = re.sub(r"\s+", " ", "".join(self.item_texts.pop())).strip()
            has_children = self.item_has_children.pop()
            if not has_children:
                album = parse_bestofjazz_item(text)
                if album:
                    self.albums.append(album)


def parse_bestofjazz_item(text: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"(.+?)\s+[–—]\s+(.+)", text.strip())
    if not match:
        return None
    artist = match.group(1).strip()
    album = re.sub(r"\s*\([^()]*\)\s*$", "", match.group(2)).strip()
    return (artist, album) if artist and album else None


def fetch_page(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; JazzPlaylistBuilder/1.0)"})
    with urlopen(request, timeout=30) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(encoding, errors="replace")


def parse_jazztrail_albums(html: str) -> tuple[str, str, list[tuple[str, str]]]:
    parser = HeadingParser()
    parser.feed(html)
    page_heading = ""
    year = ""
    albums: list[tuple[str, str]] = []
    active = False
    current_artist = ""

    for level, text in parser.headings:
        heading_match = PAGE_HEADING.fullmatch(text)
        if heading_match:
            if not page_heading:
                page_heading = text
                year = heading_match.group("year")
                active = True
            elif len(albums) >= 30:
                break
            continue
        if not active:
            continue
        if level == 2:
            if text.casefold() == "additional links":
                break
            current_artist = text
        elif level == 3 and current_artist:
            albums.append((current_artist, text))
            current_artist = ""
            if len(albums) >= 30:
                break

    if not year or not albums:
        raise ValueError("could not find the Best 30 Jazz Albums heading and artist/title pairs on the page")
    return page_heading, year, albums


def parse_bestofjazz_albums(html: str, url: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    parser = BestOfJazzParser()
    parser.feed(html)
    review_year_match = re.search(r"/(?P<year>\d{4})-reviews/?$", urlsplit(url).path, re.IGNORECASE)
    year = parser.year or (review_year_match.group("year") if review_year_match else "")
    heading = parser.heading or (f"Best Jazz {year} Reviews" if review_year_match else "")
    if not year or not parser.albums:
        raise ValueError("could not find a year and album entries on the Best of Jazz page")
    return heading, year, parser.albums


def parse_albums(html: str, url: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    host = (urlsplit(url).hostname or "").casefold()
    if host == "bestofjazz.org" or host.endswith(".bestofjazz.org"):
        return parse_bestofjazz_albums(html, url)
    return parse_jazztrail_albums(html)


def write_missing_reports(path: Path, reports: list[tuple[str, list[str], int, int]]) -> None:
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    preamble: list[str] = []
    sections: dict[str, str] = {}
    marker = None
    current: list[str] = []
    for line in existing.splitlines():
        if MISSING_SECTION.fullmatch(line):
            if marker is None:
                pass
            else:
                sections[marker] = "\n".join(current).rstrip()
            marker = line
            current = [line]
        elif marker is None:
            preamble.append(line)
        else:
            current.append(line)
    if marker is not None:
        sections[marker] = "\n".join(current).rstrip()

    for marker, missing, processed, total in reports:
        report = [marker, f"# Processed {processed} of {total} albums"]
        report.extend(missing or ["No missing albums in this batch."])
        sections[marker] = "\n".join(report)

    preserved = "\n".join(preamble).rstrip()
    report_sections = "\n\n".join(sections.values())
    content = (preserved + "\n\n" if preserved else "") + report_sections + ("\n" if report_sections else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def pending_album_keys(path: Path, marker: str) -> tuple[bool, set[str]]:
    if not path.exists():
        return False, set()
    in_section = False
    section_found = False
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if MISSING_SECTION.fullmatch(line):
            if in_section:
                break
            in_section = line.casefold() == marker.casefold()
            section_found = section_found or in_section
            continue
        if not in_section or not line.startswith("- "):
            continue
        match = re.fullmatch(r"(.+?)\s+\((?:album not found|no audio tracks found|not processed yet)\)", line[2:], re.IGNORECASE)
        if match:
            keys.add(normalize(match.group(1)))
    return section_found, keys


def read_playlist(path: Path, reset: bool) -> tuple[list[str], set[str]]:
    entries = ["#EXTM3U"]
    seen: set[str] = set()
    if reset or not path.exists():
        return entries, seen
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.casefold() in seen:
            continue
        entries.append(entry)
        seen.add(entry.casefold())
    return entries, seen


def configured_path(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def matching_artist(artist: str, album: str, overrides: dict[str, str]) -> str:
    exact_key = normalize(f"{artist} - {album}")
    if exact_key in overrides:
        return overrides[exact_key]

    requested_artists = {artist_key(part) for part in split_artist_names(artist)}
    for override_key, replacement in overrides.items():
        override_artist, separator, override_album = override_key.rpartition(" - ")
        if not separator or normalize(override_album) != normalize(album):
            continue
        override_artists = {artist_key(part) for part in split_artist_names(override_artist)}
        if requested_artists == override_artists:
            return replacement
    return artist


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("make_best_30_jazz_config.json"))
    parser.add_argument("--collection", type=Path, help="Override the configured music-library path.")
    parser.add_argument("--playlist", type=Path, help="Override the output playlist path.")
    parser.add_argument("--missing", type=Path, help="Override the missing-report path.")
    parser.add_argument("--limit", type=int, help="Process only the first N albums; omit to process all albums.")
    parser.add_argument("--reset", action="store_true", help="Recheck every album and rebuild each playlist from scratch.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    urls = config["urls"]
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls or not all(isinstance(url, str) and url for url in urls):
        raise ValueError("config 'urls' must be a non-empty list of page URLs")
    if args.playlist and len(urls) > 1 and "{year}" not in str(args.playlist):
        parser.error("--playlist must include {year} when processing multiple URLs")
    base = args.config.resolve().parent
    collection = args.collection or configured_path(base, config.get("collection", "M:/"))
    playlist_pattern = config.get("playlist_pattern", "best-30-jazz-albums-{year}.m3u8")
    artist_overrides = {
        normalize(str(key)): value
        for key, value in config.get("artist_overrides", {}).items()
    }

    remaining = args.limit
    reports_by_path: dict[Path, list[tuple[str, list[str], int, int]]] = {}
    processed_years: set[str] = set()
    for url in urls:
        if remaining == 0:
            break
        print(f"Fetching: {url}")
        heading, year, albums = parse_albums(fetch_page(url), url)
        if year in processed_years:
            raise ValueError(f"multiple URLs use year {year}; use one page per year")
        processed_years.add(year)
        host = (urlsplit(url).hostname or "").casefold()
        site_outputs = config.get("site_outputs", {})
        site_output = next(
            (settings for domain, settings in site_outputs.items()
             if host == domain.casefold() or host.endswith("." + domain.casefold())),
            {},
        )
        if args.playlist:
            playlist_value = Path(str(args.playlist).format(year=year))
        else:
            page_playlist_pattern = site_output.get("playlist_pattern", playlist_pattern)
            playlist_value = configured_path(base, page_playlist_pattern.format(year=year))
        missing_path = args.missing or configured_path(base, site_output.get("missing", config.get("missing", "missing.txt")))
        marker = MISSING_MARKER.format(heading=heading.title())
        section_found, pending_keys = pending_album_keys(missing_path, marker)
        if section_found and not args.reset:
            candidates = [
                (artist, album)
                for artist, album in albums
                if normalize(f"{artist} - {album}") in pending_keys
            ]
            print(f"Retrying {len(candidates)} missing or unprocessed album(s) from the report")
        else:
            candidates = albums
        if not candidates:
            print(f"No pending albums for {safe_console_text(heading)}")
            continue

        selected = candidates if remaining is None else candidates[:remaining]
        unprocessed = candidates[len(selected):]
        if remaining is not None:
            remaining -= len(selected)
        print(f"Found {len(albums)} album entries: {safe_console_text(heading)}")
        print(f"Processing {len(selected)} album(s) from {collection}")

        playlist_entries, seen_paths = read_playlist(playlist_value, args.reset)
        missing = [f"- {artist} - {album} (not processed yet)" for artist, album in unprocessed]
        for index, (artist, album) in enumerate(selected, start=1):
            print(f"Processing {index}/{len(selected)}: {safe_console_text(artist)} - {safe_console_text(album)}")
            lookup_artist = matching_artist(artist, album, artist_overrides)
            if lookup_artist != artist:
                print(f"  Artist override: {safe_console_text(lookup_artist)}")
            album_directory = find_album(collection, lookup_artist, album, year)
            tracks = tracks_in(album_directory) if album_directory else []
            if not tracks:
                reason = "album not found" if album_directory is None else "no audio tracks found"
                missing_entry = f"- {artist} - {album} ({reason})"
                missing.append(missing_entry)
                print(f"  MISSING: {safe_console_text(missing_entry)}")
                continue

            added = 0
            for track in tracks:
                entry = str(track)
                if entry.casefold() not in seen_paths:
                    seen_paths.add(entry.casefold())
                    playlist_entries.append(entry)
                    added += 1
            print(f"  FOUND: {safe_console_text(str(album_directory))} ({added} playlist entr{'y' if added == 1 else 'ies'})")

        playlist_value.parent.mkdir(parents=True, exist_ok=True)
        playlist_value.write_text("\n".join(playlist_entries) + "\n", encoding="utf-8")
        reports_by_path.setdefault(missing_path, []).append((marker, missing, len(selected), len(albums)))
        print(f"Wrote {len(playlist_entries) - 1} playlist entries to {playlist_value}")

    for missing_path, reports in reports_by_path.items():
        write_missing_reports(missing_path, reports)
        print(f"Wrote {sum(len(report[1]) for report in reports)} missing album(s) to {missing_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)