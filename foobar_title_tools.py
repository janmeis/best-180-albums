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
    try:
        from mutagen import File as MutagenFile
    except ImportError as exc:
        raise RuntimeError("Mutagen is required: python -m pip install mutagen") from exc
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
    try:
        from mutagen import File as MutagenFile
    except ImportError as exc:
        raise RuntimeError("Mutagen is required: python -m pip install mutagen") from exc
    audio = MutagenFile(path)
    return getattr(getattr(audio, "info", None), "sample_rate", None)


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
) -> None:
    for extra in matching_extras(path, source, patterns):
        target = target_dir / extra.name
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
                raise FileExistsError(f"destination already exists: {target}")
            target_dir.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(extra), str(target))
            else:
                shutil.copy2(extra, target)
        copied_targets.add(resolved_target)


def copy_files(
    source: Path,
    destination: Path,
    template: str,
    dry_run: bool,
    extensions: list[str],
    extras: list[str] | None = None,
    move: bool = False,
) -> int:
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
            return 0

    copied = 0
    audio_paths = [path for path in files if path.suffix.casefold() in extensions]
    handled_audio = 0
    incomplete_move = False
    copied_extras: set[Path] = set()
    processed_album_dirs: set[Path] = set()
    skipped_album_dirs: set[Path] = set()

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

    for path in files:
        if path.suffix.casefold() == ".cue": continue
        if path.suffix.casefold() not in extensions: continue
        tags = tags_for(path)
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
                    handled_audio += 1
                else:
                    incomplete_move = True
                continue
            audio_target = target_dir / path.name
            cue_target = target_dir / cue.name
            print(f"{'Would copy' if dry_run else 'Copy'}: {path} -> {audio_target}")
            print(f"{'Would copy' if dry_run else 'Copy'} CUE: {cue} -> {cue_target}")
            if not dry_run:
                if audio_target.exists() or cue_target.exists():
                    raise FileExistsError(f"destination already exists: {audio_target if audio_target.exists() else cue_target}")
                target_dir.mkdir(parents=True, exist_ok=True)
                if move:
                    shutil.move(str(path), str(audio_target))
                    shutil.move(str(cue), str(cue_target))
                else:
                    shutil.copy2(path, audio_target)
                    shutil.copy2(cue, cue_target)
            copy_extras(path, source, target_dir, extras or [], dry_run, copied_extras, move)
            copied += 1
            handled_audio += 1
            continue
        relative = evaluate(template, tags).strip(" /\\")
        if not relative:
            print(f"SKIP (empty format): {path}")
            incomplete_move = True
            continue
        relative = "/".join(INVALID.sub("_", part).rstrip(" .") for part in relative.replace("\\", "/").split("/"))
        target = destination / relative
        # Path.suffix treats the final words after a dot as an extension too:
        # "0201-Intro Vol. 2" has suffix ". 2". Always append the audio suffix.
        if not target.name.casefold().endswith(path.suffix.casefold()):
            target = target.with_name(target.name + path.suffix)
        if skip_existing_album(target.parent):
            if move and not target_overlaps_source(target.parent):
                handled_audio += 1
            else:
                incomplete_move = True
            continue
        if target.resolve() == path.resolve():
            print(f"SKIP (same path): {path}")
            incomplete_move = True
            continue
        action = "move" if move else "copy"
        print(f"{'Would ' + action if dry_run else action.capitalize()}: {path} -> {target}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists(): raise FileExistsError(f"destination already exists: {target}")
            if move:
                shutil.move(str(path), str(target))
            else:
                shutil.copy2(path, target)
        copy_extras(path, source, target.parent, extras or [], dry_run, copied_extras, move)
        copied += 1
        handled_audio += 1

    if move and source.is_dir():
        if audio_paths and not incomplete_move and handled_audio == len(audio_paths):
            print(f"{'Would remove' if dry_run else 'Removing'} source folder and remaining contents: {source}")
            if not dry_run:
                shutil.rmtree(source)
        elif audio_paths:
            print(f"WARNING: source folder left in place because not all audio files were moved: {source}")
        else:
            print(f"WARNING: source folder left in place because it contains no supported audio files: {source}")
    return copied


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
    try:
        from mutagen import File as MutagenFile
    except ImportError as exc:
        raise RuntimeError("Mutagen is required: python -m pip install mutagen") from exc
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
    cp.add_argument("source", type=Path); cp.add_argument("destination", type=Path)
    cp.add_argument("--format", dest="template"); cp.add_argument("--dry-run", action="store_true")
    cp.add_argument("--move", action="store_true", help="move files and remove the source folder after a complete move")
    tag = sub.add_parser("tag", help="copy or move by file tags, or infer tags from paths")
    tag.add_argument("source", type=Path, help="audio file or directory")
    tag.add_argument("destination", type=Path, nargs="?", help="copy files into folders formatted from their tags")
    tag.add_argument("--format", dest="template"); tag.add_argument("--write", action="store_true")
    tag.add_argument("--dry-run", action="store_true")
    tag.add_argument("--move", action="store_true", help="move files and remove the source folder after a complete move")
    args = parser.parse_args()
    if args.command == "tag" and args.move and args.destination is None:
        parser.error("tag --move requires a destination")
    try:
        if not args.source.exists():
            raise FileNotFoundError(
                f"source path does not exist: {args.source}. In PowerShell, use '\\.' or '/' after a quoted path ending in a backslash."
            )
        config = load_config(args.config)
        extras = extra_patterns(config.get("extra", "*.jpg"))
        action = "move" if args.move else "copy"
        if args.command == "copy":
            count = copy_files(args.source, args.destination, args.template or config["copy_format"], args.dry_run, [e.casefold() for e in config["audio_extensions"]], extras, args.move)
            print(f"{ 'Would ' + action if args.dry_run else action.capitalize() + 'd'} {count} audio file(s).")
        elif args.destination:
            count = copy_files(args.source, args.destination, args.template or config["copy_format"], args.dry_run, [e.casefold() for e in config["audio_extensions"]], extras, args.move)
            print(f"{ 'Would ' + action if args.dry_run else action.capitalize() + 'd'} {count} audio file(s).")
        else:
            template = args.template or config["path_format"]
            paths = [args.source] if args.source.is_file() else sorted(p for p in args.source.rglob("*") if p.is_file() and p.suffix.casefold() != ".cue")
            if not paths:
                print(f"No files found in source: {args.source}")
            for path in paths:
                # Include the selected root folder. It often carries artist, year,
                # and album fields, while the file below it carries track/title.
                relative_path = Path(path.name) if args.source.is_file() else Path(args.source.name) / path.relative_to(args.source)
                try: write_tags(path, infer(relative_path, template), not args.write or args.dry_run)
                except ValueError as exc: print(f"SKIP: {exc}", file=sys.stderr)
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
