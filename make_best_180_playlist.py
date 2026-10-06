"""Build a playlist from best-180-albums.txt and the music collection on M:."""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import unicodedata
from pathlib import Path


def safe_console_text(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "replace").decode("ascii")


AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".alac",
    ".cue",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
ALBUM_DIRECTORY = re.compile(
    r"^\[(?P<year>\d{4})(?:\s*\(\d{4}\))?\]\s*(?P<title>.*)$"
)
ENSEMBLE_SUFFIXES = re.compile(
    r"\s+(?:big\s+)?(?:trio|quartet|quintet|sextet|septet|octet|band|orchestra|ensemble|group|collective)$",
    re.IGNORECASE,
)
ARTIST_ALIASES = {
    "yussef kamaal": "Yussef Dayes",
}
_ARTIST_DIRECTORY_CACHE: dict[str, list[Path]] = {}


def normalize(value: str) -> str:
    """Make comparisons tolerant of accents, punctuation, and case differences."""
    value = value.replace("’", "'").replace("´", "'").replace("–", "-").replace("—", "-")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character)).casefold()
    value = re.sub(r"\s+", " ", value).strip().strip(".!?").strip()
    return value


def artist_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize(value))


def top_level_artist_directories(collection: Path) -> list[Path]:
    key = str(collection.resolve()).casefold()
    if key not in _ARTIST_DIRECTORY_CACHE:
        with os.scandir(collection) as entries:
            _ARTIST_DIRECTORY_CACHE[key] = [Path(entry.path) for entry in entries if entry.is_dir()]
    return _ARTIST_DIRECTORY_CACHE[key]


def album_key(value: str) -> str:
    return re.sub(r"\b(?:and|of|or|the)\b", " ", normalize(value)).strip()


def album_matches(wanted: str, candidate: str) -> bool:
    wanted_tokens = set(re.findall(r"[a-z0-9]+", album_key(wanted)))
    candidate_tokens = set(re.findall(r"[a-z0-9]+", album_key(candidate)))
    return bool(wanted_tokens and candidate_tokens) and (
        wanted_tokens <= candidate_tokens or candidate_tokens <= wanted_tokens
    )


def parse_entry(line: str) -> tuple[str, str, str | None] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if "\t" in line:
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) >= 4 and parts[0].isdigit():
            artist, album, year = parts[1], parts[2], parts[3] or None
            return artist, album, year

    match = re.match(r"^(?P<artist>.+?)\s+–\s+(?P<album>.+?)\s*$", line)
    if match is None:
        return None

    artist = match.group("artist").strip()
    album = match.group("album").strip()
    year_match = re.search(r"\s*\((\d{4})\)\s*$", album)
    year = year_match.group(1) if year_match else None
    if year_match:
        album = album[: year_match.start()].rstrip()
    return artist, album, year


def split_artist_names(artist: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s*(?:[;,/]|&|\band\b|\+)\s*|\s+-\s+", artist, flags=re.IGNORECASE)
        if part.strip()
    ]


