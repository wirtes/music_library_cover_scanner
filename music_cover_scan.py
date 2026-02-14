#!/usr/bin/env python3
"""Scan music directories for missing artwork and optionally extract embedded covers.

Designed for macOS (and generally cross-platform), this script recursively walks a
music library and performs independent, flag-controlled steps:

1) Check album directories for missing `cover.jpg`
2) Extract embedded artwork from music files into `cover.jpg`
3) Report directories with neither `cover.jpg` nor embedded artwork
4) Download cover art for directories missing `album.jpg`, `cover.jpg`, and embedded art

Requires: mutagen
Install with: pip install mutagen
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


MUSIC_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".mp4",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".wma",
    ".wav",
    ".aiff",
    ".aif",
    ".ape",
    ".mka",
}


@dataclass
class ScanStats:
    album_dirs_scanned: int = 0
    album_dirs_with_cover_jpg: int = 0
    album_dirs_missing_cover_jpg: int = 0
    covers_extracted: int = 0
    extraction_failures: int = 0
    artwork_downloaded: int = 0
    download_failures: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively scan music folders, optionally extract embedded artwork to "
            "cover.jpg, report missing artwork, or download missing covers."
        )
    )
    parser.add_argument(
        "root",
        type=Path,
        help="Root music directory to scan recursively.",
    )

    parser.add_argument(
        "--scan-missing-cover",
        action="store_true",
        help="List album directories missing cover.jpg.",
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="Extract embedded artwork to cover.jpg for directories missing it.",
    )
    parser.add_argument(
            "--report-missing-artwork",
            action="store_true",
            help=(
            "List album directories with neither cover.jpg nor embedded artwork "
            "in their music files."
            ),
    )
    parser.add_argument(
        "--download-missing-artwork",
        action="store_true",
        help=(
            "Download cover art for albums with no album.jpg, no cover.jpg, and "
            "no embedded artwork; save as cover.jpg."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not write files. Useful with --extract/--download-missing-artwork."
        ),
    )
    parser.add_argument(
        "--extensions",
        nargs="+",
        default=sorted(MUSIC_EXTENSIONS),
        help="Music file extensions to treat as audio (default: common formats).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )

    args = parser.parse_args()

    if not (
        args.scan_missing_cover
        or args.extract
        or args.report_missing_artwork
        or args.download_missing_artwork
    ):
        parser.error(
            "Enable at least one action flag: --scan-missing-cover, --extract, "
            "--report-missing-artwork, or --download-missing-artwork"
        )

    normalized = set()
    for ext in args.extensions:
        ext = ext.lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        normalized.add(ext)
    args.extensions = normalized

    return args


def is_album_dir(path: Path, extensions: set[str]) -> bool:
    try:
        return any(
            child.is_file() and child.suffix.lower() in extensions
            for child in path.iterdir()
        )
    except PermissionError:
        logging.warning("Skipping unreadable directory: %s", path)
        return False


def iter_album_dirs(root: Path, extensions: set[str]):
    for current, _, _ in __import__("os").walk(root):
        current_path = Path(current)
        if is_album_dir(current_path, extensions):
            yield current_path


def extract_embedded_image_bytes(audio_path: Path) -> Optional[bytes]:
    """Return first embedded artwork bytes found for a supported file."""
    try:
        from mutagen import File as MutagenFile
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise SystemExit(
            "This script requires mutagen. Install it with: pip install mutagen"
        ) from exc

    try:
        audio = MutagenFile(str(audio_path))
    except Exception as err:  # pragma: no cover - defensive I/O parsing guard
        logging.debug("Failed to read tags for %s: %s", audio_path, err)
        return None

    if audio is None:
        return None

    # ID3 (mp3): APIC frames
    tags = getattr(audio, "tags", None)
    if tags:
        try:
            for key in tags.keys():
                if key.startswith("APIC"):
                    frame = tags[key]
                    data = getattr(frame, "data", None)
                    if data:
                        return data
        except Exception:
            pass

        # FLAC / Ogg / others using mutagen Picture-like blocks
        pictures = getattr(tags, "pictures", None)
        if pictures:
            for pic in pictures:
                data = getattr(pic, "data", None)
                if data:
                    return data

        # MP4/M4A covr atom
        covr = tags.get("covr") if hasattr(tags, "get") else None
        if covr:
            first = covr[0]
            if isinstance(first, (bytes, bytearray)):
                return bytes(first)

        # Some formats expose metadata_block_picture (base64) but mutagen usually
        # decodes this into pictures; we intentionally keep logic compact.

    # FLAC object may expose pictures on the audio object itself.
    pictures = getattr(audio, "pictures", None)
    if pictures:
        for pic in pictures:
            data = getattr(pic, "data", None)
            if data:
                return data

    return None


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    # Basic signature checks for common embedded artwork formats.
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):  # GIF
        return True
    if data.startswith(b"BM"):  # BMP
        return True
    if data[0:4] == b"RIFF" and data[8:12] == b"WEBP":  # WEBP
        return True
    return False


def pick_source_with_embedded_art(dir_path: Path, extensions: set[str]) -> Optional[Path]:
    files = sorted(
        [p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() in extensions],
        key=lambda p: p.name.lower(),
    )
    for audio_file in files:
        data = extract_embedded_image_bytes(audio_file)
        if data and looks_like_image(data):
            return audio_file
    return None


def iter_audio_files(dir_path: Path, extensions: set[str]) -> list[Path]:
    return sorted(
        [p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() in extensions],
        key=lambda p: p.name.lower(),
    )


def extract_album_artist(audio_path: Path) -> tuple[Optional[str], Optional[str]]:
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return (None, None)

    try:
        audio = MutagenFile(str(audio_path), easy=True)
    except Exception:
        return (None, None)
    if audio is None:
        return (None, None)

    tags = getattr(audio, "tags", None)
    if not tags or not hasattr(tags, "get"):
        return (None, None)

    def first_tag(*names: str) -> Optional[str]:
        for name in names:
            value = tags.get(name)
            if isinstance(value, list) and value:
                text = str(value[0]).strip()
                if text:
                    return text
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        return None

    album = first_tag("album")
    artist = first_tag("albumartist", "artist")
    return (album, artist)


def album_artist_from_dir(dir_path: Path, extensions: set[str]) -> tuple[str, str]:
    files = iter_audio_files(dir_path, extensions)
    for audio_file in files:
        album, artist = extract_album_artist(audio_file)
        if album and artist:
            return (album, artist)
        if album:
            fallback_artist = dir_path.parent.name.strip() if dir_path.parent != dir_path else "Unknown Artist"
            return (album, fallback_artist or "Unknown Artist")

    album_name = dir_path.name.strip() or "Unknown Album"
    artist_name = dir_path.parent.name.strip() if dir_path.parent != dir_path else "Unknown Artist"
    return (album_name, artist_name or "Unknown Artist")


def fetch_itunes_artwork_bytes(album: str, artist: str, timeout: float = 12.0) -> Optional[bytes]:
    term = f"{artist} {album}".strip()
    params = urllib.parse.urlencode(
        {
            "term": term,
            "entity": "album",
            "media": "music",
            "limit": 10,
        }
    )
    search_url = f"https://itunes.apple.com/search?{params}"

    try:
        with urllib.request.urlopen(search_url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as err:
        logging.debug("Artwork search failed for '%s': %s", term, err)
        return None

    results = payload.get("results", [])
    if not isinstance(results, list):
        return None

    preferred_url = None
    lowered_album = album.lower().strip()
    lowered_artist = artist.lower().strip()
    for item in results:
        if not isinstance(item, dict):
            continue
        artwork_url = item.get("artworkUrl100")
        if not artwork_url:
            continue
        item_album = str(item.get("collectionName", "")).lower().strip()
        item_artist = str(item.get("artistName", "")).lower().strip()
        if lowered_album and lowered_album in item_album and (not lowered_artist or lowered_artist in item_artist):
            preferred_url = artwork_url
            break
        if preferred_url is None:
            preferred_url = artwork_url

    if not preferred_url:
        return None

    high_res_url = preferred_url.replace("100x100bb", "1200x1200bb")
    try:
        with urllib.request.urlopen(high_res_url, timeout=timeout) as resp:
            data = resp.read()
    except Exception as err:
        logging.debug("Artwork download failed from '%s': %s", high_res_url, err)
        return None

    return data if looks_like_image(data) else None


def write_cover_jpg(dir_path: Path, data: bytes, dry_run: bool) -> bool:
    cover_path = dir_path / "cover.jpg"
    if dry_run:
        logging.info("[dry-run] Would write %s", cover_path)
        return True
    try:
        cover_path.write_bytes(data)
        logging.info("Wrote %s", cover_path)
        return True
    except OSError as err:
        logging.error("Failed writing %s: %s", cover_path, err)
        return False


def process_album_dirs(root: Path, actions: argparse.Namespace) -> int:
    stats = ScanStats()
    missing_both: list[Path] = []

    for album_dir in iter_album_dirs(root, actions.extensions):
        stats.album_dirs_scanned += 1

        cover_jpg = album_dir / "cover.jpg"
        album_jpg = album_dir / "album.jpg"
        has_cover = cover_jpg.is_file()

        if has_cover:
            stats.album_dirs_with_cover_jpg += 1
        else:
            stats.album_dirs_missing_cover_jpg += 1
            if actions.scan_missing_cover:
                print(f"MISSING cover.jpg: {album_dir}")

        embedded_source = None
        embedded_data = None

        if actions.extract or actions.report_missing_artwork or actions.download_missing_artwork:
            embedded_source = pick_source_with_embedded_art(album_dir, actions.extensions)
            if embedded_source is not None:
                embedded_data = extract_embedded_image_bytes(embedded_source)

        if actions.extract and not has_cover:
            if embedded_data:
                ok = write_cover_jpg(album_dir, embedded_data, actions.dry_run)
                if ok:
                    stats.covers_extracted += 1
                else:
                    stats.extraction_failures += 1
            else:
                stats.extraction_failures += 1
                logging.info("No embedded art found for extraction: %s", album_dir)

        if actions.report_missing_artwork:
            has_embedded = embedded_source is not None and embedded_data is not None
            if not has_cover and not has_embedded:
                missing_both.append(album_dir)

        if actions.download_missing_artwork:
            has_embedded = embedded_source is not None and embedded_data is not None
            has_album_jpg = album_jpg.is_file()
            if not has_cover and not has_album_jpg and not has_embedded:
                album_name, artist_name = album_artist_from_dir(album_dir, actions.extensions)
                downloaded = fetch_itunes_artwork_bytes(album_name, artist_name)
                if downloaded:
                    ok = write_cover_jpg(album_dir, downloaded, actions.dry_run)
                    if ok:
                        stats.artwork_downloaded += 1
                    else:
                        stats.download_failures += 1
                else:
                    stats.download_failures += 1
                    logging.info(
                        "No downloadable artwork found for: %s (artist='%s', album='%s')",
                        album_dir,
                        artist_name,
                        album_name,
                    )

    if actions.report_missing_artwork:
        print("\nDirectories with neither cover.jpg nor embedded artwork:")
        if missing_both:
            for path in missing_both:
                print(path)
        else:
            print("(none)")

    print("\nSummary:")
    print(f"Album directories scanned: {stats.album_dirs_scanned}")
    print(f"Album directories with cover.jpg: {stats.album_dirs_with_cover_jpg}")
    print(f"Album directories missing cover.jpg: {stats.album_dirs_missing_cover_jpg}")
    if actions.extract:
        print(f"cover.jpg files {'to write' if actions.dry_run else 'written'}: {stats.covers_extracted}")
        print(f"Extraction failures/no embedded art: {stats.extraction_failures}")
    if actions.download_missing_artwork:
        print(f"Artwork downloads {'to write' if actions.dry_run else 'written'}: {stats.artwork_downloaded}")
        print(f"Download failures/not found: {stats.download_failures}")

    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    del argv  # argparse uses sys.argv directly
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    root = args.root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Root path is not a directory: {root}")

    return process_album_dirs(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
