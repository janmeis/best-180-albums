# Album genre lookup

`musicbrainz_genres.py` looks up an album and combines genre evidence from:

- MusicBrainz release-group genres and tags
- Discogs release genres and styles
- Last.fm album tags

MusicBrainz is always used. Discogs and Last.fm are optional and require API credentials.

## Install

```bash
pip install -r requirements.txt
```

## Single album

```bash
python musicbrainz_genres.py "The Beatles" "Abbey Road"
```

The output shows the top three genres, their weighted points, and the sources that contributed them.

## Album list

For a tab-separated file with `order`, `artist`, `album`, and `year` columns:

```bash
python musicbrainz_genres.py --file "best-180-albums 3.txt"
```

The script creates `best-180-albums 3_with_genres.txt`, preserving the original columns and appending a comma-separated genre column. Progress is logged to the console. Interrupted runs can continue with:

```bash
python musicbrainz_genres.py --file "best-180-albums 3.txt" --resume
```

## Optional sources

PowerShell:

```powershell
$env:DISCOGS_TOKEN = "your-discogs-token"
$env:LASTFM_API_KEY = "your-lastfm-key"
python musicbrainz_genres.py "The Beatles" "Abbey Road"
```

The scoring favors album-level Discogs data, then MusicBrainz, then Last.fm:

- Discogs: 5 points per genre/style
- MusicBrainz: 4 points multiplied by its tag count
- Last.fm: 2 points per tag

Use API keys from the providers directly. Do not commit them to the project or put them in the input list.
