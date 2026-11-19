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


def path_value(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_")


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


def format_sample_rate(sample_rate: int | None) -> str:
    """Format sample rate as a human-readable string with appropriate units."""
    if sample_rate is None:
        return ""
    if sample_rate >= 1000:
        return f"[{sample_rate / 1000:.1f}kHz]"
    return f"[{sample_rate}Hz]"


def cue_for_audio(path: Path) -> Path | None:
    if path.suffix.casefold() == ".flac":
        flac_count = sum(1 for sibling in path.parent.glob("*") if sibling.is_file() and sibling.suffix.casefold() == ".flac")
        if flac_count > 1:
            return None
    for cue in path.parent.glob("*.cue"):
        try:
            cue_text = cue.read_text(encoding="utf-8-sig")
        except UnicodeError:
            cue_text = cue.read_text(encoding="cp1252")
        if re.search(r"(?im)^\s*FILE\s+\"?" + re.escape(path.name) + r"\"?(?:\s|$)", cue_text):
            return cue
    return None


def tags_from_cue(cue: Path) -> dict[str, str]:
    try:
        cue_text = cue.read_text(encoding="utf-8-sig")
    except UnicodeError:
        cue_text = cue.read_text(encoding="cp1252")
    tags: dict[str, str] = {}
    for line in cue_text.splitlines():
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
        return [source]
    paths = []
    for directory, subdirectories, filenames in os.walk(source):
        subdirectories[:] = sorted(name for name in subdirectories if name.casefold() != "#recycle")
        paths.extend(
            Path(directory) / filename
            for filename in filenames
            if Path(filename).suffix.casefold() in extensions
        )
    return sorted(paths)


JPEG_SUFFIXES = {".jpg", ".jpeg"}
COVER_STEMS = {"folder", "front", "cover"}
JPEG_WARNING_BYTES = 1024 * 1024


def extra_patterns(value: Any) -> list[str]:
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, list):
        raise ValueError("config 'extra' must be a pattern string or list of patterns")
    patterns = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("config 'extra' patterns must be strings")
        patterns.extend(pattern.strip() for pattern in item.split(",") if pattern.strip())
    return patterns


def matching_extras(path: Path, source: Path, patterns: list[str]) -> list[Path]:
    if not patterns:
        return []
    root = (source if source.is_dir() else path.parent).resolve()
    current = path.parent.resolve()
    while current != root:
        matches = sorted({candidate for pattern in patterns for candidate in current.glob(pattern) if candidate.is_file()})
        if matches:
            return matches
        if current.parent == current:
            return []
        current = current.parent
    if root.parent == root:
        return []
    return sorted({candidate for pattern in patterns for candidate in root.glob(pattern) if candidate.is_file()})


def copy_extras(
    path: Path,
    source: Path,
    target_dir: Path,
    patterns: list[str],
    dry_run: bool,
    copied_targets: set[Path],
    move: bool,
    warned_dirs: set[Path] | None = None,
) -> None:
    if warned_dirs is None:
        warned_dirs = set()
    extras = matching_extras(path, source, patterns)
    jpgs = [extra for extra in extras if extra.suffix.casefold() in JPEG_SUFFIXES]
    for extra in extras:
        name = extra.name
        if extra.suffix.casefold() in JPEG_SUFFIXES:
            if len(jpgs) == 1 and extra.stem.casefold() not in COVER_STEMS:
                name = "folder.jpg"
        target = target_dir / name
        resolved_target = target.resolve()
        if resolved_target in copied_targets:
            continue
        if extra.resolve() == resolved_target:
            copied_targets.add(resolved_target)
            continue
        action = "move" if move else "copy"
        print(f"{'Would ' + action if dry_run else action.capitalize()} extra: {extra} -> {target}")
        if not dry_run:
            if target.exists():
                print(f"WARNING: extra already exists, skipping: {target}", file=sys.stderr)
                copied_targets.add(resolved_target)
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(extra), str(target))
            else:
                shutil.copy2(extra, target)
        copied_targets.add(resolved_target)


