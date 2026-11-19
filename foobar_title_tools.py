"""Copy audio files with foobar2000-style paths and infer tags from paths.

Requires Mutagen (``python -m pip install mutagen``). Formatting templates live in
foobar_title_tools.json beside this script; the default config is created on first run.
The supported title-format subset is fields, optional ``[...]`` sections, quoted
literal text, ``$if``, ``$greater``, ``$num``, ``$if2``, ``$left``, and ``$meta``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from mutagen import File as MutagenFile
except ImportError as exc:
    raise RuntimeError("Mutagen is required: python -m pip install mutagen") from exc


DEFAULT_CONFIG = {
    "copy_format": "%album artist%/['['%date%']' ]%album%/$if($greater(%totaldiscs%,1), %discnumber%)%tracknumber%-%title%",
    "path_format": "%album artist% - %date% - %album%/%tracknumber%. %title%",
    "audio_extensions": [".ape", ".flac", ".mp3", ".m4a", ".wav", ".wv", ".aiff", ".ogg", ".opus"],
    "extra": "*.jpg",
}
FIELD_ALIASES = {"album artist": ("album artist", "albumartist", "album_artist"),
                 "tracknumber": ("tracknumber", "track", "track number"),
                 "discnumber": ("discnumber", "disc", "disc number"),
                 "totaltracks": ("totaltracks", "tracktotal", "total tracks"),
                 "totaldiscs": ("totaldiscs", "disctotal", "total discs")}
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
HIGH_SAMPLE_RATE = 48000
JPEG_SUFFIXES = {".jpg", ".jpeg"}
COVER_STEMS = {"folder", "front", "cover"}


def path_value(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_")


def sanitize(text: str) -> str:
    """Make a formatted path safe for the filesystem, component by component."""
    return "/".join(INVALID.sub("_", part).rstrip(" .") for part in text.strip(" /\\").replace("\\", "/").split("/"))


def verb(dry_run: bool, move: bool, past: bool = False) -> str:
    if dry_run:
        return "Would move" if move else "Would copy"
    return ("Moved" if move else "Copied") if past else ("Move" if move else "Copy")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        return path.read_text(encoding="cp1252")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Created configuration: {path}")
    config = dict(DEFAULT_CONFIG)
    config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return config


def split_args(text: str) -> list[str]:
    result, start, depth, quoted = [], 0, 0, False
    i = 0
    while i < len(text):
        char = text[i]
        if char == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(": depth += 1
            elif char == ")": depth -= 1
            elif char == "," and depth == 0:
                result.append(text[start:i].strip()); start = i + 1
        i += 1
    result.append(text[start:].strip())
    return result


def field_value(tags: dict[str, str], name: str) -> str:
    key = name.casefold()
    candidates = FIELD_ALIASES.get(key, (key,))
    for candidate in candidates:
        if candidate.casefold() in tags:
            value = str(tags[candidate.casefold()])
            if key == "tracknumber":
                match = re.fullmatch(r"\s*(\d+)(?:\s*/\s*(\d+))?\s*", value)
                if match:
                    value = match.group(1).zfill(2)
            if key in {"artist", "album artist"}:
                value = re.sub(r"\s*;\s*", ", ", value)
            return value
    if key == "album artist":
        if "artist" in tags:
            return str(tags["artist"])
        return field_value(tags, "composer") or field_value(tags, "performer")
    if key == "artist":
        return field_value(tags, "album artist") or field_value(tags, "composer") or field_value(tags, "performer")
    if key == "album":
        return field_value(tags, "venue")
    return ""


def evaluate(expr: str, tags: dict[str, str]) -> str:
    out, i = [], 0
    while i < len(expr):
        if expr[i] == "'":
            if i + 1 < len(expr) and expr[i + 1] == "'":
                out.append("'"); i += 2; continue
            end = expr.find("'", i + 1)
            if end < 0: raise ValueError("unclosed literal quote")
            out.append(expr[i + 1:end]); i = end + 1; continue
        if expr[i] == "%":
            end = expr.find("%", i + 1)
            if end < 0: raise ValueError("unclosed field name")
            out.append(path_value(field_value(tags, expr[i + 1:end]))); i = end + 1; continue
        if expr[i] == "$":
            match = re.match(r"\$([a-zA-Z_][\w]*)\(", expr[i:])
            if not match: raise ValueError(f"unsupported syntax near {expr[i:]!r}")
            name = match.group(1).casefold(); start = i + match.end(); depth = 1; quoted = False; j = start
            while j < len(expr) and depth:
                if expr[j] == "'": quoted = not quoted
                elif not quoted and expr[j] == "(": depth += 1
                elif not quoted and expr[j] == ")": depth -= 1
                j += 1
            if depth: raise ValueError(f"unclosed ${name}()")
            args = split_args(expr[start:j - 1])
            vals = [evaluate(arg, tags) for arg in args]
            if name == "if": value = vals[1] if vals and vals[0] else (vals[2] if len(vals) > 2 else "")
            elif name == "if2": value = vals[0] if vals and vals[0] else (vals[1] if len(vals) > 1 else "")
            elif name == "greater": value = "1" if len(vals) > 1 and number(vals[0]) > number(vals[1]) else ""
            elif name == "left": value = vals[0][:int(vals[1])] if len(vals) > 1 else ""
            elif name == "num": value = vals[0].zfill(int(vals[1])) if len(vals) > 1 and vals[0] else (vals[0] if vals else "")
            elif name == "meta": value = field_value(tags, vals[0]) if vals else ""
            else: raise ValueError(f"unsupported function ${name}()")
            out.append(path_value(value)); i = j; continue
        if expr[i] == "[":
            end = find_optional_end(expr, i)
            if end < 0: raise ValueError("unclosed optional section")
            body = expr[i + 1:end]
            # Foobar omits optional sections whose fields are all missing.
            fields = re.findall(r"%([^%]+)%", body)
            if any(field_value(tags, field) for field in fields): out.append(evaluate(body, tags))
            i = end + 1; continue
        if expr[i] == "]": raise ValueError("unmatched ]")
        out.append(expr[i]); i += 1
    return "".join(out)


def find_optional_end(expr: str, start: int) -> int:
    depth, quoted = 1, False
    for i in range(start + 1, len(expr)):
        if expr[i] == "'": quoted = not quoted
        elif not quoted and expr[i] == "[": depth += 1
        elif not quoted and expr[i] == "]":
            depth -= 1
            if not depth: return i
    return -1


def number(value: str) -> float:
    match = re.match(r"\s*[-+]?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else 0


def tags_for(path: Path) -> dict[str, str]:
    audio = MutagenFile(path, easy=True)
    tags = {}
    for key, values in (audio or {}).items():
        if not values:
            continue
        normalized_key = str(key).casefold()
        tag_values = [values] if isinstance(values, str) else values
        if normalized_key in {"artist", "albumartist", "album_artist", "album artist"}:
            tags[normalized_key] = ", ".join(str(value) for value in tag_values)
        else:
            tags[normalized_key] = str(tag_values[0])
    tags.setdefault("filename", path.stem)
    tags.setdefault("title", tags["filename"])
    return tags


def sample_rate_for(path: Path) -> int | None:
    audio = MutagenFile(path)
    return getattr(getattr(audio, "info", None), "sample_rate", None)


def high_sample_rate(path: Path) -> int:
    """Return the file's sample rate if it is above the supported limit, else 0."""
    rate = sample_rate_for(path)
    return rate if rate and rate > HIGH_SAMPLE_RATE else 0


