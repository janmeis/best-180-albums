# foobar_title_tools Documentation

Copy audio files with foobar2000-style paths and infer tags from paths.

**Requirements:** Mutagen (`python -m pip install mutagen`)

Formatting templates live in `foobar_title_tools.json` beside this script; the default config is created on first run.

## Supported Syntax

The title-format subset supports:
- **Fields:** `%field_name%`
- **Optional sections:** `[...]`
- **Quoted literals:** `'text'`
- **Functions:** `$if()`, `$greater()`, `$num()`, `$if2()`, `$left()`, `$meta()`

---

## Commands

### `copy` - Reorganize audio files

Copy or move audio files into title-formatted folders based on metadata tags.

**Usage:**
```bash
python foobar_title_tools.py copy [source] [destination] [lossy_destination] [options]
```

**Arguments:**
- `source` — Source folder or file (optional if `source_folder` set in config)
- `destination` — Destination folder (optional if `destination_folder` set in config)
- `lossy_destination` — Folder for lossy formats like MP3 (optional if `lossy_folder` set in config)

**Options:**
- `--format <template>` — Use custom format template (default: from config `copy_format`)
- `--dry-run` — Preview changes without modifying files
- `--move` — Move files and remove source folder after complete move (default)
- `--copy` — Copy files instead of moving them
- `--config <path>` — Use custom config file (default: `foobar_title_tools.json`)

**Example:**
```bash
python foobar_title_tools.py copy "C:\Music\Downloads" "C:\Music\Library"
```

This reads metadata from each audio file and organizes them:
```
Library/
  Album Artist/
    [2024] Album Name/
      01-Track Title.flac
      02-Track Title.flac
```

**Features:**
- Skips existing album directories (prevents overwrites)
- Auto-renames single JPEG as `folder.jpg`
- Handles CUE sheets for split tracks
- Copies cover art and extras (configurable patterns)
- **High sample rate detection (per-directory)** — Skips albums >48kHz, preserves source
- **MP3 separation** — Automatically moves MP3 files to separate lossy library folder
- **Multi-disc albums** — Proper track numbering with disc prefixes (101, 102, ... 201, 202)
- **Config defaults** — Uses `source_folder`, `destination_folder`, `lossy_folder` from config if not provided
- **Image handling** — Moves images after audio files, skips existing images gracefully
- **Source cleanup** — Deletes processed directories, preserves parent if any subdirectory skipped

---

### `tag` - Manage metadata

**Two completely different modes:**

---

#### **Mode 1: Infer tags FROM FOLDER NAMES and write to files**

```bash
python foobar_title_tools.py tag [source] --write [--dry-run]
```

Reads folder structure, extracts metadata, writes to audio file tags.

**When to use:** Files have no tags, but folder names follow pattern like `Artist - Album (Year)/01 Title.mp3`

**Arguments:**
- `source` — Folder with audio files (optional, uses `source_folder` from config if omitted)

**Options:**
- `--format <template>` — Use custom path format template (default: config `path_format`)
- `--write` — Actually write tags to files (dry-run by default)
- `--dry-run` — Show what would be written without modifying

**Example:**
```bash
python foobar_title_tools.py tag "C:\Music" --write
```

---

#### **Mode 2: Read tags FROM AUDIO FILES and organize them**

```bash
python foobar_title_tools.py tag <source> <destination> [lossy_destination] [options]
```

Reads existing tags and copies/moves files to organized folder structure.

**When to use:** Files already have correct tags (from metadata editor, music player, etc.)

**Arguments:**
- `source` — Folder or file with audio files (required)
- `destination` — Destination folder for organized files (required)
- `lossy_destination` — Optional folder for lossy formats like MP3

**Options:**
- `--format <template>` — Use custom format (default: config `copy_format`)
- `--move` — Move files and remove source (default)
- `--copy` — Copy files instead
- `--dry-run` — Preview only

**Example:**
```bash
python foobar_title_tools.py tag "C:\Music\Mixed" "m:\Music" "r:\MP3s" --move
```

Result: Reads artist/album/year from each file's tags, organizes into folders

---

## Configuration (`foobar_title_tools.json`)

Default format templates and paths:

