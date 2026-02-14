#!/usr/bin/env python3
"""Scan music directories for missing artwork and optionally extract embedded covers.

Designed for macOS (and generally cross-platform), this script recursively walks a
music library and performs independent, flag-controlled steps:

1) Check album directories for missing `cover.jpg`
2) Extract embedded artwork from music files into `cover.jpg`
3) Report directories with neither `artwork.jpg` nor embedded artwork

Requires: mutagen
Install with: pip install mutagen
"""

from __future__ import annotations

import argparse
import logging
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively scan music folders, optionally extract embedded artwork to "
            "cover.jpg, and report folders with no artwork.jpg and no embedded art."
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
            "List album directories with neither artwork.jpg nor embedded artwork "
            "in their music files."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not write files. Useful with --extract to preview actions only."
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

    if not (args.scan_missing_cover or args.extract or args.report_missing_artwork):
        parser.error(
            "Enable at least one action flag: --scan-missing-cover, --extract, "
            "or --report-missing-artwork"
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
        artwork_jpg = album_dir / "artwork.jpg"
        has_cover = cover_jpg.is_file()

        if has_cover:
            stats.album_dirs_with_cover_jpg += 1
        else:
            stats.album_dirs_missing_cover_jpg += 1
            if actions.scan_missing_cover:
                print(f"MISSING cover.jpg: {album_dir}")

        embedded_source = None
        embedded_data = None

        if actions.extract or actions.report_missing_artwork:
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
            has_artwork_jpg = artwork_jpg.is_file()
            has_embedded = embedded_source is not None and embedded_data is not None
            if not has_artwork_jpg and not has_embedded:
                missing_both.append(album_dir)

    if actions.report_missing_artwork:
        print("\nDirectories with neither artwork.jpg nor embedded artwork:")
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