def artist_candidates(artist: str) -> list[str]:
    candidates = [artist]
    alias = ARTIST_ALIASES.get(normalize(artist))
    if alias:
        candidates.append(alias)
    possessive_artist = re.sub(r"'s\b", "", artist, flags=re.IGNORECASE).strip()
    if possessive_artist != artist:
        candidates.append(possessive_artist)
    simplified_artist = ENSEMBLE_SUFFIXES.sub("", artist).strip()
    if simplified_artist != artist:
        candidates.append(simplified_artist)
    candidates.extend(split_artist_names(artist))
    if normalize(artist).startswith("schlippenbach"):
        candidates.append("Alexander von Schlippenbach")
    candidates.extend(
        artist.replace(separator, replacement).strip()
        for separator in (" / ", "/", ", ", " and ")
        for replacement in (" & ", ", ")
    )
    for separator in ("/", " & ", ", ", " and "):
        candidates.extend(part.strip() for part in artist.split(separator))
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def find_artist_directories(collection: Path, artist: str) -> list[Path]:
    requested_parts = {artist_key(part) for part in split_artist_names(artist)}
    if len(requested_parts) > 1:
        exact_combined = []
        for candidate in artist_candidates(artist):
            candidate_parts = {artist_key(part) for part in split_artist_names(candidate)}
            if candidate_parts == requested_parts:
                directory = collection / candidate
                if directory.is_dir():
                    exact_combined.append(directory)
        if exact_combined:
            return list(dict.fromkeys(exact_combined))

        combined_directories = []
        for directory in top_level_artist_directories(collection):
            directory_parts = {artist_key(part) for part in split_artist_names(directory.name)}
            if requested_parts <= directory_parts:
                combined_directories.append(directory)
        if combined_directories:
            return sorted(
                combined_directories,
                key=lambda directory: len(
                    {artist_key(part) for part in split_artist_names(directory.name)} - requested_parts
                ),
            )

    wanted_artist = {normalize(candidate) for candidate in artist_candidates(artist)}
    requested_artist = artist_key(artist)
    artist_directories = []
    for candidate in artist_candidates(artist):
        directory = collection / candidate
        if directory.is_dir() and normalize(directory.name) in wanted_artist:
            artist_directories.append(directory)

    # Search targeted prefixes even when an exact base artist folder exists,
    # so collaboration folders such as Bill Frisell & Thomas Morgan are found.
    prefixes = {
        " ".join(normalize(candidate).split()[:2])
        for candidate in artist_candidates(artist)
        if len(normalize(candidate).split()) >= 2
    }
    artist_directories.extend(
        directory
        for pattern in (f"{prefix}*" for prefix in prefixes)
        for directory in collection.glob(pattern)
        if directory.is_dir()
        and (
            normalize(directory.name) in wanted_artist
            or (
                len(artist_key(directory.name)) >= 5
                and artist_key(directory.name) in requested_artist
            )
        )
    )
    if not artist_directories:
        word_fragments = {
            word
            for candidate in artist_candidates(artist)
            for word in re.findall(r"[\wÀ-ÿ]+", candidate)
            if len(word) >= 4
        }
        artist_directories.extend(
            directory
            for word in word_fragments
            for directory in collection.glob(f"*{word}*")
            if directory.is_dir()
            and (
                normalize(directory.name) in wanted_artist
                or (
                    len(artist_key(directory.name)) >= 5
                    and artist_key(directory.name) in requested_artist
                )
            )
        )
    if not artist_directories:
        prefixes = {candidate[:1] for candidate in artist_candidates(artist) if candidate}
        fuzzy_directories = []
        for prefix in prefixes:
            for directory in collection.glob(f"{prefix}*"):
                if not directory.is_dir():
                    continue
                score = difflib.SequenceMatcher(
                    None,
                    artist_key(directory.name),
                    requested_artist,
                ).ratio()
                if score >= 0.8:
                    fuzzy_directories.append((score, directory))
        artist_directories = [
            directory for _, directory in sorted(fuzzy_directories, key=lambda item: item[0], reverse=True)
        ]
    return list(dict.fromkeys(artist_directories))


def find_album(collection: Path, artist: str, album: str, year: str | None) -> Path | None:
    wanted_album = normalize(album)
    wanted_album_key = album_key(album)
    artist_directories = find_artist_directories(collection, artist)
    exact_matches = []
    token_matches = []
    fuzzy_matches = []

    for artist_directory in artist_directories:
        for album_directory in artist_directory.iterdir():
            if not album_directory.is_dir():
                continue
            match = ALBUM_DIRECTORY.match(album_directory.name)
            if match is None:
                continue
            candidate_album = normalize(match.group("title"))
            year_matches = year is not None and match.group("year") == year
            score = max(
                difflib.SequenceMatcher(None, wanted_album, candidate_album).ratio(),
                difflib.SequenceMatcher(None, wanted_album_key, album_key(candidate_album)).ratio(),
            )
            if candidate_album == wanted_album:
                exact_matches.append((year_matches, album_directory))
            elif album_matches(wanted_album, candidate_album):
                token_matches.append((year_matches, score, album_directory))
            else:
                fuzzy_matches.append((year_matches, score, album_directory))

    if exact_matches:
        return max(exact_matches, key=lambda item: item[0])[1]
    if token_matches:
        return max(token_matches, key=lambda item: (item[0], item[1]))[2]

    if fuzzy_matches:
        _, score, album_directory = max(fuzzy_matches, key=lambda item: (item[0], item[1]))
        if score >= 0.8:
            return album_directory
    return None


def available_albums(artist_directories: list[Path]) -> list[str]:
    albums = []
    for artist_directory in artist_directories:
        albums.extend(
            format_album_name(album_directory.name)
            for album_directory in artist_directory.iterdir()
            if album_directory.is_dir()
        )
    return sorted(set(albums), key=str.casefold)