def copy_files_from_paths(
    source: Path,
    destination: Path,
    path_template: str,
    copy_template: str,
    dry_run: bool,
    extensions: list[str],
    extras: list[str] | None = None,
    move: bool = False,
) -> tuple[int, set[Path], set[Path]]:
    """Copy files, inferring metadata from folder paths instead of tags.

    Returns:
        (count, fully_processed_dirs, skipped_dirs) where:
        - count: number of audio files copied
        - fully_processed_dirs: set of source directories with all files successfully moved
        - skipped_dirs: set of source directories skipped due to high sample rate
    """
    files = audio_files(source, extensions)
    copied = 0
    fully_processed_dirs: set[Path] = set()
    skipped_dirs: set[Path] = set()
    warned_dirs: set[Path] = set()
    copied_extras: set[Path] = set()

    # Group files by their source directory
    dirs_to_files: dict[Path, list[Path]] = {}
    for path in files:
        if path.suffix.casefold() not in extensions:
            continue
        dir_key = path.parent.resolve()
        if dir_key not in dirs_to_files:
            dirs_to_files[dir_key] = []
        dirs_to_files[dir_key].append(path)

    # Track target directories for extras processing
    dir_to_target_dirs: dict[Path, set[Path]] = {}

    # Process each directory
    for dir_key in sorted(dirs_to_files.keys()):
        dir_files = dirs_to_files[dir_key]

        # Check sample rate of first file in directory - skip entire directory if >48kHz
        if dir_files:
            first_rate = sample_rate_for(dir_files[0])
            if first_rate and first_rate > 48000:
                rate_str = format_sample_rate(first_rate)
                print(f"WARNING: High sample rate {rate_str} ({first_rate:,} Hz > 48,000 Hz) in {dir_key}", file=sys.stderr)
                print(f"Skipping directory: {dir_key}", file=sys.stderr)
                skipped_dirs.add(dir_key)
                continue

        # Process all audio files in this directory
        dir_success = True
        dir_to_target_dirs[dir_key] = set()

        for path in sorted(dir_files):
            # Infer tags from the folder path
            relative_path = path.relative_to(source) if not source.is_file() else Path(path.name)
            try:
                tags = infer(relative_path, path_template)
            except ValueError as exc:
                print(f"SKIP: {exc}", file=sys.stderr)
                dir_success = False
                continue

            relative = evaluate(copy_template, tags).strip(" /\\")
            if not relative:
                print(f"SKIP (empty format): {path}")
                dir_success = False
                continue

            relative = "/".join(INVALID.sub("_", part).rstrip(" .") for part in relative.replace("\\", "/").split("/"))
            target = destination / relative
            if not target.name.casefold().endswith(path.suffix.casefold()):
                target = target.with_name(target.name + path.suffix)

            # Log with sample rate
            sample_rate = sample_rate_for(path)
            rate_str = format_sample_rate(sample_rate)
            action = "move" if move else "copy"
            print(f"{'Would ' + action if dry_run else action.capitalize()} {rate_str}: {path} -> {target}")
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    print(f"WARNING: destination already exists, skipping: {target}", file=sys.stderr)
                    # Track target directory even if file exists (for extras)
                    dir_to_target_dirs[dir_key].add(target.parent.resolve())
                    continue
                if move:
                    shutil.move(str(path), str(target))
                else:
                    shutil.copy2(path, target)
            copied += 1
            # Track target directory for extras
            dir_to_target_dirs[dir_key].add(target.parent.resolve())

        # Move extras for this directory after all audio files are moved
        if dir_success and extras:
            # Use the first file in directory to find related extras
            first_file = dir_files[0]
            for target_dir in sorted(dir_to_target_dirs[dir_key]):
                copy_extras(first_file, source, target_dir, extras, dry_run, copied_extras, move, warned_dirs)
            fully_processed_dirs.add(dir_key)
        elif dir_success:
            fully_processed_dirs.add(dir_key)

    return copied, fully_processed_dirs, skipped_dirs


