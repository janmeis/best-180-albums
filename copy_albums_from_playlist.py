"""Copy complete album folders named in a playlist, with optional CUE splitting."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


# Album folders are expected to look like: [year] Album title
ALBUM_DIRECTORY = re.compile(r"^\[(?P<date>[^\]]+)\]\s*(?P<title>.+?)\s*$")
# Windows does not allow these characters in file or folder names.
INVALID_WINDOWS_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def playlist_album_directories(playlist: Path) -> list[Path]:
    """Find each distinct album folder that has a track listed in the playlist."""
    albums: dict[str, Path] = {}
    for raw_line in playlist.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        # Blank lines and lines beginning with # are playlist notes, not song paths.
        if not line or line.startswith("#"):
            continue

        track = Path(line)
        album = track.parent
        if not album.name or not album.parent.name:
            continue
        # Keep only one copy of each folder, ignoring uppercase/lowercase differences.
        albums.setdefault(str(album).casefold(), album)
    return list(albums.values())


def output_name(album: Path) -> str:
    """Build a tidy destination folder name from the artist, date, and album title."""
    match = ALBUM_DIRECTORY.match(album.name)
    if match is None:
        date = "unknown"
        title = album.name
    else:
        date = match.group("date").strip()
        title = match.group("title").strip()

    name = f"{album.parent.name}-{date}-{title}"
    name = INVALID_WINDOWS_CHARACTERS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name


def cue_tracks(cue_file: Path) -> tuple[Path, list[tuple[int, str, float]]] | None:
    """Read a CUE sheet's audio filename and each track's title and start time."""
    source_name = None
    tracks: list[tuple[int, str, float]] = []
    current_number = None
    current_title = None
    current_start = None

    for line in cue_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        file_match = re.match(r'\s*FILE\s+"([^"]+)"', line, re.IGNORECASE)
        if file_match:
            source_name = file_match.group(1)
            continue
        track_match = re.match(r"\s*TRACK\s+(\d+)\s+AUDIO", line, re.IGNORECASE)
        if track_match:
            current_number = int(track_match.group(1))
            current_title = None
            current_start = None
            continue
        title_match = re.match(r'\s*TITLE\s+"(.*)"', line, re.IGNORECASE)
        if title_match and current_number is not None:
            current_title = title_match.group(1)
            continue
        index_match = re.match(r"\s*INDEX\s+01\s+(\d+):(\d+):(\d+)", line, re.IGNORECASE)
        if index_match and current_number is not None:
            minutes, seconds, frames = (int(value) for value in index_match.groups())
            # CUE sheets measure time in minutes, seconds, and 1/75-second frames.
            current_start = minutes * 60 + seconds + frames / 75
            if current_title is not None:
                tracks.append((current_number, current_title, current_start))

    if source_name is None or not tracks:
        return None
    return cue_file.parent / source_name, tracks


def safe_track_name(number: int, title: str) -> str:
    """Make a track filename that Windows will accept."""
    name = INVALID_WINDOWS_CHARACTERS.sub("_", f"{number:02d}-{title}")
    return re.sub(r"\s+", " ", name).strip().rstrip(".") + ".flac"


def split_cue_album(album: Path, target: Path, dry_run: bool) -> set[str] | None:
    """If the album is one large audio file, split it into separate FLAC tracks."""
    cue_files = sorted(album.glob("*.cue"), key=lambda path: path.name.casefold())
    if not cue_files:
        return None
    parsed = cue_tracks(cue_files[0])
    if parsed is None:
        print(f"ERROR: could not parse cue file: {cue_files[0]}", file=sys.stderr)
        return set()

    source, tracks = parsed
    if not source.is_file():
        print(f"MISSING: cue source audio: {source}", file=sys.stderr)
        return set()

    # A track ends exactly where the next track starts; the final one runs to the end.
    for index, (number, title, start) in enumerate(tracks):
        output_name = safe_track_name(number, title)
        output_file = target / output_name
        end = tracks[index + 1][2] if index + 1 < len(tracks) else None
        if output_file.exists():
            continue
        print(f"- {output_name}")
        if dry_run:
            continue
        target.mkdir(parents=True, exist_ok=True)
        # ffmpeg does the audio cutting and saves each piece as its own FLAC file.
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-i",
            str(source),
            "-ss",
            str(start),
        ]
        if end is not None:
            command.extend(["-t", str(end - start)])
        command.extend(
            [
                "-map",
                "0:a:0",
                "-c:a",
                "flac",
                "-metadata",
                f"title={title}",
                "-y",
                str(output_file),
            ]
        )
        subprocess.run(command, check=True)

    # Once the new tracks exist, remove the old cue sheet and single-file audio copy.
    if not dry_run:
        for stale_file in target.iterdir() if target.is_dir() else []:
            if stale_file.name.casefold() in {cue.name.casefold() for cue in cue_files}:
                stale_file.unlink()
            elif stale_file.name.casefold() == source.name.casefold():
                stale_file.unlink()
    return {cue.name.casefold() for cue in cue_files} | {source.name.casefold()}


def copy_album_contents(album: Path, target: Path, dry_run: bool) -> None:
    """Copy an album's files and subfolders, keeping their original folder layout."""
    converted_names = split_cue_album(album, target, dry_run)
    for source_path in album.rglob("*"):
        relative_path = source_path.relative_to(album)
        destination_path = target / relative_path
        if source_path.is_dir():
            if not dry_run:
                destination_path.mkdir(parents=True, exist_ok=True)
            continue
        if not source_path.is_file():
            continue
        if converted_names is not None and source_path.name.casefold() in converted_names:
            continue
        if destination_path.exists():
            continue
        print(f"- {source_path.name}")
        if not dry_run:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)