def format_sample_rate(sample_rate: int | None) -> str:
    """Format sample rate as a human-readable string with appropriate units."""
    if sample_rate is None:
        return ""
    return f"[{sample_rate / 1000:.1f}kHz]" if sample_rate >= 1000 else f"[{sample_rate}Hz]"


def cue_for_audio(path: Path) -> Path | None:
    if path.suffix.casefold() == ".flac" and sum(1 for s in path.parent.glob("*") if s.is_file() and s.suffix.casefold() == ".flac") > 1:
        return None
    pattern = re.compile(r"(?im)^\s*FILE\s+\"?" + re.escape(path.name) + r"\"?(?:\s|$)")
    return next((cue for cue in path.parent.glob("*.cue") if pattern.search(read_text(cue))), None)


def tags_from_cue(cue: Path) -> dict[str, str]:
    tags: dict[str, str] = {}
    for line in read_text(cue).splitlines():
        if re.match(r"(?i)^\s*TRACK\s+\d+", line):
            break
        match = re.match(r'(?i)^\s*(PERFORMER|TITLE)\s+(?:"([^"]*)"|(.*?))\s*$', line)
        if match:
            value = (match.group(2) if match.group(2) is not None else match.group(3)).strip()
            tags["albumartist" if match.group(1).casefold() == "performer" else "album"] = value
            continue
        match = re.match(r'(?i)^\s*REM\s+DATE\s+(?:"([^"]*)"|(\S+))\s*$', line)
        if match:
            tags["date"] = match.group(1) or match.group(2)
    return tags