```json
{
  "copy_format": "%album artist%/['['$left(%date%,4)']' ]%album%/$if($greater(%totaldiscs%,1), %discnumber%)%tracknumber%-%title%",
  "path_format": "%album artist% - %date% - %album% (%%)/%tracknumber% - %title%",
  "audio_extensions": [".ape", ".flac", ".mp3", ".m4a", ".wav", ".wv", ".aiff", ".ogg", ".opus"],
  "extra": "*.jpg",
  "source_folder": "m:\\_X_\\_X_X_X_",
  "destination_folder": "m:\\",
  "lossy_folder": "r:\\"
}
```

**Configuration Keys:**
- `copy_format` — Template for organizing files (uses foobar2000 title format)
- `path_format` — Template for inferring tags from paths
- `audio_extensions` — List of file extensions to process
- `extra` — Glob pattern for cover art and extras to copy
- `source_folder` — Default source folder (used if not provided on command line)
- `destination_folder` — Default lossless music library folder (used if not provided)
- `lossy_folder` — Default lossy music library folder for MP3 files (used if not provided)

### Template Syntax

**Fields:**
- `%artist%` — Track artist (or album artist if unavailable)
- `%album artist%` — Album artist (or artist/composer/performer fallback)
- `%album%` — Album name (or venue if unavailable)
- `%title%` — Track title (or filename if unavailable)
- `%tracknumber%` — Track number (zero-padded: `01`, `02`, etc.)
- `%discnumber%` — Disc number
- `%date%` — Release year
- `%totaldiscs%` — Total number of discs
- `%totaltracks%` — Total number of tracks
- `%filename%` — Original filename without extension

**Optional sections** `[...]` — Included only if all fields inside have values:
```
%album artist%/['['%date%']' ]%album%
```
This outputs: `Album Artist/[2024] Album Name` or `Album Artist/Album Name` if date is missing.

**Functions:**

- `$if(condition, true_value, false_value)` — Conditional output
  ```
  $if(%discnumber%, disc %discnumber% -)
  ```

- `$if2(value1, value2, ...)` — Output first non-empty value
  ```
  $if2(%album artist%, %artist%, Unknown)
  ```

- `$greater(value1, value2)` — Returns `1` if value1 > value2 (numerically)
  ```
  $if($greater(%totaldiscs%, 1), %discnumber%-)
  ```

- `$num(value, length)` — Zero-pad number to length
  ```
  $num(%tracknumber%, 3)  → "001" instead of "1"
  ```

- `$left(value, length)` — Take first N characters
  ```
  $left(%title%, 10)
  ```

- `$meta(field)` — Get field value (alternative syntax)
  ```
  $meta(album)
  ```

---

## Field Aliases

The tool recognizes these tag name variations:

| Primary Field | Aliases |
|---|---|
| `album artist` | `albumartist`, `album_artist` |
| `tracknumber` | `track`, `track number` |
| `discnumber` | `disc`, `disc number` |
| `totaltracks` | `tracktotal`, `total tracks` |
| `totaldiscs` | `disctotal`, `total discs` |

---

## Examples

### Example 1: Organize by album metadata
```bash
python foobar_title_tools.py copy "D:\Downloads" "D:\Music" --format "%artist%/%album%/%tracknumber% %title%"
```

Result:
```
Music/
  Pink Floyd/
    The Dark Side of the Moon/
      01 Speak to Me
      02 Breathe
      ...
  Radiohead/
    Kid A/
      01 Everything in Its Right Place
      ...
```

### Example 2: Infer from organized folder structure
```bash
python foobar_title_tools.py tag "D:\Music" --write
```

Reads folder names like `Artist - Year - Album/01. Title.flac` and writes tags to the files.

### Example 3: Dry-run preview
```bash
python foobar_title_tools.py copy "D:\Music\Source" "D:\Music\Dest" --dry-run
```

Shows what would happen without modifying any files:
```
Would copy: source.flac -> dest/artist/album/01-title.flac
Would copy extra: cover.jpg -> dest/artist/album/folder.jpg
Copied 150 audio file(s).
```

### Example 4: Multi-disc albums
Template: `%artist%/$if($greater(%totaldiscs%,1), CD%discnumber%/)%tracknumber%-%title%`

Result:
```
Artist/
  CD1/
    01-Track A
    02-Track B
  CD2/
    01-Track C
    02-Track D
```

---

## Special Behaviors

### High Sample Rate Detection (Per-Directory)
The tool checks the sample rate of audio files **per source directory**:
- If any audio file in a directory has sample rate > 48,000 Hz, **that directory is skipped**
- Other directories with normal sample rates are processed normally
- **Source directories are preserved** when skipped (not deleted)
- Files already in destination are not affected