def copy_files(
    source: Path,
    destination: Path,
    template: str,
    dry_run: bool,
    extensions: list[str],
    extras: list[str] | None = None,
    move: bool = False,
) -> tuple[int, set[Path], set[Path]]:
    """Copy files using tags from audio files.

    Returns:
        (count, fully_processed_dirs, skipped_dirs) where:
        - count: number of audio files copied
        - fully_processed_dirs: set of source directories with all files successfully moved
        - skipped_dirs: set of source directories skipped due to high sample rate
    """
    source_directory = None
    if move and source.is_dir():
        resolved_source = source.resolve()
        source_directory = resolved_source
        if resolved_source == Path(resolved_source.anchor).resolve():
            raise ValueError(f"refusing to move and delete a drive or share root: {source}")
        try:
            destination.resolve().relative_to(resolved_source)
        except ValueError:
            pass
        else:
            raise ValueError(f"refusing to move into the source directory: {destination}")

    files = audio_files(source, extensions)
    first_audio = next((path for path in files if path.suffix.casefold() in extensions), None)
    first_sample_rate = sample_rate_for(first_audio) if first_audio is not None else None
    high_sample_rate = first_sample_rate is not None and first_sample_rate > 48000
    if high_sample_rate:
        print(f"WARNING: first audio file {first_audio} has sample rate {first_sample_rate:,} Hz, above 48,000 Hz.")
    if high_sample_rate:
        try:
            answer = input(f"{'Move' if move else 'Copy'} files anyway? [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().casefold() not in {"y", "yes"}:
            action = "move" if move else "copy"
            print(f"{action.capitalize()} cancelled; no files were {action}d.")
            return 0, set(), set()

    copied = 0
    audio_paths = [path for path in files if path.suffix.casefold() in extensions]
    handled_audio = 0
    incomplete_move = False
    copied_extras: set[Path] = set()
    warned_dirs: set[Path] = set()
    processed_album_dirs: set[Path] = set()
    skipped_album_dirs: set[Path] = set()
    fully_processed_source_dirs: set[Path] = set()
    skipped_source_dirs: set[Path] = set()

    # Group files by source directory
    source_dirs_to_files: dict[Path, list[Path]] = {}
    for path in audio_paths:
        dir_key = path.parent.resolve()
        if dir_key not in source_dirs_to_files:
            source_dirs_to_files[dir_key] = []
        source_dirs_to_files[dir_key].append(path)

    def skip_existing_album(target_dir: Path) -> bool:
        resolved_dir = target_dir.resolve()
        if resolved_dir in skipped_album_dirs:
            return True
        if resolved_dir in processed_album_dirs:
            return False
        processed_album_dirs.add(resolved_dir)
        if target_dir.is_dir():
            print(f"WARNING: destination album already exists, skipping: {target_dir}")
            skipped_album_dirs.add(resolved_dir)
            return True
        return False

    def target_overlaps_source(target_dir: Path) -> bool:
        if source_directory is None:
            return False
        try:
            target_dir.resolve().relative_to(source_directory)
        except ValueError:
            return False
        return True

    # Process each source directory
    for src_dir in sorted(source_dirs_to_files.keys()):
        dir_files = source_dirs_to_files[src_dir]
        dir_handled_audio = 0
        dir_incomplete = False

        # Check sample rate of first file in directory
        if dir_files:
            first_rate = sample_rate_for(dir_files[0])
            if first_rate and first_rate > 48000:
                rate_str = format_sample_rate(first_rate)
                print(f"WARNING: High sample rate {rate_str} ({first_rate:,} Hz > 48,000 Hz) in {src_dir}", file=sys.stderr)
                print(f"Skipping directory: {src_dir}", file=sys.stderr)
                skipped_source_dirs.add(src_dir)
                continue

            # Check if destination for this directory already exists (based on first file's tags)
            first_file = dir_files[0]
            first_tags = tags_for(first_file)
            first_target_path = evaluate(template, first_tags).strip(" /\\")
            if first_target_path:
                first_target_path = "/".join(INVALID.sub("_", part).rstrip(" .") for part in first_target_path.replace("\\", "/").split("/"))
                first_target_dir = (destination / first_target_path).parent  # Get the album folder
                if first_target_dir.exists() and first_target_dir.is_dir():
                    print(f"WARNING: destination album already exists, skipping: {first_target_dir}", file=sys.stderr)
                    # Mark directory as fully processed since destination already has the files
                    fully_processed_source_dirs.add(src_dir)
                    continue

        # Process files in this directory
        target_dirs_for_extras: set[Path] = set()
        for path in sorted(dir_files):
            if path.suffix.casefold() == ".cue":
                continue
            if path.suffix.casefold() not in extensions:
                continue

            tags = tags_for(path)
            sample_rate = sample_rate_for(path)
            rate_str = format_sample_rate(sample_rate)
            cue = cue_for_audio(path)
            if cue:
                for key, value in tags_from_cue(cue).items():
                    tags.setdefault(key, value)
                folder_template = template.rpartition("/")[0]
                relative_folder = evaluate(folder_template, tags).strip(" /\\") if folder_template else ""
                if not relative_folder:
                    relative_folder = path.parent.name
                relative_folder = "/".join(INVALID.sub("_", part).rstrip(" .") for part in relative_folder.replace("\\", "/").split("/"))
                target_dir = destination / relative_folder
                if skip_existing_album(target_dir):
                    if move and not target_overlaps_source(target_dir):
                        dir_handled_audio += 1
                    else:
                        dir_incomplete = True
                    continue
                audio_target = target_dir / path.name
                cue_target = target_dir / cue.name
                print(f"{'Would copy' if dry_run else 'Copy'} {rate_str}: {path} -> {audio_target}")
                print(f"{'Would copy' if dry_run else 'Copy'} CUE: {cue} -> {cue_target}")
                if not dry_run:
                    if audio_target.exists() or cue_target.exists():
                        print(f"WARNING: destination already exists, skipping: {audio_target if audio_target.exists() else cue_target}", file=sys.stderr)
                        dir_handled_audio += 1
                        continue
                    target_dir.mkdir(parents=True, exist_ok=True)
                    if move:
                        shutil.move(str(path), str(audio_target))
                        shutil.move(str(cue), str(cue_target))
                    else:
                        shutil.copy2(path, audio_target)
                        shutil.copy2(cue, cue_target)
                target_dirs_for_extras.add(target_dir)
                copied += 1
                dir_handled_audio += 1
                continue

            relative = evaluate(template, tags).strip(" /\\")
            if not relative:
                print(f"SKIP (empty format): {path}")
                dir_incomplete = True
                continue
            relative = "/".join(INVALID.sub("_", part).rstrip(" .") for part in relative.replace("\\", "/").split("/"))
            target = destination / relative
            # Path.suffix treats the final words after a dot as an extension too:
            # "0201-Intro Vol. 2" has suffix ". 2". Always append the audio suffix.
            if not target.name.casefold().endswith(path.suffix.casefold()):
                target = target.with_name(target.name + path.suffix)
            if skip_existing_album(target.parent):
                if move and not target_overlaps_source(target.parent):
                    dir_handled_audio += 1
                else:
                    dir_incomplete = True
                continue
            if target.resolve() == path.resolve():
                print(f"SKIP (same path): {path}")
                dir_incomplete = True
                continue
            action = "move" if move else "copy"
            print(f"{'Would ' + action if dry_run else action.capitalize()} {rate_str}: {path} -> {target}")
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    print(f"WARNING: destination already exists, skipping: {target}", file=sys.stderr)
                    target_dirs_for_extras.add(target.parent)
                    dir_handled_audio += 1
                    continue
                if move:
                    shutil.move(str(path), str(target))
                else:
                    shutil.copy2(path, target)
            target_dirs_for_extras.add(target.parent)
            copied += 1
            dir_handled_audio += 1

        # Move extras after all audio files in directory have been processed
        if not dir_incomplete and dir_files:
            first_file = dir_files[0]
            for target_dir in sorted(target_dirs_for_extras):
                copy_extras(first_file, source, target_dir, extras or [], dry_run, copied_extras, move, warned_dirs)

        # Mark directory as fully processed if all files were handled successfully
        if dir_handled_audio == len(dir_files) and not dir_incomplete:
            fully_processed_source_dirs.add(src_dir)
        else:
            incomplete_move = True

    # Directory deletion is handled by main() function to ensure proper per-directory handling

    return copied, fully_processed_source_dirs, skipped_source_dirs


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


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("foobar_title_tools.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    cp = sub.add_parser("copy", help="copy or move supported audio files into title-formatted folders")
    cp.add_argument("source", type=Path, nargs="?");
    cp.add_argument("destination", type=Path, nargs="?")
    cp.add_argument("lossy_destination", type=Path, nargs="?", help="optional folder for lossy formats (MP3)")
    cp.add_argument("--format", dest="template"); cp.add_argument("--dry-run", action="store_true")
    cp_mode = cp.add_mutually_exclusive_group()
    cp_mode.add_argument("--move", dest="explicit_move", action="store_true", help="move files and remove the source folder after a complete move (default)")
    cp_mode.add_argument("--copy", action="store_true", help="copy files instead of moving them")
    tag = sub.add_parser("tag", help="copy or move by file tags, or infer tags from paths")
    tag.add_argument("source", type=Path, nargs="?", help="audio file or directory (optional, uses config default if omitted)")
    tag.add_argument("destination", type=Path, nargs="?", help="copy files into folders formatted from their tags (optional)")
    tag.add_argument("lossy_destination", type=Path, nargs="?", help="optional folder for lossy formats (MP3)")
    tag.add_argument("--format", dest="template"); tag.add_argument("--write", action="store_true")
    tag.add_argument("--dry-run", action="store_true")
    tag_mode = tag.add_mutually_exclusive_group()
    tag_mode.add_argument("--move", dest="explicit_move", action="store_true", help="move files and remove the source folder after a complete move (default)")
    tag_mode.add_argument("--copy", action="store_true", help="copy files instead of moving them")
    args = parser.parse_args()
    args.move = not args.copy
    if args.command == "tag" and args.explicit_move and args.destination is None:
        parser.error("tag --move requires a destination")
    try:
        config = load_config(args.config)
        extras = extra_patterns(config.get("extra", "*.jpg"))
        action = "move" if not args.copy else "copy"

        if args.command == "copy":
            source_folder = config.get("source_folder", "")
            dest_folder = config.get("destination_folder", "")
            source = args.source or (Path(source_folder) if source_folder else None)
            destination = args.destination or (Path(dest_folder) if dest_folder else None)
            if not source or not destination or not source.exists():
                parser.error("source and destination are required and must exist (or set in config: source_folder, destination_folder)")

            audio_exts = [e.casefold() for e in config["audio_extensions"]]
            count, fully_processed, skipped = copy_files_from_paths(source, destination, args.template or config["path_format"], config["copy_format"], args.dry_run, audio_exts, extras, not args.copy)
            print(f"{'Would ' + action if args.dry_run else action.capitalize() + 'd'} {count} audio file(s).")
            if skipped:
                skipped_list = ", ".join(str(d) for d in sorted(skipped))
                print(f"Skipped directories (high sample rate): {skipped_list}")

            # Delete fully processed source directories
            if not args.copy and fully_processed:
                for dir_path in sorted(fully_processed):
                    if dir_path.exists() and dir_path.is_dir():
                        print(f"{'Would delete' if args.dry_run else 'Deleted'} source directory: {dir_path}")
                        if not args.dry_run:
                            try:
                                shutil.rmtree(str(dir_path))
                            except Exception as e:
                                print(f"WARNING: Could not delete directory {dir_path}: {e}", file=sys.stderr)

                # Try to delete parent if all subdirectories are processed/skipped
                if source.is_dir() and skipped:  # only if some dirs were skipped
                    all_subdirs = {p.resolve() for p in source.iterdir() if p.is_dir()}
                    processed_or_skipped = fully_processed | skipped
                    if all_subdirs and all_subdirs <= processed_or_skipped:  # all subdirs are accounted for
                        try:
                            remaining = list(source.iterdir())
                            if not remaining:  # truly empty
                                print(f"{'Would delete' if args.dry_run else 'Deleted'} empty parent directory: {source}")
                                if not args.dry_run:
                                    source.rmdir()
                        except Exception as e:
                            if args.dry_run:
                                print(f"NOTE: Parent directory would not be deleted (not empty): {source}")
                            else:
                                print(f"NOTE: Parent directory not empty or could not be deleted: {source}", file=sys.stderr)
        elif args.command == "tag":
            # Tag command: read tags FROM AUDIO FILES and organize them
            source_folder = config.get("source_folder", "")
            dest_folder = config.get("destination_folder", "")
            lossy_folder = config.get("lossy_folder", "")

            source = args.source or (Path(source_folder) if source_folder else None)
            destination = args.destination or (Path(dest_folder) if dest_folder else None)
            lossy_destination = args.lossy_destination or (Path(lossy_folder) if lossy_folder else None)

            if not source or not destination:
                parser.error("source and destination are required (or set in config: source_folder, destination_folder)")

            if not source.exists():
                parser.error(f"source does not exist: {source}")

            # Sample rate check is now done per-directory in copy_files
            audio_exts = [e.casefold() for e in config["audio_extensions"]]
            files = audio_files(source, audio_exts)

            # Separate MP3s to lossy folder if specified
            if lossy_destination and lossy_destination.as_posix().strip():
                lossless_exts = [e for e in audio_exts if e != ".mp3"]
                mp3_files = [p for p in files if p.suffix.casefold() == ".mp3"]
                lossless_files = [p for p in files if p.suffix.casefold() in lossless_exts]

                # Track MP3 directories for deletion and extras
                mp3_dirs_to_files: dict[Path, list[Path]] = {}
                mp3_fully_processed: set[Path] = set()
                mp3_skipped_existing: set[Path] = set()  # Track skipped due to existing destination
                copied_extras: set[Path] = set()
                warned_dirs: set[Path] = set()

                if mp3_files:
                    print(f"Moving {len(mp3_files)} MP3 file(s) to lossy library: {lossy_destination}")

                    # Group MP3s by source directory
                    for mp3 in mp3_files:
                        dir_key = mp3.parent.resolve()
                        if dir_key not in mp3_dirs_to_files:
                            mp3_dirs_to_files[dir_key] = []
                        mp3_dirs_to_files[dir_key].append(mp3)

                    # Process each MP3 directory
                    mp3_target_dirs: dict[Path, set[Path]] = {}
                    warned_destinations: set[Path] = set()  # Track warned destinations to avoid duplicates
                    for src_dir in sorted(mp3_dirs_to_files.keys()):
                        dir_mp3_files = mp3_dirs_to_files[src_dir]
                        dir_success = True

                        # Check if destination for this directory already exists (based on first file's tags)
                        first_mp3 = dir_mp3_files[0]
                        first_tags = tags_for(first_mp3)
                        first_target_path = evaluate(config["copy_format"], first_tags).strip(" /\\")
                        if first_target_path:
                            first_target_path = "/".join(INVALID.sub("_", part).rstrip(" .") for part in first_target_path.replace("\\", "/").split("/"))
                            first_target_dir = (lossy_destination / first_target_path).parent  # Get the album folder
                            if first_target_dir.exists() and first_target_dir.is_dir():
                                # Only warn once per destination
                                if first_target_dir.resolve() not in warned_destinations:
                                    print(f"WARNING: destination album already exists, skipping: {first_target_dir}", file=sys.stderr)
                                    warned_destinations.add(first_target_dir.resolve())
                                # Track as skipped due to existing destination
                                mp3_skipped_existing.add(src_dir)
                                continue
                        mp3_target_dirs[src_dir] = set()

                        for mp3 in sorted(dir_mp3_files):
                            tags = tags_for(mp3)
                            sample_rate = sample_rate_for(mp3)
                            rate_str = format_sample_rate(sample_rate)
                            relative = evaluate(args.template or config["copy_format"], tags).strip(" /\\")
                            if not relative:
                                print(f"SKIP (empty format): {mp3}")
                                dir_success = False
                                continue
                            relative = "/".join(INVALID.sub("_", part).rstrip(" .") for part in relative.replace("\\", "/").split("/"))
                            target = lossy_destination / relative
                            if not target.name.casefold().endswith(mp3.suffix.casefold()):
                                target = target.with_name(target.name + mp3.suffix)
                            action_type = "copy" if args.copy else "move"
                            print(f"{'Would ' + action_type if args.dry_run else action_type.capitalize()} {rate_str}: {mp3} -> {target}")
                            if not args.dry_run:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                if target.exists():
                                    print(f"WARNING: destination already exists, skipping: {target}", file=sys.stderr)
                                    mp3_target_dirs[src_dir].add(target.parent)
                                    continue
                                if not args.copy:
                                    shutil.move(str(mp3), str(target))
                                else:
                                    shutil.copy2(mp3, target)
                            mp3_target_dirs[src_dir].add(target.parent)

                        # Move extras for this MP3 directory after all files are processed
                        if dir_success and dir_mp3_files:
                            first_file = dir_mp3_files[0]
                            for target_dir in sorted(mp3_target_dirs[src_dir]):
                                copy_extras(first_file, source, target_dir, extras or [], args.dry_run, copied_extras, not args.copy, warned_dirs)
                            mp3_fully_processed.add(src_dir)

                # Count MP3s that were actually processed (not skipped)
                mp3_count_moved = sum(len(files) for src_dir, files in mp3_dirs_to_files.items() if src_dir in mp3_fully_processed)

                # Only process lossless files if any exist
                if lossless_files:
                    count, fully_processed, skipped = copy_files(source, destination, args.template or config["copy_format"], args.dry_run, lossless_exts, extras, not args.copy)
                    total_moved = mp3_count_moved + count
                    print(f"{'Would ' + action if args.dry_run else action.capitalize() + 'd'} {total_moved} audio file(s) ({mp3_count_moved} MP3 + {count} lossless).")
                    if skipped:
                        skipped_list = ", ".join(str(d) for d in sorted(skipped))
                        print(f"Skipped directories (high sample rate): {skipped_list}")
                else:
                    fully_processed = set()
                    skipped = set()
                    if mp3_count_moved:
                        print(f"{'Would ' + action if args.dry_run else action.capitalize() + 'd'} {mp3_count_moved} audio file(s) ({mp3_count_moved} MP3).")
                    else:
                        print(f"No audio files to process.")

                # Combine MP3 and lossless fully processed directories (also include skipped ones for deletion)
                all_fully_processed = (mp3_fully_processed | fully_processed | mp3_skipped_existing)

                # Delete fully processed source directories (both MP3 and lossless)
                if not args.copy and all_fully_processed:
                    # First, determine which parent directories will be deleted
                    dirs_to_delete_later = set()
                    for dir_path in all_fully_processed:
                        parent = dir_path.parent.resolve()
                        if parent != source.resolve() and parent != Path(parent.anchor).resolve():
                            # Check if this parent will be deleted
                            parent_subdirs = {p.resolve() for p in parent.iterdir() if p.is_dir()} if parent.exists() else set()
                            if parent_subdirs and parent_subdirs <= all_fully_processed:
                                dirs_to_delete_later.add(parent)

                    # Only show deletion messages for directories whose parents won't be deleted
                    for dir_path in sorted(all_fully_processed):
                        if dir_path.exists() and dir_path.is_dir():
                            # Only show if parent is not being deleted
                            if dir_path.parent.resolve() not in dirs_to_delete_later:
                                print(f"{'Would delete' if args.dry_run else 'Deleted'} source directory: {dir_path}")
                                if not args.dry_run:
                                    try:
                                        shutil.rmtree(str(dir_path))
                                    except Exception as e:
                                        print(f"WARNING: Could not delete directory {dir_path}: {e}", file=sys.stderr)

                    # Try to delete parent directories of deleted dirs (recursive)
                    # In dry-run, we simulate checking if parents would be empty
                    dirs_to_check = set(all_fully_processed)
                    checked_parents = set()
                    while dirs_to_check:
                        new_dirs_to_check = set()
                        for dir_path in dirs_to_check:
                            parent = dir_path.parent.resolve()
                            if parent in checked_parents or parent == source.resolve() or parent == Path(parent.anchor).resolve():
                                continue
                            checked_parents.add(parent)

                            try:
                                if parent.exists() and parent.is_dir():
                                    if args.dry_run:
                                        # In dry-run, check if all subdirs of parent are being deleted
                                        parent_subdirs = {p.resolve() for p in parent.iterdir() if p.is_dir()}
                                        if parent_subdirs and parent_subdirs <= all_fully_processed:
                                            # All subdirs would be deleted, so parent would be empty (or contain only extras)
                                            print(f"Would delete empty parent directory: {parent}")
                                            new_dirs_to_check.add(parent)
                                    else:
                                        # Actually try to delete if not dry-run
                                        try:
                                            # Try to remove as empty directory first
                                            if not any(parent.iterdir()):
                                                parent.rmdir()
                                                print(f"Deleted empty parent directory: {parent}")
                                            else:
                                                # Parent has remaining files (extras that already existed), try to remove them too
                                                shutil.rmtree(str(parent))
                                                print(f"Deleted parent directory: {parent}")
                                            new_dirs_to_check.add(parent)
                                        except Exception as inner_e:
                                            pass  # Could not delete, leave it
                            except Exception as e:
                                if not args.dry_run:
                                    pass  # Parent not empty or other error, that's fine
                        dirs_to_check = new_dirs_to_check

                    # Try to delete top-level parent if all subdirectories are processed/skipped
                    if source.is_dir() and skipped:  # only if some dirs were skipped
                        all_subdirs = {p.resolve() for p in source.iterdir() if p.is_dir()}
                        processed_or_skipped = all_fully_processed | skipped
                        if all_subdirs and all_subdirs <= processed_or_skipped:  # all subdirs are accounted for
                            try:
                                remaining = list(source.iterdir())
                                if not remaining:  # truly empty
                                    print(f"{'Would delete' if args.dry_run else 'Deleted'} empty parent directory: {source}")
                                    if not args.dry_run:
                                        source.rmdir()
                            except Exception as e:
                                if args.dry_run:
                                    print(f"NOTE: Parent directory would not be deleted (not empty): {source}")
                                else:
                                    print(f"NOTE: Parent directory not empty or could not be deleted: {source}", file=sys.stderr)
            else:
                count, fully_processed, skipped = copy_files(source, destination, args.template or config["copy_format"], args.dry_run, audio_exts, extras, not args.copy)
                print(f"{'Would ' + action if args.dry_run else action.capitalize() + 'd'} {count} audio file(s).")
                if skipped:
                    skipped_list = ", ".join(str(d) for d in sorted(skipped))
                    print(f"Skipped directories (high sample rate): {skipped_list}")

                # Delete fully processed source directories
                if not args.copy and fully_processed:
                    for dir_path in sorted(fully_processed):
                        if dir_path.exists() and dir_path.is_dir():
                            print(f"{'Would delete' if args.dry_run else 'Deleted'} source directory: {dir_path}")
                            if not args.dry_run:
                                try:
                                    shutil.rmtree(str(dir_path))
                                except Exception as e:
                                    print(f"WARNING: Could not delete directory {dir_path}: {e}", file=sys.stderr)

                    # Try to delete parent if all subdirectories are processed/skipped
                    if source.is_dir() and skipped:  # only if some dirs were skipped
                        all_subdirs = {p.resolve() for p in source.iterdir() if p.is_dir()}
                        processed_or_skipped = fully_processed | skipped
                        if all_subdirs and all_subdirs <= processed_or_skipped:  # all subdirs are accounted for
                            try:
                                remaining = list(source.iterdir())
                                if not remaining:  # truly empty
                                    print(f"{'Would delete' if args.dry_run else 'Deleted'} empty parent directory: {source}")
                                    if not args.dry_run:
                                        source.rmdir()
                            except Exception as e:
                                if args.dry_run:
                                    print(f"NOTE: Parent directory would not be deleted (not empty): {source}")
                                else:
                                    print(f"NOTE: Parent directory not empty or could not be deleted: {source}", file=sys.stderr)
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
