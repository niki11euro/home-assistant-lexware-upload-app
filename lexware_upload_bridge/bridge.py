"""WE.R receipt bridge: explicit jobs, local archive first, then receipt upload.

No network access at startup. No directory watcher. No automatic retries of
scans or uploads. All external I/O is injectable for offline tests.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web
from PIL import Image, ImageOps

LOG = logging.getLogger("lexware_upload_bridge")
def _package_version() -> str:
    """config.yaml is the single release-version source for source and images."""
    match = re.search(r"^version:\s*['\"]?(\d+\.\d+\.\d+)['\"]?\s*$",
                      Path(__file__).with_name("config.yaml").read_text(encoding="utf-8"), re.M)
    if match is None:
        raise RuntimeError("Invalid package version")
    return match.group(1)


VERSION = _package_version()
LEXWARE_UPLOAD_URL = "https://api.lexware.io/v1/files"
LEXWARE_UPLOAD_TIMEOUT_SECONDS = 180
MAX_API_RESPONSE_BYTES = 64_000
MAX_SOURCE_BYTES = 100_000_000
MAX_PIXELS = 145_000_000  # A4 at 1200 dpi is approximately 139 million pixels.
MAX_RPC_BYTES = 10_000_000
INGRESS_PEER = "172.30.32.2"
JPG_PIPELINE = "JPG | @:pipeline.high-quality"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. ()-]{0,180}\.(?:jpg|jpeg|png)$", re.I)
SCAN_IMAGE_PIPELINES = frozenset({JPG_PIPELINE, "JPG | @:pipeline.medium-quality",
                                 "JPG | @:pipeline.low-quality", "PNG"})
SCAN_NUMERIC_FEATURES = {"top": "-t", "left": "-l", "width": "-x", "height": "-y",
                         "pageWidth": "--page-width", "pageHeight": "--page-height",
                         "brightness": "--brightness", "contrast": "--contrast"}
SCAN_ENUM_FEATURES = {"mode": "--mode", "source": "--source",
                      "adfMode": "--adf-mode", "ald": "--ald"}
SCAN_PARAM_NAMES = frozenset({"deviceId", "resolution", "dynamicLineart"}
                             | SCAN_NUMERIC_FEATURES.keys() | SCAN_ENUM_FEATURES.keys())
SCAN_CONFIGURATION_FIELDS = ("scanner_device_id", "scan_resolution", "scan_mode", "scan_format",
                             "scan_paper_size", "scan_brightness", "scan_contrast")
SCAN_PAPER_SIZES = {"A4": (210, 297), "A5": (148, 210), "A6": (105, 148)}


class BridgeError(Exception):
    """An error safe to expose without leaking tokens, bodies or receipt content."""
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BridgeError("unsafe_file", "Die lokale Archivdatei ist nicht regulaer.")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def validate_local_url(value: str, *, mcp: bool = False) -> str:
    """Allow only an explicitly configured local service, never request-chosen URLs."""
    try:
        u = urlsplit(value)
        _ = u.port
    except ValueError as exc:
        raise BridgeError("invalid_url", "Ungueltige interne Dienstadresse.") from exc
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise BridgeError("invalid_url", "Nur interne HTTP(S)-Adressen ohne Zugangsdaten sind erlaubt.")
    host = u.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        # Metadata/link-local, multicast and wildcard addresses must not be targeted.
        valid = address.is_private and not (address.is_link_local or address.is_multicast or address.is_unspecified)
    except ValueError:
        valid = bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", host)) or host.endswith(".local.hass.io")
    if not valid:
        raise BridgeError("nonlocal_url", "Scanner und MCP muessen interne Dienste sein.")
    if mcp and u.path.rstrip("/") != "/mcp":
        raise BridgeError("invalid_mcp_path", "Der MCP-Endpunkt muss auf /mcp enden.")
    if not mcp and u.path not in {"", "/"}:
        raise BridgeError("invalid_scanner_path", "Die Scanneradresse darf keinen Pfad enthalten.")
    return value.rstrip("/")


def archive_subdir(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(x in {"..", "."} for x in p.parts) or "\\" in value or "\x00" in value:
        raise BridgeError("invalid_archive", "Ungueltiger relativer Archivordner.")
    # This app is exclusively for business receipts. It must never target private paths.
    if len(p.parts) < 2 or p.parts[0] != "WE.R":
        raise BridgeError("wrong_scope", "Der Archivordner muss unter WE.R liegen.")
    return str(p)


def checked_directory(path: Path, *, create: bool) -> Path:
    """Reject symlinks in every existing component. Never follow archive symlinks."""
    if not path.is_absolute():
        raise BridgeError("invalid_path", "Ein absoluter Speicherpfad ist erforderlich.")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if not create:
                raise BridgeError("missing_path", "Der Speicherordner fehlt.")
            current.mkdir(mode=0o750)
            mode = current.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise BridgeError("unsafe_path", "Archivpfade duerfen keine Verknuepfungen enthalten.")
    return path


def atomic_write(path: Path, payload: bytes, *, replace: bool = False) -> None:
    """Commit a file and its directory entry durably, refusing accidental overwrites."""
    checked_directory(path.parent, create=False)
    if path.is_symlink():
        raise BridgeError("unsafe_file", "Verknuepfungen im Archiv sind nicht erlaubt.")
    temp = path.parent / (".tmp-" + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temp, path)
        else:
            # link is atomic and, unlike rename, fails if the destination exists.
            os.link(temp, path, follow_symlinks=False)
            temp.unlink()
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp.exists():
            temp.unlink()


@dataclass(frozen=True)
class Settings:
    scan_enabled: bool = False
    upload_enabled: bool = False
    scanserv_url: str = "http://172.30.32.1:8080"
    scanner_device_id: str = ""
    scan_resolution: int = 1200
    scan_mode: str = "Color"
    scan_format: str = "JPG"
    scan_paper_size: str = "A4"
    scan_brightness: int = 1000
    scan_contrast: int = 1000
    upload_backend: str = "mcp"
    lexware_api_key: str = field(default="", repr=False)
    mcp_url: str = ""
    mcp_token: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)
    archive_subdirectory: str = "WE.R/Lexware Upload"
    max_upload_bytes: int = 4_800_000
    scan_timeout_seconds: int = 900
    mcp_timeout_seconds: int = 180
    min_free_mb: int = 512

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        allowed = cls.__dataclass_fields__
        unknown = set(raw) - set(allowed)
        if unknown:
            raise BridgeError("unknown_option", "Unbekannte App-Einstellung.")
        raw = dict(raw)
        if isinstance(raw.get("scan_resolution"), str):
            if raw["scan_resolution"] not in {"75", "100", "200", "300", "600", "1200"}:
                raise BridgeError("invalid_resolution", "Die Aufloesung muss aus der angebotenen Liste gewaehlt werden.")
            raw["scan_resolution"] = int(raw["scan_resolution"])
        s = cls(**raw)
        if type(s.scan_enabled) is not bool or type(s.upload_enabled) is not bool:
            raise BridgeError("invalid_flag", "Scan- und Uploadfreigabe muessen Schalter sein.")
        for name in ["scan_resolution", "scan_brightness", "scan_contrast", "max_upload_bytes", "scan_timeout_seconds", "mcp_timeout_seconds", "min_free_mb"]:
            if type(getattr(s, name)) is not int:
                raise BridgeError("invalid_option", "Numerische Einstellungen muessen Ganzzahlen sein.")
        if s.scan_resolution not in {75, 100, 200, 300, 600, 1200} or not 100_000 <= s.max_upload_bytes <= 4_800_000:
            raise BridgeError("invalid_quality", "Ungueltige Aufloesung oder Uploadgroesse.")
        if (not isinstance(s.scan_mode, str) or s.scan_mode not in {"Color", "Gray", "Lineart"} or
                not isinstance(s.scan_format, str) or s.scan_format not in {"JPG", "PNG"} or
                not isinstance(s.scan_paper_size, str) or s.scan_paper_size not in {*SCAN_PAPER_SIZES, "Full"}):
            raise BridgeError("invalid_scan_option", "Ungueltiger Scanmodus, Bildtyp oder Papierbereich.")
        if not 0 <= s.scan_brightness <= 2000 or not 0 <= s.scan_contrast <= 2000:
            raise BridgeError("invalid_scan_lighting", "Helligkeit und Kontrast muessen zwischen 0 und 2000 liegen.")
        if not 60 <= s.scan_timeout_seconds <= 1800 or not 30 <= s.mcp_timeout_seconds <= 300:
            raise BridgeError("invalid_timeout", "Ungueltiges Zeitlimit.")
        if not 128 <= s.min_free_mb <= 100_000:
            raise BridgeError("invalid_space_limit", "Mindestens 128 MB freier Speicher sind erforderlich.")
        if not isinstance(s.upload_backend, str) or s.upload_backend not in {"mcp", "api"}:
            raise BridgeError("invalid_backend", "Als Uploadverbindung ist api oder mcp erlaubt.")
        for token in (s.api_token, s.mcp_token, s.lexware_api_key):
            if not isinstance(token, str) or any(ord(c) < 32 or ord(c) > 126 for c in token):
                raise BridgeError("invalid_token", "Zugangstoken darf keine Steuerzeichen enthalten.")
        if s.api_token and len(s.api_token) < 32:
            raise BridgeError("weak_api_token", "Der optionale API-Token muss mindestens 32 Zeichen haben.")
        if s.mcp_token and len(s.mcp_token) < 16:
            raise BridgeError("weak_mcp_token", "Der MCP-Token muss mindestens 16 Zeichen haben.")
        if s.lexware_api_key and (len(s.lexware_api_key) < 16 or len(s.lexware_api_key) > 4096 or s.lexware_api_key.strip() != s.lexware_api_key):
            raise BridgeError("invalid_lexware_key", "Der Lexware-API-Schluessel ist ungueltig.")
        if not isinstance(s.scanner_device_id, str) or len(s.scanner_device_id) > 500:
            raise BridgeError("invalid_device", "Ungueltige Scannerkennung.")
        validate_local_url(s.scanserv_url)
        if s.mcp_url:
            validate_local_url(s.mcp_url, mcp=True)
        archive_subdir(s.archive_subdirectory)
        return s

    @property
    def upload_configured(self) -> bool:
        if self.upload_backend == "api":
            return bool(self.lexware_api_key)
        return bool(self.mcp_url and self.mcp_token)


class Store:
    """Small SQLite journal plus one readable status file beside each receipt."""
    def __init__(self, data_root: Path, share_root: Path, settings: Settings):
        self.settings = settings
        self.data_root = checked_directory(data_root, create=True)
        self.root = checked_directory(share_root / archive_subdir(settings.archive_subdirectory), create=True)
        dbpath = data_root / "jobs.sqlite3"
        if dbpath.is_symlink():
            raise BridgeError("unsafe_database", "Die Auftragsdatenbank darf keine Verknuepfung sein.")
        self.db = sqlite3.connect(dbpath)
        os.chmod(dbpath, 0o600)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.commit()
        self.recover()

    def close(self) -> None:
        self.db.close()

    def check_space(self, extra_bytes: int = 0) -> None:
        needed = self.settings.min_free_mb * 1_000_000 + extra_bytes
        if shutil.disk_usage(self.root).free < needed:
            raise BridgeError("disk_full", "Zu wenig freier Speicher. Es wurde nichts hochgeladen.", 507)

    def directory(self, job: dict[str, Any]) -> Path:
        jid, month = job["id"], job["month"]
        if not UUID_RE.fullmatch(jid) or not re.fullmatch(r"\d{4}-\d{2}", month):
            raise BridgeError("invalid_job", "Ungueltige Archivkennung.")
        return checked_directory(self.root / month / jid, create=False)

    def get(self, jid: str) -> dict[str, Any]:
        if not UUID_RE.fullmatch(jid):
            raise BridgeError("invalid_job", "Ungueltige Auftragskennung.", 404)
        row = self.db.execute("SELECT record FROM jobs WHERE id=?", (jid,)).fetchone()
        if row is None:
            raise BridgeError("job_missing", "Auftrag nicht gefunden.", 404)
        return json.loads(row[0])

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT record FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,))]

    def create(self, resolution: int, want_upload: bool) -> dict[str, Any]:
        self.check_space()
        jid = str(uuid.uuid4())
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        checked_directory(self.root / month / jid, create=True)
        job = {"id": jid, "month": month, "scope": "WE.R", "created_at": utc_now(),
               "updated_at": utc_now(), "status": "queued", "resolution": resolution,
               "want_upload": want_upload, "source_filename": None, "original": None,
               "upload": None, "file_id": None, "voucher_id": None, "error": None,
               "duplicate_of": None, "accepted_at": None}
        self.save(job)
        return job

    def save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = utc_now()
        raw = json.dumps(job, ensure_ascii=False, indent=2).encode("utf-8")
        # A failed archive write prevents any subsequent remote side effect.
        atomic_write(self.directory(job) / "status.json", raw, replace=True)
        with self.db:
            self.db.execute("INSERT INTO jobs(id,record) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                            (job["id"], raw.decode("utf-8")))

    def update(self, jid: str, **changes: Any) -> dict[str, Any]:
        job = self.get(jid)
        job.update(changes)
        self.save(job)
        return job

    def path(self, job: dict[str, Any], kind: str) -> Path:
        if kind not in {"original", "upload"} or not job.get(kind):
            raise BridgeError("file_missing", "Die Archivdatei ist noch nicht vorhanden.", 404)
        name = job[kind]["filename"]
        if name not in {"original.jpg", "original.png", "upload.jpg", "upload.png"}:
            raise BridgeError("invalid_file", "Ungueltiger Archivdateiname.")
        path = self.directory(job) / name
        if path.is_symlink() or not path.is_file():
            raise BridgeError("file_missing", "Die Archivdatei fehlt oder ist nicht regulaer.", 404)
        return path

    def verify(self, job: dict[str, Any]) -> None:
        for kind in ("original", "upload"):
            if file_digest(self.path(job, kind)) != job[kind]["sha256"]:
                raise BridgeError("archive_changed", "Eine Archivdatei wurde veraendert. Upload gesperrt.", 409)

    def duplicate(self, job: dict[str, Any]) -> dict[str, Any] | None:
        for (raw,) in self.db.execute("SELECT record FROM jobs WHERE id<>?", (job["id"],)):
            other = json.loads(raw)
            if other.get("upload", {}) and other["upload"]["sha256"] == job["upload"]["sha256"]:
                if other["status"] in {"uploaded", "duplicate", "uploading", "needs_review"}:
                    return other
        return None

    def save_ack(self, job: dict[str, Any], result: dict[str, str]) -> None:
        ack = {"job_id": job["id"], "upload_sha256": job["upload"]["sha256"],
               "file_id": result["id"], "voucher_id": result.get("voucherId"), "accepted_at": utc_now()}
        atomic_write(self.directory(job) / "lexware-response.json", json.dumps(ack, indent=2).encode())

    def ack_exists(self, job: dict[str, Any]) -> bool:
        path = self.directory(job) / "lexware-response.json"
        return path.exists() or path.is_symlink()

    def load_ack(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Accept a local acknowledgement only when its identity and files match."""
        try:
            path = self.directory(job) / "lexware-response.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
                return None
            candidate = json.loads(path.read_text())
            if not isinstance(candidate, dict):
                return None
            if (candidate.get("job_id") != job["id"] or
                candidate.get("upload_sha256") != job["upload"]["sha256"] or
                not isinstance(candidate.get("file_id"), str) or
                not UUID_RE.fullmatch(candidate["file_id"])):
                return None
            voucher_id = candidate.get("voucher_id")
            if voucher_id is not None and (not isinstance(voucher_id, str) or not UUID_RE.fullmatch(voucher_id)):
                return None
            if not isinstance(candidate.get("accepted_at"), str):
                return None
            accepted_at = datetime.fromisoformat(candidate["accepted_at"])
            if accepted_at.tzinfo is None or accepted_at.utcoffset() != timezone.utc.utcoffset(None):
                return None
            self.verify(job)
            return candidate
        except (ValueError, OSError, BridgeError, KeyError, TypeError):
            return None

    def restore_ack(self, job: dict[str, Any]) -> bool:
        ack = self.load_ack(job)
        if ack is None:
            return False
        self.update(job["id"], status="uploaded", file_id=ack["file_id"],
                    voucher_id=ack.get("voucher_id"), accepted_at=ack["accepted_at"], error=None)
        return True

    def recover(self) -> None:
        # No network operations and no resuming jobs during startup.
        rows = list(self.db.execute("SELECT record FROM jobs"))
        for (raw,) in rows:
            job = json.loads(raw)
            if job["status"] in {"uploading", "needs_review", "interrupted", "error", "ready", "preflight"}:
                if self.restore_ack(job):
                    continue
                if self.ack_exists(job) or job.get("file_id") or job.get("accepted_at"):
                    self.update(job["id"], status="needs_review", error={"code": "invalid_ack",
                                "message": "Vorhandene Uploadbestaetigung ist nicht lokal verifizierbar. In Lexware pruefen."})
                    continue
            if job["status"] == "uploading":
                self.update(job["id"], status="needs_review", error={"code": "interrupted_upload",
                            "message": "Upload unterbrochen. Vor jedem neuen Versuch in Lexware pruefen."})
            elif job["status"] == "preflight":
                self.update(job["id"], status="ready", error={"code": "interrupted_preflight",
                            "message": "Vorpruefung unterbrochen. Noch kein Upload gesendet."})
            elif job["status"] in {"queued", "scanning", "receiving", "preparing", "archived"}:
                self.update(job["id"], status="interrupted", error={"code": "interrupted_job",
                            "message": "Auftrag unterbrochen. Kein automatischer Neustart oder Upload."})