Output:
```
WARNING: High sample rate [96.0kHz] (96,000 Hz > 48,000 Hz) in m:\...\Susanne Sundfor
Skipping directory: m:\...\Susanne Sundfor
```

### Existing Destination Handling
If the destination album folder already exists:
- **A warning is printed** (not an error)
- **The source directory is skipped** without trying to move individual files
- **Source directories are still deleted** (marked as fully processed)
- This prevents duplicate attempts at previously processed albums

Output:
```
WARNING: destination album already exists, skipping: r:\Why_\[2026] Oh Golly & Yes Lord
```

### MP3 Separation
If a `lossy_destination` folder is specified:
- MP3 files are automatically moved/copied to the lossy folder
- Lossless formats (FLAC, WAV, etc.) go to the main library folder
- Multi-disc albums (CD1, CD2, etc.) are properly handled
- Uses the same naming template for both destinations

**Example:**
```bash
python foobar_title_tools.py tag "source/" "m:\Music" "r:\MP3s"
```

Result:
```
m:\Music/
  Artist/Album/
    01-Track.flac
    02-Track.flac

r:\MP3s/
  Artist/Album/
    01-Track.mp3
    02-Track.mp3
```

### Source Directory Cleanup
After moving files:
- **Individual album directories are deleted** after all files are successfully moved
- **Parent directories are deleted** if ALL subdirectories were processed
- **Parent directory is preserved** if ANY subdirectory was skipped (high sample rate)
- Remaining files (like already-existing extras) are cleaned up along with the directory

Output:
```
Would delete source directory: m:\...\Album Name
Would delete empty parent directory: m:\...\
```

### Image/Extra Files
- Cover art and extras are moved **after all audio files** in each directory are processed
- Images are searched in parent directories automatically (follows the same path as audio)
- Already-existing images are skipped with a warning (not an error)

### Multi-Disc Albums
When processing albums with multiple discs (CD1, CD2, etc.):
- Track numbers are prefixed with disc number (101, 102, ... 201, 202, etc.)
- Uses template: `$if($greater(%totaldiscs%,1), %discnumber%)%tracknumber%`
- Each disc's files are organized correctly in the destination

If a `.cue` file references an audio file, the tool:
1. Reads PERFORMER, TITLE, and DATE from the CUE
2. Merges with audio file tags
3. Uses folder structure from the cue metadata
4. Copies both audio and CUE files together

### Path Validation
Invalid filesystem characters are replaced with `_`:
- `<`, `>`, `:`, `"`, `/`, `\`, `|`, `?`, `*`, null bytes
- Trailing spaces and dots are removed per path component

---

## Tips

1. **Test first:** Always use `--dry-run` before actual moves
2. **Cover art:** Add `*.jpg` to `extra` patterns to auto-copy covers as `folder.jpg`
3. **High-res albums:** High sample rate detection is per-directory, so only high-res albums are skipped
4. **Already processed:** Use `--dry-run` to preview; existing destination albums are skipped
5. **Fallbacks:** Use `$if2()` for fields that might be missing
6. **Case handling:** Template output is case-sensitive; adjust as needed
7. **Multi-disc:** Use `$if($greater(%totaldiscs%,1), %discnumber%)` to prefix disc number to track number
8. **Sample rates:** Logged for each file (e.g., `[44.1kHz]`, `[96.0kHz]`) in output
9. **Image files:** Moved after all audio files are processed, with no warning for existing images

---

## Troubleshooting

| Issue | Solution |
|---|---|
| "Mutagen is required" | Run: `python -m pip install mutagen` |
| Directories skipped with high sample rate | This is intentional - high-res albums (>48kHz) are preserved. Check sample rate if not intended |
| "destination album already exists" warning | Album folder exists in destination from prior run - source will be cleaned up but files won't be re-copied |
| Path doesn't match template | Check if `--format` or `--path_format` template matches your path structure |
| Tags not written | Ensure you used `--write` flag in tag mode (default is dry-run) |
| Config not found | First run auto-creates `foobar_title_tools.json` in script directory |
| Parent directory not deleted | Parent is preserved if any subdirectory was skipped (high sample rate) or incompletely processed |
| Duplicate source files in destination | May occur if moving same album twice - use `--dry-run` first to verify

---

## See Also

- Mutagen documentation: https://mutagen.readthedocs.io/
- foobar2000 title format guide: https://www.foobar2000.org/titleformat