def audio_files(source: Path, extensions: list[str]) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.casefold() in extensions else []
    paths = []
    for directory, subdirectories, filenames in os.walk(source):
        subdirectories[:] = sorted(name for name in subdirectories if name.casefold() != "#recycle")
        paths.extend(Path(directory) / name for name in filenames if Path(name).suffix.casefold() in extensions)
    return sorted(paths)


def extra_patterns(value: Any) -> list[str]:
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("config 'extra' must be a pattern string or a list of pattern strings")
    return [pattern.strip() for item in values for pattern in item.split(",") if pattern.strip()]


def matching_extras(path: Path, source: Path, patterns: list[str]) -> list[Path]:
    """Find extras next to the audio file, climbing towards the source root until some match."""
    root = (source if source.is_dir() else path.parent).resolve()
    current = path.parent.resolve()
    while patterns and current.parent != current:  # never look in a drive root
        matches = sorted({found for pattern in patterns for found in current.glob(pattern) if found.is_file()})
        if matches:
            return matches
        if current == root:
            break
        current = current.parent
    return []


def place(src: Path, dst: Path, dry_run: bool, move: bool, label: str = "") -> bool:
    """Log and perform one move/copy; return False if the destination already exists."""
    print(f"{verb(dry_run, move)} {label}: {src} -> {dst}")
    if dry_run:
        return True
    if dst.exists():
        print(f"WARNING: destination already exists, skipping: {dst}", file=sys.stderr)
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    (shutil.move if move else shutil.copy2)(src, dst)
    return True


def copy_extras(path: Path, source: Path, target_dir: Path, patterns: list[str], dry_run: bool, done: set[Path], move: bool) -> None:
    found = matching_extras(path, source, patterns)
    jpgs = [extra for extra in found if extra.suffix.casefold() in JPEG_SUFFIXES]
    for extra in found:
        # A lone JPEG with a non-cover name becomes the folder image.
        name = "folder.jpg" if len(jpgs) == 1 and extra in jpgs and extra.stem.casefold() not in COVER_STEMS else extra.name
        target = target_dir / name
        resolved = target.resolve()
        if resolved in done:
            continue
        done.add(resolved)
        if extra.resolve() != resolved:
            place(extra, target, dry_run, move, "extra")


