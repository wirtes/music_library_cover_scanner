# Music Cover Scanner

`music_cover_scan.py` recursively scans a music library for missing artwork and can extract embedded cover art to `cover.jpg`.

## Features

- Finds album directories missing `cover.jpg`
- Extracts embedded artwork from audio tags into `cover.jpg`
- Reports directories that have neither `cover.jpg` nor embedded artwork
- Supports dry-run mode to preview actions without writing files
- Works recursively from a root music directory

## Requirements

- Python 3.9+
- `mutagen`

Install dependency:

```bash
pip install mutagen
```

## Script

```bash
python3 music_cover_scan.py --help
```

## Usage

Basic format:

```bash
python3 music_cover_scan.py <ROOT_MUSIC_DIRECTORY> [FLAGS]
```

At least one action flag is required:

- `--scan-missing-cover`
- `--extract`
- `--report-missing-artwork`

### Common Examples

1. Scan for album folders missing `cover.jpg`:

```bash
python3 music_cover_scan.py "/path/to/music" --scan-missing-cover
```

2. Preview extraction actions only (no file writes):

```bash
python3 music_cover_scan.py "/path/to/music" --extract --dry-run
```

3. Extract embedded artwork into `cover.jpg` where missing:

```bash
python3 music_cover_scan.py "/path/to/music" --extract
```

4. Report directories with neither `cover.jpg` nor embedded artwork:

```bash
python3 music_cover_scan.py "/path/to/music" --report-missing-artwork
```

5. Full scan + extract + report in one run:

```bash
python3 music_cover_scan.py "/path/to/music" \
  --scan-missing-cover \
  --extract \
  --report-missing-artwork
```

6. Use custom audio extensions:

```bash
python3 music_cover_scan.py "/path/to/music" \
  --scan-missing-cover \
  --extensions .mp3 .m4a .flac .ogg .opus
```

## Flags

- `--scan-missing-cover` list album directories missing `cover.jpg`
- `--extract` extract embedded artwork to `cover.jpg` when missing
- `--report-missing-artwork` list directories with no `cover.jpg` and no embedded artwork
- `--dry-run` preview extraction without writing files
- `--extensions ...` override detected audio extensions
- `--verbose` enable verbose logs

## Notes

- The script only writes `cover.jpg` when `--extract` is used and `--dry-run` is not set.
- If a directory already has `cover.jpg`, extraction is skipped for that directory.
- Embedded image formats are auto-detected (JPEG/PNG/GIF/BMP/WEBP).