def copy_albums(
    playlist: Path,
    destination: Path,
    dry_run: bool,
    limit: int | None,
    single_album: Path | None,
) -> int:
    """Choose albums, prepare destination folders, and copy each album's contents."""
    albums = [single_album] if single_album is not None else playlist_album_directories(playlist)
    if limit is not None:
        albums = albums[:limit]
    expected_names = {output_name(album).casefold() for album in albums}
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)

    # In playlist mode, remove destination album folders no longer in the playlist.
    if single_album is None:
        for existing_folder in destination.iterdir() if destination.is_dir() else []:
            if existing_folder.is_dir() and existing_folder.name.casefold() not in expected_names:
                print(f"{'Would remove' if dry_run else 'Removing'}: {existing_folder}")
                if not dry_run:
                    shutil.rmtree(existing_folder)

    copied = 0
    destinations: dict[str, Path] = {}

    for album in albums:
        target = destination / output_name(album)
        existing_source = destinations.get(str(target).casefold())
        if existing_source is not None and existing_source != album:
            print(
                f"ERROR: output name collision:\n  {existing_source}\n  {album}\n  -> {target}",
                file=sys.stderr,
            )
            continue
        destinations[str(target).casefold()] = album

        if not album.is_dir():
            print(f"MISSING: {album}", file=sys.stderr)
            continue

        print(f"{'Would copy' if dry_run else 'Copying'}: {album} -> {target}")
        copy_album_contents(album, target, dry_run)
        copied += 1

    print(f"{'Would copy' if dry_run else 'Copied'} {copied} album folder(s).")
    return 0 if copied == len(destinations) else 1


def main() -> int:
    """Read command-line options and start the copy job."""
    reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure_stdout):
        reconfigure_stdout(errors="replace")
    reconfigure_stderr = getattr(sys.stderr, "reconfigure", None)
    if callable(reconfigure_stderr):
        reconfigure_stderr(errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    # These options let you choose the playlist, destination, or a smaller trial run.
    parser.add_argument(
        "--playlist",
        type=Path,
        default=Path("best-180-albums.m3u8"),
        help="M3U8 playlist containing album track paths.",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(r"C:\Users\janme\Downloads\_MECHEN  M3"),
        help="Folder receiving the copied album folders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List planned copies without copying anything.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process only the first N albums.",
    )
    parser.add_argument(
        "--album",
        type=Path,
        help="Process one album folder directly instead of reading the playlist.",
    )
    args = parser.parse_args()

    if not args.playlist.is_file():
        parser.error(f"playlist not found: {args.playlist}")

    return copy_albums(args.playlist, args.destination, args.dry_run, args.limit, args.album)


if __name__ == "__main__":
    raise SystemExit(main())