def image_info(data: bytes) -> dict[str, Any]:
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise BridgeError("invalid_size", "Leere Datei oder Bilddatei groesser als 100 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Explicit limit instead of globally suppressing Pillow's safety checks.
            previous = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = MAX_PIXELS
            try:
                with Image.open(io.BytesIO(data)) as im:
                    if im.format not in {"JPEG", "PNG"} or getattr(im, "n_frames", 1) != 1:
                        raise BridgeError("invalid_format", "Nur einzelne JPG- und PNG-Bilder werden verarbeitet.")
                    if im.width * im.height > MAX_PIXELS:
                        raise BridgeError("too_many_pixels", "Die Bildaufloesung ueberschreitet das Sicherheitslimit.")
                    result = {"width": im.width, "height": im.height, "mime": Image.MIME[im.format],
                              "extension": ".jpg" if im.format == "JPEG" else ".png"}
                    im.verify()
                with Image.open(io.BytesIO(data)) as decoded:
                    decoded.load()
                return result
            finally:
                Image.MAX_IMAGE_PIXELS = previous
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError("invalid_image", "Die Bilddatei ist beschaedigt oder nicht unterstuetzt.") from exc


def prepare_upload(data: bytes, dpi: int, limit: int) -> tuple[bytes, dict[str, Any]]:
    info = image_info(data)
    if len(data) <= limit:
        return data, {**info, "modified": False, "dpi": dpi, "quality": None}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as raw:
                image = ImageOps.exif_transpose(raw).convert("RGB")
                icc = raw.info.get("icc_profile")
        try:
            # Preserve dimensions first; if necessary reduce only the upload copy.
            for target_dpi in sorted({dpi, min(dpi, 600), min(dpi, 300)}, reverse=True):
                scale = target_dpi / dpi
                size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
                candidate = image if size == image.size else image.resize(size, Image.Resampling.LANCZOS)
                try:
                    for quality in (95, 92, 88, 85):
                        target = io.BytesIO()
                        kwargs = {"quality": quality, "optimize": True, "subsampling": 0,
                                  "dpi": (target_dpi, target_dpi)}
                        if icc and len(icc) < 1_000_000:
                            kwargs["icc_profile"] = icc
                        candidate.save(target, "JPEG", **kwargs)
                        payload = target.getvalue()
                        if len(payload) <= limit:
                            return payload, {"extension": ".jpg", "mime": "image/jpeg", "width": size[0],
                                             "height": size[1], "modified": True,
                                             "dpi": target_dpi, "quality": quality}
                finally:
                    if candidate is not image:
                        candidate.close()
        finally:
            image.close()
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError("conversion_failed", "Uploadkopie konnte nicht sicher erzeugt werden.") from exc
    raise BridgeError("cannot_fit", "Datei bleibt zu gross. Original gesichert; bitte manuell pruefen.")


async def read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    if response.content_length is not None and response.content_length > limit:
        raise BridgeError("response_too_large", "Die Antwort ueberschreitet die erlaubte Groesse.")
    chunks, length = [], 0
    async for block in response.content.iter_chunked(65536):
        length += len(block)
        if length > limit:
            raise BridgeError("response_too_large", "Die Antwort ueberschreitet die erlaubte Groesse.")
        chunks.append(block)
    return b"".join(chunks)


def _profile_number(value: Any) -> int | float:
    """Normalise numeric text fields from scanservjs without JS-style coercion."""
    if isinstance(value, str):
        value = value.strip()
        if len(value) > 64 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value):
            raise BridgeError("invalid_scan_profile", "Das Scannerprofil enthaelt einen ungueltigen Zahlenwert.")
    elif type(value) not in {int, float}:
        raise BridgeError("invalid_scan_profile", "Das Scannerprofil enthaelt einen ungueltigen Zahlenwert.")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise BridgeError("invalid_scan_profile", "Das Scannerprofil enthaelt einen ungueltigen Zahlenwert.") from exc
    if not math.isfinite(number):
        raise BridgeError("invalid_scan_profile", "Das Scannerprofil enthaelt einen ungueltigen Zahlenwert.")
    return int(number) if number.is_integer() else number