def album_directories(artist_directories: list[Path]) -> list[Path]:
    return [
        album_directory
        for artist_directory in artist_directories
        for album_directory in artist_directory.iterdir()
        if album_directory.is_dir()
    ]


def format_album_name(directory_name: str) -> str:
    match = ALBUM_DIRECTORY.match(directory_name)
    if match is None:
        return directory_name
    return f"{match.group('title')} ({match.group('year')})"


def tracks_in(album_directory: Path) -> list[Path]:
    files = [path for path in album_directory.rglob("*") if path.is_file()]
    cue_files = [path for path in files if path.suffix.casefold() == ".cue"]
    if cue_files:
        return sorted(cue_files, key=lambda path: str(path).casefold())
    return sorted(
        (path for path in files if path.suffix.casefold() in AUDIO_EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", type=Path, default=Path("M:/"))
    parser.add_argument("--list", dest="source_list", type=Path, default=Path("best-180-albums 3.txt"))
    parser.add_argument("--playlist", type=Path, default=Path("best-180-albums.m3u8"))
    parser.add_argument("--missing", type=Path, default=Path("missing.txt"))
    parser.add_argument("--limit", type=int, help="Process only the first N list entries.")
    args = parser.parse_args()

    playlist_entries: list[str] = ["#EXTM3U"]
    missing_sections: list[tuple[list[str], list[str] | None]] = []
    missing_groups: dict[tuple[str, ...], list[str]] = {}
    listed_artist_directories: set[str] = set()
    playlist_paths: set[str] = set()
    lines = args.source_list.read_text(encoding="utf-8-sig").splitlines()
    if args.limit is not None:
        lines = lines[: args.limit]

    for order_number, line in enumerate(lines, start=1):
        parsed = parse_entry(line)
        if parsed is None:
            if line.strip():
                missing_sections.append(([f"{order_number}\t{line.strip()}\t(unrecognized list entry)"], None))
            continue

        artist, album, year = parsed
        artist_directories = find_artist_directories(args.collection, artist)
        album_directory = find_album(args.collection, artist, album, year)
        tracks = tracks_in(album_directory) if album_directory else []
        print(f"Processing: {order_number}: {safe_console_text(line.strip())}")
        entry_date = year or (album_directory.name.split(']')[0][1:] if album_directory else "")
        if not tracks:
            substitution = None
            if artist_directories and album_directory is None:
                candidates = album_directories(artist_directories)
                if len(candidates) == 1:
                    substitution = candidates[0]
                    tracks = tracks_in(substitution)
            reason = (
                f"album not found; using only album found: {format_album_name(substitution.name)}"
                if substitution is not None and tracks
                else "album not found"
                if artist_directories and album_directory is None
                else "artist folder not found"
                if not artist_directories
                else "no audio tracks found"
            )
            entry = f"{order_number}\t{artist}\t{album}\t{entry_date}\t({reason})"
            if artist_directories and album_directory is None and substitution is None:
                group_key = tuple(str(directory) for directory in artist_directories)
                section = missing_groups.get(group_key)
                if section is None:
                    section = []
                    missing_groups[group_key] = section
                    new_artist_directories = [
                        directory
                        for directory in artist_directories
                        if str(directory) not in listed_artist_directories
                    ]
                    listed_artist_directories.update(str(directory) for directory in artist_directories)
                    missing_sections.append((section, available_albums(new_artist_directories)))
                section.append(entry)
            else:
                missing_sections.append(([entry], None))
            for track in tracks:
                track_name = str(track)
                if track_name.casefold() not in playlist_paths:
                    playlist_paths.add(track_name.casefold())
                    playlist_entries.append(track_name)
            continue

        for track in tracks:
            track_name = str(track)
            if track_name.casefold() not in playlist_paths:
                playlist_paths.add(track_name.casefold())
                playlist_entries.append(track_name)

    args.playlist.write_text("\n".join(playlist_entries) + "\n", encoding="utf-8")
    rendered_albums: set[str] = set()
    missing = []
    for entries, albums in missing_sections:
        missing.extend(entries)
        for album in albums or []:
            if album.casefold() not in rendered_albums:
                rendered_albums.add(album.casefold())
                missing.append(f"- {album}")
    args.missing.write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
    print(f"Wrote {len(playlist_entries)} entries to {args.playlist}")
    print(f"Wrote {sum(len(entries) for entries, _ in missing_sections)} missing entries to {args.missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())