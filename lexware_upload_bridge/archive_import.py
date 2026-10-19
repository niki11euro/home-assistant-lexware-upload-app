"""Explicit, offline migration of confirmed receipt history into a fresh database.

Archive files are evidence, never migration output. Any incomplete or ambiguous
record aborts the entire import; the old app must be stopped before migration.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote

from bridge import (BridgeError, MAX_PIXELS, MAX_SOURCE_BYTES, SAFE_FILENAME,
                    SCAN_CONFIGURATION_FIELDS, Settings, Store, UUID_RE,
                    archive_subdir, checked_directory, image_info, sanitize_scan_profile)

MARKER = ".wer-lexware-upload-bridge"
MAX_RECORD_BYTES = 65_536
MAX_ACK_BYTES = 8192
MAX_JOBS = 10_000
MAX_ARCHIVE_BYTES = 5_000_000_000
SHA_RE = re.compile(r"[0-9a-f]{64}")
UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)")
BASE_FIELDS = frozenset({"id", "month", "scope", "created_at", "updated_at", "status",
                         "resolution", "want_upload", "source_filename", "original",
                         "upload", "file_id", "voucher_id", "error", "duplicate_of", "accepted_at"})
OPTIONAL_FIELDS = frozenset({"scan_request_id", "scan_configuration", "scanner_settings"})
IMAGE_FIELDS = frozenset({"filename", "bytes", "sha256", "dpi", "width", "height", "mime", "extension"})


def _invalid() -> BridgeError:
    # Never expose private paths, parsed contents or original exceptions.
    return BridgeError("archive_import_invalid", "Archivhistorie ist unvollstaendig oder nicht eindeutig verifizierbar. Alte App und Sicherung pruefen; nichts importiert.", 409)


def parse_startup_options(raw: object) -> tuple[Settings, bool]:
    if not isinstance(raw, dict):
        raise BridgeError("invalid_options", "Die App-Einstellungen muessen ein Objekt sein.")
    options = dict(raw)
    requested = options.pop("import_archive_history", False)
    if type(requested) is not bool:
        raise BridgeError("invalid_import_flag", "Archivimport muss ein Schalter sein.")
    settings = Settings.from_dict(options)
    if requested and (settings.scan_enabled or settings.upload_enabled):
        raise BridgeError("archive_import_gates", "Fuer den Archivimport Scannen und Upload beide ausschalten.", 409)
    return settings, requested


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not UTC_RE.fullmatch(value):
        raise _invalid()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid() from None
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise _invalid()
    return parsed


def _uuid(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    try:
        value = json.loads(data, object_pairs_hook=_no_duplicate_keys,
                           parse_constant=lambda _: (_ for _ in ()).throw(_invalid()))
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Evidence:
    def __init__(self):
        self.files: list[tuple[Path, int, tuple, str]] = []
        self.directories: list[tuple[Path, tuple[str, ...]]] = []
        self.total_bytes = 0

    def children(self, directory: Path) -> list[Path]:
        checked_directory(directory, create=False)
        children = sorted(directory.iterdir(), key=lambda p: p.name)
        self.directories.append((directory, tuple(p.name for p in children)))
        return children

    def read(self, path: Path, limit: int) -> bytes:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= limit:
            raise _invalid()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if _fingerprint(os.fstat(source.fileno())) != _fingerprint(before):
                raise _invalid()
            data = source.read(limit + 1)
            if len(data) != before.st_size or _fingerprint(os.fstat(source.fileno())) != _fingerprint(before):
                raise _invalid()
        self.total_bytes += len(data)
        if self.total_bytes > MAX_ARCHIVE_BYTES:
            raise _invalid()
        self.files.append((path, limit, _fingerprint(before), hashlib.sha256(data).hexdigest()))
        return data

    def recheck(self) -> None:
        # Catch concurrent changes before inserting even the first database row.
        for directory, names in self.directories:
            checked_directory(directory, create=False)
            if tuple(sorted(p.name for p in directory.iterdir())) != names:
                raise _invalid()
        for path, limit, fingerprint, checksum in self.files:
            if _fingerprint(path.lstat()) != fingerprint:
                raise _invalid()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                data = source.read(limit + 1)
                if (_fingerprint(os.fstat(source.fileno())) != fingerprint or
                        hashlib.sha256(data).hexdigest() != checksum):
                    raise _invalid()


def _validate_image(job: dict, kind: str, directory: Path, evidence: _Evidence) -> str:
    meta = job[kind]
    fields = IMAGE_FIELDS | ({"modified", "quality"} if kind == "upload" else set())
    if not isinstance(meta, dict) or set(meta) != fields:
        raise _invalid()
    extension = meta["extension"]
    if (extension not in {".jpg", ".png"} or meta["filename"] != kind + extension or
            meta["mime"] != {".jpg": "image/jpeg", ".png": "image/png"}[extension] or
            type(meta["bytes"]) is not int or type(meta["dpi"]) is not int or
            not 75 <= meta["dpi"] <= job["resolution"] or
            type(meta["width"]) is not int or type(meta["height"]) is not int or
            min(meta["width"], meta["height"]) < 1 or meta["width"] * meta["height"] > MAX_PIXELS or
            not isinstance(meta["sha256"], str) or not SHA_RE.fullmatch(meta["sha256"])):
        raise _invalid()
    if kind == "original" and meta["dpi"] != job["resolution"]:
        raise _invalid()
    if kind == "upload" and (type(meta["modified"]) is not bool or
            (meta["quality"] is not None and (type(meta["quality"]) is not int or not 1 <= meta["quality"] <= 100))):
        raise _invalid()
    data = evidence.read(directory / meta["filename"], MAX_SOURCE_BYTES if kind == "original" else 4_800_000)
    if len(data) != meta["bytes"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise _invalid()
    actual = image_info(data)
    if any(meta[key] != value for key, value in actual.items()):
        raise _invalid()
    return meta["filename"]


def _validate_job(job: dict, month: str, jid: str, directory: Path, evidence: _Evidence) -> None:
    if (set(job) - BASE_FIELDS - OPTIONAL_FIELDS or not BASE_FIELDS <= set(job) or
            job["id"] != jid or job["month"] != month or job["scope"] != "WE.R" or
            job["status"] not in {"uploaded", "duplicate"} or job["error"] is not None or
            job["want_upload"] is not True or type(job["resolution"]) is not int or
            not 75 <= job["resolution"] <= 1200 or not _uuid(job["file_id"]) or
            (job["voucher_id"] is not None and not _uuid(job["voucher_id"]))):
        raise _invalid()
    created, updated, accepted = (_utc(job[key]) for key in ("created_at", "updated_at", "accepted_at"))
    if updated < created or accepted > updated:
        raise _invalid()
    if job["source_filename"] is not None and (not isinstance(job["source_filename"], str) or not SAFE_FILENAME.fullmatch(job["source_filename"])):
        raise _invalid()
    if "scan_request_id" in job and not _uuid(job["scan_request_id"]):
        raise _invalid()
    if "scan_configuration" in job:
        config = job["scan_configuration"]
        if not isinstance(config, dict) or set(config) != set(SCAN_CONFIGURATION_FIELDS):
            raise _invalid()
        Settings.from_dict(config)
        if config["scan_resolution"] != job["resolution"]:
            raise _invalid()
    if "scanner_settings" in job:
        profile = job["scanner_settings"]
        if not isinstance(profile, dict) or not isinstance(profile.get("params"), dict):
            raise _invalid()
        device = profile["params"].get("deviceId")
        if not isinstance(device, str) or not 1 <= len(device) <= 500:
            raise _invalid()
        clean = sanitize_scan_profile(profile, device)
        if clean["params"]["resolution"] != job["resolution"]:
            raise _invalid()
    expected = {"status.json", _validate_image(job, "original", directory, evidence),
                _validate_image(job, "upload", directory, evidence)}
    if job["status"] == "uploaded":
        if job["duplicate_of"] is not None:
            raise _invalid()
        expected.add("lexware-response.json")
        ack = _json(evidence.read(directory / "lexware-response.json", MAX_ACK_BYTES))
        if (set(ack) != {"job_id", "upload_sha256", "file_id", "voucher_id", "accepted_at"} or
                ack["job_id"] != jid or ack["upload_sha256"] != job["upload"]["sha256"] or
                ack["file_id"] != job["file_id"] or ack["voucher_id"] != job["voucher_id"]):
            raise _invalid()
        if not created <= _utc(ack["accepted_at"]) <= accepted:
            raise _invalid()
    elif not _uuid(job["duplicate_of"]) or job["duplicate_of"] == jid:
        raise _invalid()
    if set(p.name for p in evidence.children(directory)) != expected:
        raise _invalid()


def _records(root: Path) -> tuple[list[dict], _Evidence]:
    evidence = _Evidence()
    jobs: dict[str, dict] = {}
    for month in evidence.children(root):
        if month.name == MARKER:
            evidence.read(month, 128)
            continue
        if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month.name) or month.name.startswith("0000-"):
            raise _invalid()
        for directory in evidence.children(month):
            if not _uuid(directory.name) or directory.name in jobs or len(jobs) >= MAX_JOBS:
                raise _invalid()
            checked_directory(directory, create=False)
            job = _json(evidence.read(directory / "status.json", MAX_RECORD_BYTES))
            _validate_job(job, month.name, directory.name, directory, evidence)
            jobs[directory.name] = job
    if not jobs:
        raise BridgeError("archive_import_empty", "Keine bestaetigte Archivhistorie zum Import vorhanden.", 409)
    for job in jobs.values():
        if job["status"] == "duplicate":
            reference = jobs.get(job["duplicate_of"])
            if (reference is None or reference["status"] != "uploaded" or
                    any(job[key] != reference[key] for key in ("file_id", "voucher_id", "accepted_at")) or
                    job["upload"]["sha256"] != reference["upload"]["sha256"] or
                    _utc(job["created_at"]) < _utc(reference["created_at"])):
                raise _invalid()
    # Keep original accepted jobs before same-second duplicates so future
    # Store.duplicate() calls retain a direct reference to an uploaded job.
    return sorted(jobs.values(), key=lambda job: (_utc(job["created_at"]), job["status"] == "duplicate", job["id"])), evidence


def _has_rows(data_root: Path) -> bool:
    database = data_root / "jobs.sqlite3"
    if not database.exists() and not database.is_symlink():
        return False
    if not stat.S_ISREG(database.lstat().st_mode) or database.is_symlink():
        raise _invalid()
    # Read-only inspection must precede Store.recover(), which may update jobs.
    uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        return db.execute("SELECT EXISTS(SELECT 1 FROM jobs)").fetchone()[0] == 1


def open_store(data_root: Path, share_root: Path, settings: Settings, *, import_history: bool = False) -> Store:
    """Open normal storage, or import terminal history without touching the archive."""
    store = None
    try:
        if type(import_history) is not bool:
            raise BridgeError("invalid_import_flag", "Archivimport muss ein Schalter sein.")
        if import_history and (settings.scan_enabled or settings.upload_enabled):
            raise BridgeError("archive_import_gates", "Fuer den Archivimport Scannen und Upload beide ausschalten.", 409)
        checked_directory(data_root, create=True)
        root = checked_directory(share_root / archive_subdir(settings.archive_subdirectory), create=True)
        populated = _has_rows(data_root)
        if import_history and populated:
            raise BridgeError("archive_import_nonempty", "Die Auftragsdatenbank ist bereits befuellt. Import-Schalter ausschalten; nichts importiert.", 409)
        if not populated:
            contents = list(root.iterdir())
            if not import_history and any(path.name != MARKER for path in contents):
                raise BridgeError("archive_import_required", "Vorhandene Archivhistorie trifft auf eine leere Auftragsdatenbank. Erst ausdruecklich bei ausgeschaltetem Scan und Upload importieren.", 409)
        if not import_history:
            return Store(data_root, share_root, settings)
        jobs, evidence = _records(root)
        store = Store(data_root, share_root, settings)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            if store.db.execute("SELECT EXISTS(SELECT 1 FROM jobs)").fetchone()[0]:
                raise _invalid()
            evidence.recheck()
            store.db.executemany("INSERT INTO jobs(id,record) VALUES(?,?)",
                                 [(job["id"], json.dumps(job, ensure_ascii=False)) for job in jobs])
        return store
    except BridgeError:
        if store is not None:
            store.close()
        raise
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError):
        if store is not None:
            store.close()
        raise _invalid() from None