def sanitize_scan_profile(raw: Any, configured_device_id: str) -> dict[str, Any]:
    """Copy the selected browser profile; never accept addresses, paths or commands."""
    if not isinstance(raw, dict) or set(raw) - {"params", "pipeline", "filters", "batch", "index", "version"}:
        raise BridgeError("invalid_scan_profile", "Das Scannerprofil ist ungueltig oder enthaelt fremde Felder.")
    params = raw.get("params")
    if not isinstance(params, dict) or set(params) - SCAN_PARAM_NAMES:
        raise BridgeError("invalid_scan_profile", "Das Scannerprofil enthaelt unzulaessige Scannerparameter.")
    if not configured_device_id or params.get("deviceId") != configured_device_id:
        raise BridgeError("wrong_scanner", "Bitte in scanservjs den konfigurierten WE.R-Scanner auswaehlen.")
    if params.get("source") != "Flatbed":
        raise BridgeError("flatbed_required", "Diese App verarbeitet nur eine einzelne Flachbettseite.")
    if raw.get("batch", "none") != "none" or type(raw.get("index", 1)) is not int or raw.get("index", 1) != 1:
        raise BridgeError("single_page_required", "Mehrseitenauftraege sind in dieser App nicht freigegeben.")
    pipeline = raw.get("pipeline")
    if not isinstance(pipeline, str) or pipeline not in SCAN_IMAGE_PIPELINES:
        raise BridgeError("image_pipeline_required", "Bitte in scanservjs JPG oder PNG als Format auswaehlen.")
    filters = raw.get("filters")
    if not isinstance(filters, list) or len(filters) > 32 or any(
            not isinstance(item, str) or not item or len(item) > 200 or
            any(ord(c) < 32 for c in item) for item in filters):
        raise BridgeError("invalid_scan_filters", "Die Scannerfilter sind ungueltig.")
    if len(set(filters)) != len(filters):
        raise BridgeError("invalid_scan_filters", "Die Scannerfilter duerfen nicht mehrfach angegeben werden.")
    clean: dict[str, Any] = {"deviceId": configured_device_id}
    resolution = _profile_number(params.get("resolution"))
    if type(resolution) is not int or not 75 <= resolution <= 1200:
        raise BridgeError("invalid_resolution", "Das Scannerprofil muss 75 bis 1200 dpi als ganze Zahl enthalten.")
    clean["resolution"] = resolution
    for key in SCAN_NUMERIC_FEATURES:
        if key in params:
            value = _profile_number(params[key])
            if ((key in {"top", "left"} and value < 0) or
                    (key in {"width", "height", "pageWidth", "pageHeight"} and value <= 0)):
                raise BridgeError("invalid_geometry", "Der gewaehlte Scanbereich ist ungueltig.")
            clean[key] = value
    for key in SCAN_ENUM_FEATURES:
        if key in params:
            value = params[key]
            if not isinstance(value, str) or not value or len(value) > 100 or any(ord(c) < 32 for c in value):
                raise BridgeError("invalid_scan_profile", "Das Scannerprofil enthaelt eine ungueltige Auswahl.")
            clean[key] = value
    if "dynamicLineart" in params:
        if type(params["dynamicLineart"]) is not bool:
            raise BridgeError("invalid_scan_profile", "Die Lineart-Einstellung muss ein Schalter sein.")
        clean["dynamicLineart"] = params["dynamicLineart"]
    return {"params": clean, "pipeline": pipeline, "filters": list(filters), "batch": "none", "index": 1}


