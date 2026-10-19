#!/usr/bin/env python3
"""Container entrypoint. Startup initializes storage, not scanner/MCP traffic."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
from aiohttp import web
from bridge import BridgeError, Manager, Settings, Store, archive_subdir, checked_directory, make_app
from archive_import import open_store, parse_startup_options

UID, GID = 10001, 10001
MARKER = ".wer-lexware-upload-bridge"


def prepare_owned_directory(path: Path) -> None:
    existed = path.exists()
    checked_directory(path, create=True)
    marker = path / MARKER
    if marker.is_symlink():
        raise BridgeError("unsafe_marker", "Ungueltige Archiv-Markierung.")
    if existed and not marker.exists() and any(path.iterdir()):
        raise BridgeError("occupied_directory", "Zielordner ist nicht leer und gehoert nicht dieser App.")
    if not marker.exists():
        marker.write_text("WE.R Lexware Upload Bridge storage v1\n", encoding="utf-8")
    os.chmod(path, 0o750)
    if os.geteuid() == 0:
        os.chown(path, UID, GID)
        os.chown(marker, UID, GID)
    os.chmod(marker, 0o640)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options", default="/data/options.json")
    parser.add_argument("--data-root", default="/data/bridge")
    parser.add_argument("--share-root", default="/share")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings, import_history = parse_startup_options(json.loads(Path(args.options).read_text(encoding="utf-8")))
        if args.check_config:
            print(json.dumps({"valid": True, "scan_enabled": settings.scan_enabled,
                              "upload_enabled": settings.upload_enabled,
                              "import_archive_history": import_history}))
            return
        os.umask(0o027)
        share = checked_directory(Path(args.share_root), create=False)
        relative = Path(archive_subdir(settings.archive_subdirectory))
        # New parent folders are traversable. Existing parent permissions stay intact.
        parent = share
        for component in relative.parts[:-1]:
            parent /= component
            if not parent.exists():
                parent.mkdir(mode=0o755)
                os.chmod(parent, 0o755)
            checked_directory(parent, create=False)
        prepare_owned_directory(share / relative)
        prepare_owned_directory(Path(args.data_root))
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(GID)
            os.setuid(UID)
        store = open_store(Path(args.data_root), share, settings, import_history=import_history)
        manager = Manager(settings, store)
        logging.info("WE.R Bridge bereit. Scanfreigabe=%s, Uploadfreigabe=%s. Keine Startauftraege.",
                     settings.scan_enabled, settings.upload_enabled)
        web.run_app(make_app(manager), host="0.0.0.0", port=8099, access_log=None,
                    print=None, shutdown_timeout=20)
    except BridgeError as exc:
        logging.error("Start abgebrochen: %s", exc.message)
        raise SystemExit(1)
    except Exception as exc:
        # Configuration parsing errors can contain secrets; never print raw exceptions.
        logging.error("Start abgebrochen (%s). Einstellungen und Speicherrechte pruefen.", type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