def transfer_files(
    source: Path,
    destination: Path,
    template: str,
    dry_run: bool,
    extensions: list[str],
    extras: list[str],
    move: bool,
    path_template: str | None = None,
) -> tuple[int, set[Path], set[Path]]:
    """Copy or move audio files into folders built from ``template``.

    Tags are read from the files (with CUE sheet support and existing-album checks), or
    inferred from the file paths when ``path_template`` is given.

    Returns (count, fully_processed_dirs, skipped_dirs): the number of audio files placed,
    the source directories that were completely handled, and those skipped for a high sample rate.
    """
    from_tags = path_template is None
    root = source.resolve() if move and source.is_dir() else None
    if root:
        if root.parent == root:
            raise ValueError(f"refusing to move and delete a drive or share root: {source}")
        if destination.resolve().is_relative_to(root):
            raise ValueError(f"refusing to move into the source directory: {destination}")

    files = audio_files(source, extensions)
    if from_tags and files and (rate := high_sample_rate(files[0])):
        print(f"WARNING: first audio file {files[0]} has sample rate {rate:,} Hz, above {HIGH_SAMPLE_RATE:,} Hz.")
        try:
            answer = input(f"{verb(False, move)} files anyway? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().casefold() not in {"y", "yes"}:
            print(f"{verb(False, move)} cancelled; no files were {verb(False, move, True).lower()}.")
            return 0, set(), set()

    by_dir: dict[Path, list[Path]] = {}
    for path in files:
        by_dir.setdefault(path.parent.resolve(), []).append(path)

    count, done_dirs, skipped_dirs = 0, set(), set()
    placed_extras, checked_albums, existing_albums = set(), set(), set()

    def album_exists(album: Path) -> bool:
        # Only the first file of an album sees it as pre-existing; later files see the folder we created.
        album = album.resolve()
        if album not in checked_albums:
            checked_albums.add(album)
            if album.is_dir():
                print(f"WARNING: destination album already exists, skipping: {album}")
                existing_albums.add(album)
        return album in existing_albums

    for src_dir, dir_files in sorted(by_dir.items()):
        if rate := high_sample_rate(dir_files[0]):
            print(f"WARNING: High sample rate {format_sample_rate(rate)} ({rate:,} Hz > {HIGH_SAMPLE_RATE:,} Hz) in {src_dir}", file=sys.stderr)
            print(f"Skipping directory: {src_dir}", file=sys.stderr)
            skipped_dirs.add(src_dir)
            continue
        if from_tags:
            first = sanitize(evaluate(template, tags_for(dir_files[0])))
            if first and (first_album := (destination / first).parent).is_dir():
                print(f"WARNING: destination album already exists, skipping: {first_album}", file=sys.stderr)
                done_dirs.add(src_dir)  # the destination already has this album
                continue

        incomplete, target_dirs = False, set()
        for path in dir_files:
            rate_str = format_sample_rate(sample_rate_for(path))
            cue = cue_for_audio(path) if from_tags else None
            try:
                tags = tags_for(path) if from_tags else infer(path.relative_to(source) if source.is_dir() else Path(path.name), path_template)
            except ValueError as exc:
                print(f"SKIP: {exc}", file=sys.stderr)
                incomplete = True
                continue
            if cue:  # audio and CUE travel together, in a folder named from the CUE-merged tags
                for key, value in tags_from_cue(cue).items():
                    tags.setdefault(key, value)
                album = destination / sanitize(evaluate(template.rpartition("/")[0], tags) or path.parent.name)
                pairs = [(path, album / path.name, rate_str), (cue, album / cue.name, "CUE")]
            else:
                relative = sanitize(evaluate(template, tags))
                if not relative:
                    print(f"SKIP (empty format): {path}")
                    incomplete = True
                    continue
                target = destination / relative
                # Path.suffix treats the final words after a dot as an extension too:
                # "0201-Intro Vol. 2" has suffix ". 2". Always append the audio suffix.
                if not target.name.casefold().endswith(path.suffix.casefold()):
                    target = target.with_name(target.name + path.suffix)
                album, pairs = target.parent, [(path, target, rate_str)]

            if from_tags and album_exists(album):
                if not move or (root and album.resolve().is_relative_to(root)):
                    incomplete = True
                continue
            if pairs[0][1].resolve() == path.resolve():
                print(f"SKIP (same path): {path}")
                incomplete = True
                continue
            count += all(place(src, dst, dry_run, move, label) for src, dst, label in pairs)
            target_dirs.add(album.resolve())

        if not incomplete:
            for target_dir in sorted(target_dirs):
                copy_extras(dir_files[0], source, target_dir, extras, dry_run, placed_extras, move)
            done_dirs.add(src_dir)

    return count, done_dirs, skipped_dirs


def cleanup_sources(source: Path, processed: set[Path], skipped: set[Path], dry_run: bool, prune_parents: bool = False) -> None:
    """Delete fully processed source folders, plus (optionally) ancestors left with nothing unprocessed."""
    if not processed:
        return
    root = source.resolve()
    doomed, pending = set(processed), set(processed)
    while prune_parents and pending:
        parent = pending.pop().parent
        if root in parent.parents and parent not in doomed and parent.is_dir() \
                and all(sub.resolve() in doomed for sub in parent.iterdir() if sub.is_dir()):
            doomed.add(parent)
            pending.add(parent)
    for directory in sorted(doomed):
        if directory.parent in doomed or not directory.is_dir():
            continue  # an ancestor takes this one along
        print(f"{'Would delete' if dry_run else 'Deleted'} {'source' if directory in processed else 'parent'} directory: {directory}")
        if not dry_run:
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                print(f"WARNING: Could not delete directory {directory}: {exc}", file=sys.stderr)

    # The source itself goes only if some folders were skipped and everything else is gone.
    if source.is_dir() and skipped:
        subdirs = {sub.resolve() for sub in source.iterdir() if sub.is_dir()}
        if subdirs and subdirs <= processed | skipped and not any(source.iterdir()):
            print(f"{'Would delete' if dry_run else 'Deleted'} empty parent directory: {source}")
            if not dry_run:
                try:
                    source.rmdir()
                except OSError as exc:
                    print(f"NOTE: Parent directory not empty or could not be deleted: {source} ({exc})", file=sys.stderr)


def path_regex(template: str) -> tuple[re.Pattern[str], list[str]]:
    names, pieces, i = [], [], 0
    while i < len(template):
        if template[i:i + 2] == "%%":
            pieces.append("(?:[^/]*)")
            i += 2
            continue
        if template[i] == "%":
            end = template.find("%", i + 1)
            if end < 0: raise ValueError("unclosed field in path format")
            name = template[i + 1:end].casefold()
            if name in names: raise ValueError(f"repeated field %{name}% cannot be inferred unambiguously")
            names.append(name); pieces.append("(.+?)"); i = end + 1
        else:
            pieces.append(re.escape(template[i])); i += 1
    return re.compile("^" + "".join(pieces) + "$", re.IGNORECASE), names


def infer(path: Path, template: str) -> dict[str, str]:
    pattern, names = path_regex(template)
    text = path.as_posix().lstrip("/")
    # File extensions are normally omitted from foobar copy formats.
    if path.suffix and not template.casefold().endswith(path.suffix.casefold()):
        text = text[:-len(path.suffix)]
    match = pattern.match(text)
    if not match:
        # Match paths with or without a root prefix by trying suffixes.
        for i in range(len(text)):
            if text[i] == "/" and (match := pattern.match(text[i + 1:])): break
    if not match: raise ValueError(f"path does not match template: {path}")
    values = dict(zip(names, match.groups()))
    if "discnumber" in values: values["discnumber"] = re.sub(r"(?i)^CD", "", values["discnumber"])
    return values


def write_tags(path: Path, inferred: dict[str, str], dry_run: bool) -> None:
    if dry_run:
        print(f"Would tag {path}: {inferred}"); return
    audio = MutagenFile(path, easy=True)
    if audio is None: raise RuntimeError(f"unsupported audio file: {path}")
    if audio.tags is None: audio.add_tags()
    mapping = {"album artist": "albumartist", "tracknumber": "tracknumber", "discnumber": "discnumber"}
    for key, value in inferred.items(): audio[mapping.get(key, key)] = [value]
    audio.save()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("foobar_title_tools.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("copy", "copy or move supported audio files into title-formatted folders"),
                            ("tag", "copy or move by file tags, or infer tags from paths")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("source", type=Path, nargs="?", help="audio file or directory (optional, uses config default if omitted)")
        command.add_argument("destination", type=Path, nargs="?", help="destination folder (optional, uses config default if omitted)")
        command.add_argument("lossy_destination", type=Path, nargs="?", help="optional folder for lossy formats (MP3)")
        command.add_argument("--format", dest="template")
        command.add_argument("--dry-run", action="store_true")
        if name == "tag":
            command.add_argument("--write", action="store_true")
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--move", dest="explicit_move", action="store_true", help="move files and remove the source folder after a complete move (default)")
        mode.add_argument("--copy", action="store_true", help="copy files instead of moving them")
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args()
    move = not args.copy
    if args.command == "tag" and args.explicit_move and args.destination is None:
        parser.error("tag --move requires a destination")
    try:
        config = load_config(args.config)

        def folder(arg: Path | None, key: str) -> Path | None:
            return arg or (Path(config[key]) if config.get(key) else None)

        source, destination = folder(args.source, "source_folder"), folder(args.destination, "destination_folder")
        if not source or not destination:
            parser.error("source and destination are required (or set in config: source_folder, destination_folder)")
        if not source.exists():
            parser.error(f"source does not exist: {source}")

        extensions = [e.casefold() for e in config["audio_extensions"]]
        if args.command == "copy":  # infer tags from the folder names
            template, path_template = config["copy_format"], args.template or config["path_format"]
        else:  # read tags from the files
            template, path_template = args.template or config["copy_format"], None
        lossy = folder(args.lossy_destination, "lossy_folder") if args.command == "tag" else None
        jobs = [(destination, extensions)]
        if lossy:
            jobs = [(lossy, [e for e in extensions if e == ".mp3"]), (destination, [e for e in extensions if e != ".mp3"])]

        counts, processed, skipped = [], set(), set()
        for job_destination, job_extensions in jobs:
            count, done_dirs, skipped_dirs = transfer_files(source, job_destination, template, args.dry_run, job_extensions,
                                                            extra_patterns(config.get("extra", "*.jpg")), move, path_template)
            counts.append(count)
            processed |= done_dirs
            skipped |= skipped_dirs

        breakdown = f" ({counts[0]} MP3 + {counts[1]} lossless)" if lossy else ""
        print(f"{verb(args.dry_run, move, True)} {sum(counts)} audio file(s){breakdown}.")
        if skipped:
            print(f"Skipped directories (high sample rate): {', '.join(str(d) for d in sorted(skipped))}")
        if move:
            cleanup_sources(source, processed, skipped, args.dry_run, prune_parents=bool(lossy))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