def _validate_scan_capabilities(profile: dict[str, Any], device: dict[str, Any], *, require_complete: bool = True) -> None:
    features, settings = device.get("features"), device.get("settings")
    if not isinstance(features, dict) or not isinstance(settings, dict):
        raise BridgeError("invalid_scanner_context", "Scanner lieferte keinen gueltigen Konfigurationskontext.", 502)
    params = profile["params"]

    def feature(name: str) -> dict[str, Any]:
        value = features.get(name)
        if not isinstance(value, dict):
            raise BridgeError("unsupported_scan_parameter", "Eine gewaehlte Einstellung wird vom Scanner nicht angeboten.")
        return value

    def options(group: dict[str, Any], name: str) -> list[Any]:
        value = group.get(name)
        result = value.get("options") if isinstance(value, dict) else None
        if not isinstance(result, list):
            raise BridgeError("invalid_scanner_context", "Scanner lieferte keine gueltige Auswahlliste.", 502)
        return result

    if params["resolution"] not in options(features, "--resolution"):
        raise BridgeError("resolution_unavailable", "Diese Aufloesung wird vom Scanner nicht angeboten.")
    if profile["pipeline"] not in options(settings, "pipeline"):
        raise BridgeError("pipeline_missing", "Das gewaehlte Bildformat wird vom Scanner nicht angeboten.")
    filter_options = options(settings, "filters")
    if any(item not in filter_options for item in profile["filters"]):
        raise BridgeError("filter_unavailable", "Ein gewaehlter Scannerfilter wird nicht angeboten.")
    if "none" not in options(settings, "batchMode"):
        raise BridgeError("single_page_unavailable", "Der Scanner bietet keinen Einzelauftrag an.")
    for key, name in SCAN_ENUM_FEATURES.items():
        if key in params:
            if params[key] not in options(features, name):
                raise BridgeError("selection_unavailable", "Eine gewaehlte Scannereinstellung wird nicht angeboten.")
        elif require_complete and name in features and key != "ald":
            raise BridgeError("incomplete_scan_profile", "Scannerprofil unvollstaendig. Bitte scanservjs erneut oeffnen.")
    if "dynamicLineart" in params:
        feature("--disable-dynamic-lineart")
    bounds: dict[str, tuple[int | float, int | float]] = {}
    for key, name in SCAN_NUMERIC_FEATURES.items():
        if key not in params:
            if require_complete and name in features:
                raise BridgeError("incomplete_scan_profile", "Scannerprofil unvollstaendig. Bitte scanservjs erneut oeffnen.")
            continue
        limits = feature(name).get("limits")
        if not isinstance(limits, list) or len(limits) != 2 or any(type(x) not in {int, float} for x in limits):
            raise BridgeError("invalid_scanner_context", "Scanner lieferte keine gueltigen Bereichsgrenzen.", 502)
        try:
            lower, upper = (_profile_number(x) for x in limits)
        except BridgeError as exc:
            raise BridgeError("invalid_scanner_context", "Scanner lieferte keine gueltigen Bereichsgrenzen.", 502) from exc
        if lower > upper:
            raise BridgeError("invalid_scanner_context", "Scanner lieferte keine gueltigen Bereichsgrenzen.", 502)
        if not lower <= params[key] <= upper:
            raise BridgeError("scan_value_out_of_range", "Eine Scannereinstellung liegt ausserhalb der angebotenen Grenzen.")
        bounds[key] = (lower, upper)
    for origin, extent in (("left", "width"), ("top", "height")):
        if origin in params and extent in bounds and params[origin] + params[extent] > bounds[extent][1] + 1e-7:
            raise BridgeError("invalid_geometry", "Der gewaehlte Scanbereich ragt ueber das Flachbett hinaus.")


def configured_scan_profile(settings: Settings, device: dict[str, Any]) -> dict[str, Any]:
    """Build one page from immutable app options and the advertised active features."""
    features = device.get("features")
    if not isinstance(features, dict):
        raise BridgeError("invalid_scanner_context", "Scanner lieferte keinen gueltigen Konfigurationskontext.", 502)

    def active(name: str) -> bool:
        feature = features.get(name)
        return isinstance(feature, dict) and feature.get("enabled", True) is not False

    def limits(name: str) -> tuple[int | float, int | float]:
        value = features[name].get("limits")
        if (not isinstance(value, list) or len(value) != 2 or
                any(type(x) not in {int, float} for x in value)):
            raise BridgeError("invalid_scanner_context", "Scanner lieferte keine gueltigen Bereichsgrenzen.", 502)
        try:
            lower, upper = (_profile_number(x) for x in value)
        except BridgeError as exc:
            raise BridgeError("invalid_scanner_context", "Scanner lieferte keine gueltigen Bereichsgrenzen.", 502) from exc
        if lower > upper:
            raise BridgeError("invalid_scanner_context", "Scanner lieferte keine gueltigen Bereichsgrenzen.", 502)
        return lower, upper

    if not active("--resolution"):
        raise BridgeError("resolution_unavailable", "Der Scanner bietet keine aktive Aufloesungsauswahl an.")
    source_options = features.get("--source", {}).get("options") if isinstance(features.get("--source"), dict) else None
    if not active("--source") or not isinstance(source_options, list) or "Flatbed" not in source_options:
        raise BridgeError("flatbed_unavailable", "Der konfigurierte Scanner bietet kein freigegebenes Flachbett an.")
    params: dict[str, Any] = {"deviceId": settings.scanner_device_id,
                              "resolution": settings.scan_resolution, "source": "Flatbed"}
    if active("--mode"):
        params["mode"] = settings.scan_mode
    for key, value in (("brightness", settings.scan_brightness), ("contrast", settings.scan_contrast)):
        if active(SCAN_NUMERIC_FEATURES[key]):
            params[key] = value
    for key, name in (("left", "-l"), ("top", "-t")):
        if active(name):
            params[key] = limits(name)[0]
    if settings.scan_paper_size == "Full":
        for extent, name, origin in (("width", "-x", "left"), ("height", "-y", "top"),
                                     ("pageWidth", "--page-width", "left"),
                                     ("pageHeight", "--page-height", "top")):
            if active(name):
                params[extent] = limits(name)[1] - params.get(origin, 0)
    else:
        width, height = SCAN_PAPER_SIZES[settings.scan_paper_size]
        for key, value in (("width", width), ("height", height), ("pageWidth", width), ("pageHeight", height)):
            if active(SCAN_NUMERIC_FEATURES[key]):
                params[key] = value
    for key in ("left", "top", "width", "height", "pageWidth", "pageHeight"):
        if key in params and (params[key] < 0 or (key not in {"left", "top"} and params[key] == 0)):
            raise BridgeError("invalid_geometry", "Der konfigurierte Scanbereich ist ungueltig.")
    profile = {"params": params, "pipeline": JPG_PIPELINE if settings.scan_format == "JPG" else "PNG",
               "filters": [], "batch": "none", "index": 1}
    _validate_scan_capabilities(profile, device, require_complete=False)
    return profile


class ScannerClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = validate_local_url(settings.scanserv_url)
        self.last_scan_profile: dict[str, Any] | None = None

    async def scan(self, profile: dict[str, Any] | int | None = None) -> str:
        if not self.settings.scanner_device_id:
            raise BridgeError("device_not_configured", "Die Scannerkennung ist noch nicht konfiguriert.")
        # Legacy int/browser fixtures remain internal; public requests select app options only.
        legacy = type(profile) is int
        configured = profile is None or (isinstance(profile, dict) and "scan_resolution" in profile)
        selected_settings = self.settings
        if configured and profile is not None:
            if set(profile) != set(SCAN_CONFIGURATION_FIELDS):
                raise BridgeError("invalid_scan_configuration", "Die gespeicherte Scankonfiguration ist unvollstaendig.")
            selected_settings = Settings.from_dict(profile)
            if selected_settings.scanner_device_id != self.settings.scanner_device_id:
                raise BridgeError("wrong_scanner", "Die gespeicherte Scannerkennung passt nicht zur App.")
        if legacy:
            resolution = profile
            request = {"params": {"deviceId": self.settings.scanner_device_id, "mode": "Color",
                       "source": "Flatbed", "resolution": resolution, "top": 0, "left": 0,
                       "width": 210, "height": 297, "brightness": 1000, "contrast": 1000},
                       "pipeline": JPG_PIPELINE, "filters": [], "batch": "none", "index": 1}
        elif not configured:
            request = sanitize_scan_profile(profile, self.settings.scanner_device_id)
        timeout = aiohttp.ClientTimeout(total=self.settings.scan_timeout_seconds, connect=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(self.base + "/api/v1/context", allow_redirects=False) as response:
                if response.status != 200:
                    raise BridgeError("scanner_unavailable", "Scanner-Vorpruefung fehlgeschlagen.", 502)
                context = json.loads(await read_limited(response, 1_000_000))
            if not isinstance(context, dict) or not isinstance(context.get("devices"), list):
                raise BridgeError("invalid_scanner_context", "Scanner lieferte keinen gueltigen Konfigurationskontext.", 502)
            devices = [d for d in context["devices"] if isinstance(d, dict) and d.get("id") == self.settings.scanner_device_id]
            if len(devices) != 1:
                raise BridgeError("scanner_missing", "Der konfigurierte Scanner wird nicht eindeutig erkannt.", 409)
            device = devices[0]
            if configured:
                request = configured_scan_profile(selected_settings, device)
            elif legacy:
                if resolution not in device.get("features", {}).get("--resolution", {}).get("options", []):
                    raise BridgeError("resolution_unavailable", "Diese Aufloesung wird vom Scanner nicht angeboten.")
                if JPG_PIPELINE not in device.get("settings", {}).get("pipeline", {}).get("options", []):
                    raise BridgeError("pipeline_missing", "Das JPG-Profil mit hoher Qualitaet fehlt.")
            else:
                _validate_scan_capabilities(request, device)
            self.last_scan_profile = json.loads(json.dumps(request))
            # Exactly one POST. A timeout is never interpreted as permission to rescan.
            async with session.post(self.base + "/api/v1/scan", json=request, allow_redirects=False) as response:
                if response.status != 200:
                    raise BridgeError("scan_failed", "Scanner meldet einen Fehler. Nicht automatisch wiederholt.", 502)
                result = json.loads(await read_limited(response, 1_000_000))
            name = result.get("file", {}).get("name")
            if not isinstance(name, str) or not SAFE_FILENAME.fullmatch(name) or ".." in name:
                raise BridgeError("unsafe_scan_result", "Scanner lieferte keinen sicheren eindeutigen Bilddateinamen.", 502)
            return name

    async def download(self, name: str) -> bytes:
        if not SAFE_FILENAME.fullmatch(name) or ".." in name:
            raise BridgeError("unsafe_filename", "Ungueltiger Scan-Dateiname.")
        timeout = aiohttp.ClientTimeout(total=180, connect=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(self.base + "/api/v1/files/" + quote(name, safe=""), allow_redirects=False) as response:
                if response.status != 200:
                    raise BridgeError("download_failed", "Die konkrete Scan-Datei konnte nicht abgeholt werden.", 502)
                return await read_limited(response, MAX_SOURCE_BYTES)


class McpClient:
    """Minimal Streamable HTTP MCP client. Only one write tool is implemented."""
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None
        self.session_id: str | None = None
        self.protocol: str | None = None
        self.counter = 0
        self._upload_validator: Any = None

    async def __aenter__(self) -> "McpClient":
        if not self.settings.mcp_url or not self.settings.mcp_token:
            raise BridgeError("mcp_not_configured", "MCP-Adresse oder Zugangstoken fehlt. Noch kein Upload.", 409)
        validate_local_url(self.settings.mcp_url, mcp=True)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
            total=self.settings.mcp_timeout_seconds, connect=15), trust_env=False)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self.session:
            await self.session.close()

    def headers(self) -> dict[str, str]:
        headers = {"Authorization": "Bearer " + self.settings.mcp_token,
                   "Accept": "application/json, text/event-stream"}
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def rpc(self, method: str, params: dict[str, Any], *, notification: bool = False) -> dict[str, Any]:
        assert self.session is not None
        self.counter += 1
        rid = self.counter
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = rid
        async with self.session.post(self.settings.mcp_url, json=payload, headers=self.headers(), allow_redirects=False) as response:
            if response.status not in {200, 202, 204}:
                raise BridgeError("mcp_http", f"MCP-Anfrage abgewiesen (HTTP {response.status}).", 502)
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                if len(sid) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in sid):
                    raise BridgeError("mcp_session", "Ungueltige MCP-Sitzungskennung.", 502)
                self.session_id = sid
            if notification:
                await read_limited(response, 1_000_000)
                return {}
            if response.content_type == "text/event-stream":
                result = await self._sse(response, rid)
            else:
                result = json.loads(await read_limited(response, MAX_RPC_BYTES))
            if (not isinstance(result, dict) or type(result.get("id")) is not int or
                result.get("id") != rid or result.get("jsonrpc") != "2.0" or "method" in result or
                ("result" in result) == ("error" in result)):
                raise BridgeError("mcp_response", "MCP-Antwort konnte nicht zugeordnet werden.", 502)
            if "error" in result:
                raise BridgeError("mcp_rpc_error", "MCP meldet einen Protokoll- oder Berechtigungsfehler.", 502)
            answer = result.get("result")
            if not isinstance(answer, dict):
                raise BridgeError("mcp_result", "MCP lieferte kein gueltiges Ergebnis.", 502)
            return answer

    async def _server_request(self, packet: dict[str, Any]) -> None:
        """Reply only to protocol ping; reject unsupported server capabilities."""
        if "id" not in packet:  # Progress and logging notifications need no reply.
            return
        if type(packet["id"]) not in {str, int} or not isinstance(packet.get("method"), str):
            raise BridgeError("mcp_request", "MCP lieferte eine ungueltige Serveranfrage.", 502)
        answer: dict[str, Any] = {"jsonrpc": "2.0", "id": packet["id"]}
        if packet["method"] == "ping":
            answer["result"] = {}
        else:
            answer["error"] = {"code": -32601, "message": "Method not supported"}
        assert self.session is not None
        async with self.session.post(self.settings.mcp_url, json=answer, headers=self.headers(),
                                     allow_redirects=False) as response:
            if response.status not in {200, 202, 204}:
                raise BridgeError("mcp_reply", "MCP hat die Protokollantwort abgewiesen.", 502)
            await read_limited(response, 1_000_000)

    async def _sse(self, response: aiohttp.ClientResponse, rid: int) -> dict[str, Any]:
        # UTF-8 data is decoded by json.loads after a complete event. CR, LF and
        # CRLF framing is recognized even when a CRLF or BOM spans chunks.
        buffer, data, total = b"", [], 0
        start, skip_lf = True, False
        async for block in response.content.iter_chunked(32768):
            total += len(block)
            if total > MAX_RPC_BYTES:
                raise BridgeError("mcp_response_too_large", "MCP-Antwort ist zu gross.", 502)
            buffer += block
            if start:
                if len(buffer) < 3 and b"\xef\xbb\xbf".startswith(buffer):
                    continue
                if buffer.startswith(b"\xef\xbb\xbf"):
                    buffer = buffer[3:]
                start = False
            while buffer:
                if skip_lf:
                    if buffer.startswith(b"\n"):
                        buffer = buffer[1:]
                    skip_lf = False
                end = re.search(rb"\r\n|\r|\n", buffer)
                if end is None:
                    break
                line, buffer = buffer[:end.start()], buffer[end.end():]
                skip_lf = end.group() == b"\r"
                if line == b"data" or line.startswith(b"data:"):
                    value = line[5:] if line.startswith(b"data:") else b""
                    data.append(value[1:] if value.startswith(b" ") else value)
                elif not line and data:
                    packet = json.loads(b"\n".join(data))
                    data = []
                    if not isinstance(packet, dict) or packet.get("jsonrpc") != "2.0":
                        raise BridgeError("mcp_response", "MCP lieferte kein gueltiges Streamereignis.", 502)
                    if "method" in packet:
                        if not isinstance(packet["method"], str):
                            raise BridgeError("mcp_request", "MCP lieferte eine ungueltige Serveranfrage.", 502)
                        await self._server_request(packet)
                    elif type(packet.get("id")) is int and packet["id"] == rid:
                        return packet
        raise BridgeError("mcp_stream_incomplete", "MCP-Antwort wurde nicht vollstaendig empfangen.", 502)

    async def preflight(self) -> None:
        self._upload_validator = None
        result = await self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "wer-lexware-upload-bridge", "version": VERSION}})
        version = result.get("protocolVersion")
        if version not in {"2025-06-18", "2025-03-26"}:
            raise BridgeError("mcp_version", "Die ausgehandelte MCP-Version wird noch nicht unterstuetzt.", 409)
        self.protocol = version
        await self.rpc("notifications/initialized", {}, notification=True)
        cursor = None
        for _ in range(10):
            page = await self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            for tool in page.get("tools", []):
                if tool.get("name") == "upload-file":
                    schema = tool.get("inputSchema")
                    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
                    if not isinstance(properties, dict) or not {"fileBase64", "filename", "mimeType"}.issubset(properties):
                        raise BridgeError("mcp_schema_changed", "Das Uploadschema des MCP hat sich geaendert.", 409)
                    self._upload_validator = self._schema_validator(schema)
                    return
            cursor = page.get("nextCursor")
            if not cursor:
                break
        raise BridgeError("upload_not_permitted", "Der MCP stellt upload-file nicht bereit. Schreibrechte bleiben unveraendert.", 409)

    @staticmethod
    def _schema_validator(schema: dict[str, Any]) -> Any:
        """Compile the bounded object/string schema used by upload-file.

        This deliberately rejects unsupported validation keywords, references
        and types. It never fetches a schema or guesses an unfamiliar rule.
        """
        annotations = {"title", "description", "default", "examples", "deprecated",
                       "readOnly", "writeOnly", "$schema", "$id", "$comment"}
        supported = {"type", "properties", "required", "additionalProperties", "enum", "const",
                     "minLength", "maxLength", "pattern", "allOf", "anyOf", "oneOf"}

        def compile_rule(rule: Any, depth: int = 0) -> Callable[[Any], None]:
            if depth > 20:
                raise ValueError("Schema nesting limit")
            if type(rule) is bool:
                def boolean_rule(value: Any) -> None:
                    if not rule:
                        raise ValueError("Value forbidden")
                return boolean_rule
            if not isinstance(rule, dict) or set(rule) - supported - annotations:
                raise ValueError("Unsupported schema keyword")
            if "$schema" in rule and rule["$schema"] not in {
                "http://json-schema.org/draft-07/schema#", "https://json-schema.org/draft/2019-09/schema",
                "https://json-schema.org/draft/2020-12/schema"}:
                raise ValueError("Unsupported schema dialect")
            types = rule.get("type", [])
            if isinstance(types, str):
                types = [types]
            if (not isinstance(types, list) or any(t not in {"object", "string"} for t in types) or
                ("type" in rule and not types)):
                raise ValueError("Unsupported schema type")
            properties = rule.get("properties", {})
            if not isinstance(properties, dict) or any(not isinstance(k, str) for k in properties):
                raise ValueError("Invalid properties")
            properties = {k: compile_rule(v, depth + 1) for k, v in properties.items()}
            required = rule.get("required", [])
            if (not isinstance(required, list) or any(not isinstance(k, str) for k in required) or
                len(set(required)) != len(required)):
                raise ValueError("Invalid required properties")
            extra = compile_rule(rule.get("additionalProperties", True), depth + 1)
            enum = rule.get("enum")
            if "enum" in rule and (not isinstance(enum, list) or not enum):
                raise ValueError("Invalid enum")
            for key in ("minLength", "maxLength"):
                if key in rule and (type(rule[key]) is not int or rule[key] < 0):
                    raise ValueError("Invalid string length")
            pattern = rule.get("pattern")
            if "pattern" in rule:
                if not isinstance(pattern, str) or len(pattern) > 4096:
                    raise ValueError("Invalid string pattern")
                # Receipt base64 strings are large. Support only an anchored,
                # linear subset; unsupported regex constructs fail closed.
                if not pattern.startswith("^") or not pattern.endswith("$"):
                    raise ValueError("Unanchored pattern is unsupported")
                in_class, escaped, repeats = False, False, 0
                for character in pattern:
                    if escaped:
                        if character in "123456789":
                            raise ValueError("Regex backreference is unsupported")
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == "[":
                        in_class = True
                    elif character == "]":
                        in_class = False
                    elif not in_class:
                        if character in "()|":
                            raise ValueError("Regex groups are unsupported")
                        if character in "*+?{":
                            repeats += 1
                if repeats > 1:
                    raise ValueError("Multiple regex repetitions are unsupported")
                pattern = re.compile(pattern, flags=re.ASCII)
            alternatives = {}
            for key in ("allOf", "anyOf", "oneOf"):
                if key in rule:
                    if not isinstance(rule[key], list) or not rule[key] or len(rule[key]) > 32:
                        raise ValueError("Invalid schema alternatives")
                    alternatives[key] = [compile_rule(branch, depth + 1) for branch in rule[key]]

            def validate(value: Any) -> None:
                if types and not (("object" in types and isinstance(value, dict)) or
                                  ("string" in types and isinstance(value, str))):
                    raise ValueError("Value type differs")
                if enum is not None and value not in enum:
                    raise ValueError("Value absent from enum")
                if "const" in rule and value != rule["const"]:
                    raise ValueError("Value differs from const")
                if isinstance(value, dict):
                    if any(key not in value for key in required):
                        raise ValueError("Required property missing")
                    for key, item in value.items():
                        properties.get(key, extra)(item)
                if isinstance(value, str):
                    if len(value) < rule.get("minLength", 0) or len(value) > rule.get("maxLength", len(value)):
                        raise ValueError("String length differs")
                    if pattern is not None and pattern.search(value) is None:
                        raise ValueError("String pattern differs")
                for key, branches in alternatives.items():
                    matches = 0
                    for branch in branches:
                        try:
                            branch(value)
                            matches += 1
                        except ValueError:
                            pass
                    if (key == "allOf" and matches != len(branches)) or (key == "anyOf" and not matches) or (key == "oneOf" and matches != 1):
                        raise ValueError("Schema alternatives differ")
            return validate

        try:
            return compile_rule(schema)
        except Exception as exc:
            raise BridgeError("mcp_schema_changed", "Das Uploadschema des MCP ist ungueltig oder nicht unterstuetzt.", 409) from exc

    def validate_upload(self, data: bytes, filename: str, mime: str) -> dict[str, str]:
        if len(data) > self.settings.max_upload_bytes or mime not in {"image/jpeg", "image/png"}:
            raise BridgeError("invalid_upload", "Die Uploaddatei entspricht nicht den Vorgaben.")
        arguments = {
            "fileBase64": base64.b64encode(data).decode("ascii"), "filename": filename,
            "mimeType": mime, "type": "voucher"}
        if self._upload_validator is None:
            raise BridgeError("mcp_preflight_required", "Das Uploadschema wurde noch nicht geprueft.", 409)
        try:
            self._upload_validator(arguments)
        except Exception as exc:
            raise BridgeError("mcp_schema_changed", "Die Uploaddatei passt nicht zum angekuendigten MCP-Uploadschema. Noch kein Upload.", 409) from exc
        return arguments

    async def upload(self, data: bytes, filename: str, mime: str) -> dict[str, str]:
        arguments = self.validate_upload(data, filename, mime)
        result = await self.rpc("tools/call", {"name": "upload-file", "arguments": arguments})
        if result.get("isError"):
            raise BridgeError("mcp_tool_error", "Uploadwerkzeug meldet einen Fehler. Ergebnis vor Wiederholung pruefen.", 502)
        created = result.get("structuredContent", {})
        if not isinstance(created, dict) or not UUID_RE.fullmatch(str(created.get("id", ""))):
            raise BridgeError("missing_file_id", "Uploadantwort enthaelt keine bestaetigte Datei-ID.", 502)
        clean = {"id": created["id"]}
        if created.get("voucherId"):
            if not UUID_RE.fullmatch(str(created["voucherId"])):
                raise BridgeError("invalid_voucher_id", "Uploadantwort enthaelt eine ungueltige Beleg-ID.", 502)
            clean["voucherId"] = created["voucherId"]
        return clean


