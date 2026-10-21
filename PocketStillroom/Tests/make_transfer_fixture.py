#!/usr/bin/env python3
"""Make the native interoperability fixture from the Python export test data.

Uses only generated WAV/SQLite data in a disposable fixture directory. The
destination is explicit and the exporter refuses to replace an existing file.
No desktop application or real music library is opened.
"""
from pathlib import Path
import json
import sys
from test_transfer_export import ExportTests, export


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: make_transfer_fixture.py NEW-BUNDLE-PATH')
    fixture = ExportTests('test_exact_media_ids_metadata_loop_bounds_playlist_order_and_no_secrets')
    fixture.setUp()
    try:
        result = export.export_library(fixture.root, Path(sys.argv[1]))
        print(json.dumps(result, sort_keys=True))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
