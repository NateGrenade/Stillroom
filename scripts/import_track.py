#!/usr/bin/env python3
"""Copy a track into Stillroom; usable while the app is running."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.library import Library, LibraryError, clean_text


def main():
    parser = argparse.ArgumentParser(description="Add audio to the private Stillroom library.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Audio file to copy into the library")
    source.add_argument("--list", action="store_true", help="Print the library as JSON")
    parser.add_argument("--root", type=Path, default=ROOT, help="Library folder (default: this app's folder)")
    parser.add_argument("--title")
    parser.add_argument("--artist", default="")
    parser.add_argument("--album", default="")
    parser.add_argument("--kind", choices=("original", "loop"), default="original")
    parser.add_argument("--loop-start", type=float, default=0)
    parser.add_argument("--loop-end", type=float)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--playlist", help="Append to this playlist, creating it when needed")
    parser.add_argument("--allow-duplicate", action="store_true", help="Explicitly allow an existing file or title")
    args = parser.parse_args()
    try:
        library = Library(args.root)
        if args.list:
            result = library.library()
        else:
            if args.playlist is not None:
                clean_text(args.playlist, "Playlist name", 120, required=True)
            metadata = {"artist": args.artist, "album": args.album, "kind": args.kind,
                        "loopStart": args.loop_start, "loopEnd": args.loop_end,
                        "sourceUrl": args.source_url, "tags": [tag.strip() for tag in args.tags.split(",") if tag.strip()]}
            if args.title is not None:
                metadata["title"] = args.title
            track = library.import_file(args.file, metadata, allow_duplicate=args.allow_duplicate)
            result = {"track": track}
            if args.playlist is not None:
                result["playlist"] = library.add_to_playlist(args.playlist, track["id"])
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (LibraryError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