class LexwareApiClient:
    """Upload-only transport to Lexware's fixed HTTPS endpoint; never retries."""
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None
        self._prepared = False

    async def __aenter__(self) -> "LexwareApiClient":
        if not self.settings.lexware_api_key:
            raise BridgeError("lexware_not_configured", "Lexware-API-Schluessel fehlt. Noch kein Upload.", 409)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
            total=LEXWARE_UPLOAD_TIMEOUT_SECONDS, connect=15), trust_env=False)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self.session is not None:
            await self.session.close()

    async def preflight(self) -> None:
        # No provider request: an upload-only key need not grant profile/data reads.
        if self.session is None or self.session.closed:
            raise BridgeError("lexware_not_ready", "Die Lexware-Verbindung ist nicht vorbereitet.", 409)
        self._prepared = True

    def validate_upload(self, data: bytes, filename: str, mime: str) -> None:
        if not self._prepared:
            raise BridgeError("lexware_preflight_required", "Der Upload wurde noch nicht vorbereitet.", 409)
        if (not isinstance(data, bytes) or not data or len(data) > self.settings.max_upload_bytes or
                mime not in {"image/jpeg", "image/png"} or
                not isinstance(filename, str) or not SAFE_FILENAME.fullmatch(filename)):
            raise BridgeError("invalid_upload", "Die Uploaddatei entspricht nicht den Vorgaben.", 409)

    async def upload(self, data: bytes, filename: str, mime: str) -> dict[str, str]:
        self.validate_upload(data, filename, mime)
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=mime)
        form.add_field("type", "voucher")
        headers = {"Authorization": "Bearer " + self.settings.lexware_api_key,
                   "Accept": "application/json"}
        assert self.session is not None
        try:
            async with self.session.post(LEXWARE_UPLOAD_URL, data=form, headers=headers,
                                         allow_redirects=False) as response:
                if response.status != 202:
                    # No response body is logged or saved: it may contain private data.
                    raise BridgeError("lexware_http", f"Lexware-Upload nicht bestaetigt (HTTP {response.status}). Ergebnis vor Wiederholung pruefen.", 502)
                try:
                    result = json.loads(await read_limited(response, MAX_API_RESPONSE_BYTES))
                except (ValueError, UnicodeError):
                    raise BridgeError("lexware_response", "Lexware lieferte keine gueltige Uploadbestaetigung. Ergebnis vor Wiederholung pruefen.", 502) from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise BridgeError("lexware_request", "Lexware-Upload unterbrochen. Ergebnis vor Wiederholung pruefen; keine automatische Wiederholung.", 502) from None
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not UUID_RE.fullmatch(result["id"]):
            raise BridgeError("missing_file_id", "Uploadantwort enthaelt keine bestaetigte Datei-ID.", 502)
        clean = {"id": result["id"]}
        if "voucherId" in result:
            if not isinstance(result["voucherId"], str) or not UUID_RE.fullmatch(result["voucherId"]):
                raise BridgeError("invalid_voucher_id", "Uploadantwort enthaelt eine ungueltige Beleg-ID.", 502)
            clean["voucherId"] = result["voucherId"]
        return clean


class Manager:
    def __init__(self, settings: Settings, store: Store, scanner: Any = None,
                 mcp_factory: Callable[..., Any] | None = None):
        self.settings, self.store = settings, store
        self.scanner = scanner or ScannerClient(settings)
        # Keep the injection argument compatible with the legacy MCP tests/callers.
        self.mcp_factory = mcp_factory or (LexwareApiClient if settings.upload_backend == "api" else McpClient)
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.active_jobs: set[str] = set()

    def public_status(self) -> dict[str, Any]:
        jobs = self.store.recent()
        active_id = next(iter(self.active_jobs), None)
        active = self.store.get(active_id) if active_id else None
        latest = active or (jobs[0] if jobs else None)
        busy = bool(self.active_jobs)
        if busy:
            device_state = {"state": "busy", "label": "Auftrag laeuft",
                            "message": "Scan, lokale Sicherung und Upload sind gemeinsam gesperrt."}
        elif latest and (latest.get("error") or latest["status"] in {"error", "needs_review", "interrupted"}):
            device_state = {"state": "attention", "label": "Pruefung erforderlich",
                            "message": (latest.get("error") or {}).get("message", "Den letzten Auftrag bitte im Journal pruefen.")}
        elif not self.settings.scan_enabled or not self.settings.upload_enabled:
            device_state = {"state": "disabled", "label": "Freigabe fehlt",
                            "message": "Scannen und Lexware-Upload muessen in der App-Konfiguration freigegeben sein."}
        elif not self.settings.scanner_device_id or not self.settings.upload_configured:
            device_state = {"state": "not_configured", "label": "Konfiguration unvollstaendig",
                            "message": "Scannerkennung und gewaehlten Lexware-Zugang in der App-Konfiguration pruefen."}
        else:
            device_state = {"state": "idle", "label": "App bereit",
                            "message": "Lokaler App-Zustand. Der Scanner wird erst beim ausdruecklichen Scan kontaktiert."}
        progress = {"stage": "idle", "completed_steps": 0, "total_steps": 3,
                    "indeterminate": False, "label": "Kein Auftrag", "source": "local_journal"}
        if latest:
            phase = latest["status"]
            if phase in {"queued", "scanning"}:
                progress.update(stage="scan", label="Scannen", indeterminate=busy)
            elif phase in {"receiving", "archived", "preparing"}:
                progress.update(stage="archive", completed_steps=1, label="Lokal sichern", indeterminate=busy)
            elif phase in {"ready", "preflight", "uploading"}:
                progress.update(stage="upload", completed_steps=2, label="Lexware-Upload", indeterminate=busy)
            elif phase in {"uploaded", "duplicate"}:
                progress.update(stage="complete", completed_steps=3, label="Lexware hat den Upload angenommen")
            else:
                completed = 2 if latest.get("upload") and latest.get("original") else (1 if latest.get("source_filename") else 0)
                progress.update(stage="error", completed_steps=completed, label="Auftrag pruefen")
        return {"version": VERSION, "scope": "WE.R", "scan_enabled": self.settings.scan_enabled,
                "upload_enabled": self.settings.upload_enabled, "busy": busy,
                "scanner_configured": bool(self.settings.scanner_device_id),
                "upload_backend": self.settings.upload_backend,
                "upload_configured": self.settings.upload_configured,
                "mcp_configured": bool(self.settings.mcp_url and self.settings.mcp_token),
                "archive_subdirectory": self.settings.archive_subdirectory,
                "scanner_settings_source": "ha_app_configuration", "max_upload_bytes": self.settings.max_upload_bytes,
                "scan_profile": {"resolution": self.settings.scan_resolution, "mode": self.settings.scan_mode,
                                 "format": self.settings.scan_format, "paper_size": self.settings.scan_paper_size,
                                 "brightness": self.settings.scan_brightness, "contrast": self.settings.scan_contrast,
                                 "source": "Flatbed"},
                "active_job_id": active_id, "active_job": active, "device_state": device_state,
                "progress": progress, "jobs": jobs}

    def launch_scan(self, resolution: int | dict[str, Any] | None = None,
                    want_upload: bool = True, *, scan_request_id: str | None = None) -> dict[str, Any]:
        configured = resolution is None
        if configured:
            want_upload = True
        if not self.settings.scan_enabled:
            raise BridgeError("scan_disabled", "Scannen ist in den App-Einstellungen noch gesperrt.", 409)
        if want_upload and not self.settings.upload_enabled:
            raise BridgeError("upload_disabled", "Lexware-Uploads sind noch gesperrt.", 409)
        if self.active_jobs:
            raise BridgeError("busy", "Ein Auftrag laeuft bereits. Kein zweiter Scan gestartet.", 409)
        if scan_request_id is not None:
            if not isinstance(scan_request_id, str) or not UUID_RE.fullmatch(scan_request_id):
                raise BridgeError("invalid_scan_request_id", "Die Scan-Anfragekennung muss eine gueltige UUID sein.")
            if any(json.loads(row[0]).get("scan_request_id") == scan_request_id
                   for row in self.store.db.execute("SELECT record FROM jobs")):
                raise BridgeError("scan_request_already_used", "Diese Scan-Anfrage wurde bereits gestartet. Bitte den vorhandenen Auftrag pruefen.", 409)
        profile = None
        configuration = None
        if configured:
            configuration = {name: getattr(self.settings, name) for name in SCAN_CONFIGURATION_FIELDS}
            resolution = self.settings.scan_resolution
        elif isinstance(resolution, dict):
            profile = sanitize_scan_profile(resolution, self.settings.scanner_device_id)
            resolution = profile["params"]["resolution"]
        elif type(resolution) is not int or resolution not in {300, 600, 1200}:
            raise BridgeError("invalid_resolution", "Erlaubt sind 300, 600 oder 1200 dpi.")
        job = self.store.create(resolution, want_upload)
        changes: dict[str, Any] = {}
        if configuration is not None:
            changes["scan_configuration"] = configuration
        elif profile is not None:
            changes["scanner_settings"] = profile
        if scan_request_id is not None:
            changes["scan_request_id"] = scan_request_id
        if changes:
            job = self.store.update(job["id"], **changes)
        self._launch(job["id"], self.run_scan(job["id"]))
        return job

    def launch_upload(self, jid: str) -> dict[str, Any]:
        if not self.settings.upload_enabled:
            raise BridgeError("upload_disabled", "Lexware-Uploads sind noch gesperrt.", 409)
        if self.active_jobs:
            raise BridgeError("busy", "Ein Auftrag laeuft bereits.", 409)
        job = self.store.get(jid)
        if job["status"] != "ready":
            raise BridgeError("not_ready", "Nur lokal gesicherte, noch nicht gesendete Auftraege sind freigegeben.", 409)
        self._launch(jid, self.run_upload(jid))
        return job

    def _launch(self, jid: str, coro: Any) -> None:
        self.active_jobs.add(jid)
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def done(t: asyncio.Task[Any]) -> None:
            self.tasks.discard(t)
            self.active_jobs.discard(jid)
            if not t.cancelled() and t.exception():
                LOG.error("Auftrag %s intern abgebrochen (%s)", jid, type(t.exception()).__name__)
        task.add_done_callback(done)

    async def shutdown(self) -> None:
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    def _error(self, jid: str, exc: BaseException, status: str) -> None:
        if isinstance(exc, BridgeError):
            error = {"code": exc.code, "message": exc.message}
        else:
            error = {"code": "operation_failed", "message": "Vorgang unterbrochen. Dateien bleiben erhalten; keine automatische Wiederholung."}
        try:
            job = self.store.get(jid)
            # Session cleanup and cancellation cannot undo an accepted upload.
            if job["status"] in {"uploaded", "duplicate"}:
                LOG.warning("Nachbearbeitung fuer angenommenen Auftrag %s abgebrochen (%s)", jid, type(exc).__name__)
                return
            if self.store.restore_ack(job):
                return
            if self.store.ack_exists(job) or job.get("file_id") or job.get("accepted_at"):
                status = "needs_review"
            elif job["status"] == "uploading":
                status = "needs_review"
            elif status == "ready" and job["status"] not in {"ready", "preflight"}:
                status = job["status"]
            self.store.update(jid, status=status, error=error)
        except Exception:
            LOG.error("Status konnte fuer Auftrag %s nicht geschrieben werden; manuelle Pruefung erforderlich", jid)

    async def run_scan(self, jid: str) -> None:
        async with self.lock:
            try:
                job = self.store.update(jid, status="scanning")
                selected = job.get("scan_configuration", job.get("scanner_settings", job["resolution"]))
                name = await self.scanner.scan(selected)
                changes = {"status": "receiving", "source_filename": name}
                actual_profile = getattr(self.scanner, "last_scan_profile", None)
                if actual_profile is not None:
                    changes["scanner_settings"] = actual_profile
                job = self.store.update(jid, **changes)
                data = await self.scanner.download(name)
                await self.archive(jid, data)
                if job["want_upload"]:
                    await self._upload(jid)
            except asyncio.CancelledError as exc:
                phase = self.store.get(jid)["status"]
                self._error(jid, exc, "needs_review" if phase == "uploading" else "interrupted")
                raise
            except Exception as exc:
                phase = self.store.get(jid)["status"]
                target = "needs_review" if phase == "uploading" else ("ready" if phase in {"ready", "preflight"} else "error")
                self._error(jid, exc, target)

    async def archive(self, jid: str, data: bytes) -> None:
        info = await asyncio.to_thread(image_info, data)
        self.store.check_space(len(data) + self.settings.max_upload_bytes)
        job = self.store.get(jid)
        directory = self.store.directory(job)
        name = "original" + info["extension"]
        await asyncio.to_thread(atomic_write, directory / name, data)
        original = {**info, "filename": name, "bytes": len(data), "sha256": digest(data), "dpi": job["resolution"]}
        self.store.update(jid, status="archived", original=original)
        self.store.update(jid, status="preparing")
        candidate, meta = await asyncio.to_thread(prepare_upload, data, job["resolution"], self.settings.max_upload_bytes)
        upload_name = "upload" + meta["extension"]
        await asyncio.to_thread(atomic_write, directory / upload_name, candidate)
        upload = {**meta, "filename": upload_name, "bytes": len(candidate), "sha256": digest(candidate)}
        job = self.store.update(jid, status="ready", upload=upload, error=None)
        await asyncio.to_thread(self.store.verify, job)

    async def run_upload(self, jid: str) -> None:
        async with self.lock:
            try:
                await self._upload(jid)
            except asyncio.CancelledError as exc:
                phase = self.store.get(jid)["status"]
                self._error(jid, exc, "needs_review" if phase == "uploading" else "ready")
                raise
            except Exception as exc:
                phase = self.store.get(jid)["status"]
                self._error(jid, exc, "needs_review" if phase == "uploading" else "ready")

    async def _upload(self, jid: str) -> None:
        if not self.settings.upload_enabled:
            raise BridgeError("upload_disabled", "Lexware-Uploads sind noch gesperrt.", 409)
        job = self.store.get(jid)
        if job["status"] != "ready":
            raise BridgeError("not_ready", "Dieser Auftrag darf nicht automatisch gesendet werden.", 409)
        if self.store.restore_ack(job):
            return
        if self.store.ack_exists(job) or job.get("file_id") or job.get("accepted_at"):
            self.store.update(jid, status="needs_review")
            raise BridgeError("existing_upload", "Vorhandene Uploadbestaetigung muss zuerst geprueft werden. Nicht erneut gesendet.", 409)
        await asyncio.to_thread(self.store.verify, job)
        duplicate = self.store.duplicate(job)
        if duplicate:
            if duplicate["status"] in {"uploaded", "duplicate"} and duplicate.get("file_id"):
                self.store.update(jid, status="duplicate", duplicate_of=duplicate["id"], file_id=duplicate["file_id"],
                                  voucher_id=duplicate.get("voucher_id"), accepted_at=duplicate.get("accepted_at"))
                return
            raise BridgeError("duplicate_uncertain", "Gleiche Datei hat bereits einen unklaren Uploadstatus. Nicht erneut gesendet.", 409)
        self.store.check_space()
        self.store.update(jid, status="preflight", error=None)
        async with self.mcp_factory(self.settings) as mcp:
            await mcp.preflight()
            job = self.store.get(jid)
            await asyncio.to_thread(self.store.verify, job)
            data = await asyncio.to_thread(self.store.path(job, "upload").read_bytes)
            if digest(data) != job["upload"]["sha256"]:
                raise BridgeError("archive_changed", "Uploadkopie wurde veraendert.", 409)
            filename = f"WER_{jid}{job['upload']['extension']}"
            validator = getattr(mcp, "validate_upload", None)
            if validator is not None:
                validator(data, filename, job["upload"]["mime"])
            # Persist intent BEFORE the only write request. Unknown outcomes stay blocked.
            job = self.store.update(jid, status="uploading")
            result = await mcp.upload(data, filename, job["upload"]["mime"])
            self.store.save_ack(job, result)
            self.store.update(jid, status="uploaded", file_id=result["id"], voucher_id=result.get("voucherId"),
                              accepted_at=utc_now(), error=None)


MANAGER_KEY = web.AppKey("manager", Manager)
CSRF_KEY = web.AppKey("csrf", str)
PEERS_KEY = web.AppKey("trusted_peers", frozenset)


def make_app(manager: Manager, *, trusted_peers: frozenset[str] = frozenset({INGRESS_PEER})) -> web.Application:
    @web.middleware
    async def security(request: web.Request, handler: Any) -> web.StreamResponse:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        peer_ip = peer[0] if peer else ""
        if request.path == "/healthz" and request.method == "GET":
            if peer_ip not in {"127.0.0.1", "::1"} | set(trusted_peers):
                return web.json_response({"error": "forbidden"}, status=403)
            return web.json_response({"ok": True, "version": VERSION})
        header = request.headers.get("Authorization", "")
        token = manager.settings.api_token
        bearer_ok = bool(token) and header.isascii() and hmac.compare_digest(header, "Bearer " + token)
        ingress_ok = peer_ip in request.app[PEERS_KEY]
        if not bearer_ok and not ingress_ok:
            return web.json_response({"error": "Nicht angemeldet."}, status=401)
        if request.method not in {"GET", "HEAD"}:
            csrf = request.headers.get("X-CSRF-Token", "")
            if not bearer_ok and (not csrf.isascii() or not hmac.compare_digest(csrf, request.app[CSRF_KEY])):
                return web.json_response({"error": "CSRF-Pruefung fehlgeschlagen."}, status=403)
            if request.content_type != "application/json":
                return web.json_response({"error": "JSON erforderlich."}, status=415)
        try:
            response = await handler(request)
        except BridgeError as exc:
            response = web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
        except (ValueError, TypeError, KeyError):
            response = web.json_response({"error": "Ungueltige Anfrage."}, status=400)
        except web.HTTPException:
            raise
        except Exception as exc:
            LOG.error("Interner Anfragefehler (%s)", type(exc).__name__)
            response = web.json_response({"error": "Interner Fehler. Keine automatische Wiederholung."}, status=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
        return response

    app = web.Application(middlewares=[security], client_max_size=8192)
    app[MANAGER_KEY], app[CSRF_KEY], app[PEERS_KEY] = manager, secrets.token_urlsafe(32), trusted_peers
    static = Path(__file__).parent / "static"

    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(static / "index.html")

    async def asset(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in {"app.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(static / name)

    async def status(request: web.Request) -> web.Response:
        return web.json_response({**manager.public_status(), "csrf": app[CSRF_KEY]})

    async def scan(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict) or body:
            raise BridgeError("invalid_parameters", "Scanparameter werden ausschliesslich in der App-Konfiguration festgelegt.")
        job = manager.launch_scan(scan_request_id=request.headers.get("X-Scan-Request-ID"))
        return web.json_response({"job": job}, status=202)

    async def upload(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict) or body.get("confirm_business") is not True:
            raise BridgeError("scope_unconfirmed", "Bitte WE.R-Zuordnung bestaetigen.", 409)
        return web.json_response({"job": manager.launch_upload(request.match_info["jid"])}, status=202)

    async def download(request: web.Request) -> web.StreamResponse:
        job = manager.store.get(request.match_info["jid"])
        kind = request.match_info["kind"]
        path = manager.store.path(job, kind)
        if file_digest(path) != job[kind]["sha256"]:
            raise BridgeError("archive_changed", "Archiv-Pruefsumme stimmt nicht mehr.", 409)
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="WER_{job["id"]}_{path.name}"'})

    async def shutdown(app: web.Application) -> None:
        await manager.shutdown()
        manager.store.close()

    app.router.add_get("/healthz", status)  # middleware handles the health response.
    app.router.add_get("/", index)
    app.router.add_get("/{name:app.js|style.css}", asset)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/scan", scan)
    app.router.add_post("/api/jobs/{jid}/upload", upload)
    app.router.add_get("/api/jobs/{jid}/files/{kind}", download)
    app.on_cleanup.append(shutdown)
    return app
