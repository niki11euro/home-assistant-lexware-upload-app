"""Portable history migration: synthetic images, local files, no network."""
from __future__ import annotations

import dataclasses
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3

import aiohttp
from PIL import Image
import pytest

import archive_import
from archive_import import open_store, parse_startup_options
from bridge import (BridgeError, Manager, Settings, Store, atomic_write, image_info,
                    prepare_upload, utc_now)

FILE_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc52f"
VOUCHER_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc530"


def photo(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, "JPEG")
    return output.getvalue()


def seed_job(store, *, color="white", duplicate_of=None):
    job = store.create(300, True)
    data = photo(color)
    original = image_info(data)
    upload, meta = prepare_upload(data, 300, store.settings.max_upload_bytes)
    directory = store.directory(job)
    atomic_write(directory / "original.jpg", data)
    atomic_write(directory / "upload.jpg", upload)
    job = store.update(job["id"], status="ready",
        original={**original, "filename": "original.jpg", "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "dpi": 300},
        upload={**meta, "filename": "upload.jpg", "bytes": len(upload),
                "sha256": hashlib.sha256(upload).hexdigest()})
    if duplicate_of is None:
        store.save_ack(job, {"id": FILE_ID, "voucherId": VOUCHER_ID})
        return store.update(job["id"], status="uploaded", file_id=FILE_ID,
                            voucher_id=VOUCHER_ID, accepted_at=utc_now())
    return store.update(job["id"], status="duplicate", duplicate_of=duplicate_of["id"],
                        file_id=duplicate_of["file_id"], voucher_id=duplicate_of["voucher_id"],
                        accepted_at=duplicate_of["accepted_at"])


@pytest.fixture
def archive(tmp_path):
    settings = Settings.from_dict({})
    share = tmp_path / "share"
    old = Store(tmp_path / "old-data", share, settings)
    job = seed_job(old)
    duplicate = seed_job(old, duplicate_of=job)
    (old.root / archive_import.MARKER).write_text("WE.R Lexware Upload Bridge storage v1\n")
    root = old.root
    old.close()
    return settings, share, root, [job, duplicate]


def snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def edit_record(root, job, edit):
    path = root / job["month"] / job["id"] / "status.json"
    current = json.loads(path.read_text())
    edit(current)
    path.write_text(json.dumps(current))


def test_raw_import_flag_is_separate_typed_and_requires_both_gates_off():
    settings, requested = parse_startup_options({"import_archive_history": True})
    assert requested and not settings.scan_enabled and not settings.upload_enabled
    assert parse_startup_options({})[1] is False
    assert parse_startup_options({"import_archive_history": False, "scan_enabled": True})[0].scan_enabled
    for raw in [{"import_archive_history": "true"}, {"import_archive_history": 1},
                {"import_archive_history": None}, {"import_archive_history": True, "scan_enabled": True},
                {"import_archive_history": True, "upload_enabled": True}, []]:
        with pytest.raises(BridgeError):
            parse_startup_options(raw)


def test_fresh_database_and_existing_history_require_explicit_import(tmp_path, archive):
    settings, share, root, _ = archive
    before = snapshot(root)
    with pytest.raises(BridgeError) as error:
        open_store(tmp_path / "new-data", share, settings)
    assert error.value.code == "archive_import_required"
    assert not (tmp_path / "new-data/jobs.sqlite3").exists()
    assert snapshot(root) == before


def test_empty_fresh_installation_starts_without_import(tmp_path):
    settings = Settings()
    store = open_store(tmp_path / "data", tmp_path / "share", settings)
    try:
        assert store.recent() == []
    finally:
        store.close()


def test_confirmed_history_import_is_exact_and_restart_is_read_only(tmp_path, archive, monkeypatch):
    settings, share, root, jobs = archive
    before = snapshot(root)
    def forbidden(*args, **kwargs):
        raise AssertionError("Migration/startup/status must not open network sessions")
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    data = tmp_path / "new-data"
    imported = open_store(data, share, settings, import_history=True)
    try:
        assert {job["id"] for job in imported.recent()} == {job["id"] for job in jobs}
        for job in jobs:
            assert imported.get(job["id"]) == job
        assert not Manager(settings, imported).tasks
    finally:
        imported.close()
    assert snapshot(root) == before
    restarted = open_store(data, share, settings)
    try:
        assert len(restarted.recent()) == len(jobs)
        assert all(restarted.get(job["id"]) == job for job in jobs)
    finally:
        restarted.close()
    assert snapshot(root) == before


@pytest.mark.asyncio
async def test_import_restores_duplicate_guard_without_upload(tmp_path, archive):
    settings, share, _, jobs = archive
    active = dataclasses.replace(settings, upload_enabled=True)
    store = open_store(tmp_path / "new-data", share, settings, import_history=True)
    try:
        store.settings = active
        def forbidden(*args, **kwargs):
            raise AssertionError("Imported duplicate must never open a provider client")
        manager = Manager(active, store, mcp_factory=forbidden)
        job = store.create(300, True)
        await manager.archive(job["id"], photo())
        await manager.run_upload(job["id"])
        saved = store.get(job["id"])
        assert saved["status"] == "duplicate" and saved["file_id"] == FILE_ID
        assert saved["duplicate_of"] == jobs[0]["id"]
    finally:
        store.close()


def test_nonempty_database_cannot_be_imported_again(tmp_path, archive):
    settings, share, root, _ = archive
    data = tmp_path / "new-data"
    store = open_store(data, share, settings, import_history=True)
    store.close()
    before = snapshot(root)
    with pytest.raises(BridgeError) as error:
        open_store(data, share, settings, import_history=True)
    assert error.value.code == "archive_import_nonempty"
    assert snapshot(root) == before


@pytest.mark.parametrize("updates", [{"scan_enabled": True}, {"upload_enabled": True},
                                      {"scan_enabled": True, "upload_enabled": True}])
def test_direct_import_api_also_enforces_disabled_gates(tmp_path, archive, updates):
    settings, share, _, _ = archive
    with pytest.raises(BridgeError) as error:
        open_store(tmp_path / "new-data", share, dataclasses.replace(settings, **updates), import_history=True)
    assert error.value.code == "archive_import_gates"
    assert not (tmp_path / "new-data/jobs.sqlite3").exists()


@pytest.mark.parametrize("edit", [
    lambda job: job.update(status="needs_review"),
    lambda job: job.update(status="uploading"),
    lambda job: job.update(status="ready"),
    lambda job: job.update(status="queued"),
    lambda job: job.update(status="new_unknown_state"),
    lambda job: job.update(extra_field=True),
    lambda job: job.update(scope="Private"),
    lambda job: job.update(id="a7c66ab7-874c-49d5-8f29-f369af0fc599"),
    lambda job: job.update(month="2026-01"),
    lambda job: job.update(created_at="2026-10-19T20:00:00+02:00"),
    lambda job: job.update(accepted_at="2026-02-30T20:00:00+00:00"),
    lambda job: job.update(file_id="bad-id"),
    lambda job: job.update(error={"code": "unknown"}),
    lambda job: job["original"].update(filename="../original.jpg"),
    lambda job: job["upload"].update(sha256="0" * 64),
    lambda job: job["upload"].update(bytes=1),
    lambda job: job["upload"].update(mime="text/html"),
    lambda job: job["original"].update(width=25),
])
def test_invalid_or_ambiguous_records_abort_all_imports(tmp_path, archive, edit):
    settings, share, root, jobs = archive
    edit_record(root, jobs[0], edit)
    before = snapshot(root)
    with pytest.raises(BridgeError):
        open_store(tmp_path / "new-data", share, settings, import_history=True)
    assert snapshot(root) == before
    assert not (tmp_path / "new-data/jobs.sqlite3").exists()


@pytest.mark.parametrize("field,value", [("job_id", "bad"), ("file_id", VOUCHER_ID),
    ("voucher_id", FILE_ID), ("upload_sha256", "0" * 64), ("accepted_at", "2026-10-19T21:00:00+01:00")])
def test_ack_identity_checksum_and_utc_must_match(tmp_path, archive, field, value):
    settings, share, root, jobs = archive
    path = root / jobs[0]["month"] / jobs[0]["id"] / "lexware-response.json"
    ack = json.loads(path.read_text()); ack[field] = value; path.write_text(json.dumps(ack))
    before = snapshot(root)
    with pytest.raises(BridgeError):
        open_store(tmp_path / "new-data", share, settings, import_history=True)
    assert snapshot(root) == before


@pytest.mark.parametrize("edit", [
    lambda job: job.update(duplicate_of=job["id"]),
    lambda job: job.update(duplicate_of="a7c66ab7-874c-49d5-8f29-f369af0fc599"),
    lambda job: job.update(file_id=VOUCHER_ID),
    lambda job: job.update(voucher_id=FILE_ID),
])
def test_duplicate_needs_matching_confirmed_uploaded_reference(tmp_path, archive, edit):
    settings, share, root, jobs = archive
    edit_record(root, jobs[1], edit)
    with pytest.raises(BridgeError):
        open_store(tmp_path / "new-data", share, settings, import_history=True)


@pytest.mark.parametrize("kind", ["status", "ack", "original", "upload", "job", "month", "extra", "hardlink"])
def test_links_unexpected_files_and_file_changes_are_rejected(tmp_path, archive, kind):
    settings, share, root, jobs = archive
    job = jobs[0]; directory = root / job["month"] / job["id"]
    paths = {"status": directory / "status.json", "ack": directory / "lexware-response.json",
             "original": directory / "original.jpg", "upload": directory / "upload.jpg",
             "job": directory, "month": directory.parent}
    if kind == "extra":
        (directory / "unexpected.txt").write_text("unrecognized")
    elif kind == "hardlink":
        target = directory / "original.jpg"
        (tmp_path / "outside-original").hardlink_to(target)
    else:
        target = paths[kind]; moved = tmp_path / ("outside-" + kind)
        target.rename(moved); target.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(BridgeError):
        open_store(tmp_path / "new-data", share, settings, import_history=True)


@pytest.mark.parametrize("contents", [b'{"id":"a","id":"b"}', b'{"id":NaN}', b'[]', b' ' * 65_537])
def test_bounded_strict_json(tmp_path, archive, contents):
    settings, share, root, jobs = archive
    (root / jobs[0]["month"] / jobs[0]["id"] / "status.json").write_bytes(contents)
    with pytest.raises(BridgeError):
        open_store(tmp_path / "new-data", share, settings, import_history=True)


def test_archive_change_before_commit_is_detected_without_rows(tmp_path, archive, monkeypatch):
    settings, share, root, jobs = archive
    original = archive_import._Evidence.recheck
    def changed(evidence):
        target = root / jobs[0]["month"] / jobs[0]["id"] / "upload.jpg"
        target.write_bytes(b"changed by a concurrent writer")
        original(evidence)
    monkeypatch.setattr(archive_import._Evidence, "recheck", changed)
    with pytest.raises(BridgeError):
        open_store(tmp_path / "new-data", share, settings, import_history=True)
    with closing(sqlite3.connect(tmp_path / "new-data/jobs.sqlite3")) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_database_failure_after_first_insert_rolls_back_every_row(tmp_path, archive, monkeypatch):
    settings, share, root, jobs = archive
    before = snapshot(root)
    rejected = sorted(jobs, key=lambda job: (job["created_at"], job["id"]))[1]["id"]
    def rejecting_store(*args):
        store = Store(*args)
        store.db.execute(f"CREATE TRIGGER reject_second BEFORE INSERT ON jobs WHEN NEW.id='{rejected}' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        return store
    monkeypatch.setattr(archive_import, "Store", rejecting_store)
    with pytest.raises(BridgeError):
        open_store(tmp_path / "new-data", share, settings, import_history=True)
    with closing(sqlite3.connect(tmp_path / "new-data/jobs.sqlite3")) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert snapshot(root) == before


def test_nonempty_import_rejection_precedes_recovery_archive_writes(tmp_path, archive):
    settings, share, root, jobs = archive
    old = tmp_path / "old-data/jobs.sqlite3"
    with closing(sqlite3.connect(old)) as db:
        raw = json.loads(db.execute("SELECT record FROM jobs WHERE id=?", (jobs[0]["id"],)).fetchone()[0])
        raw["status"] = "uploading"
        db.execute("UPDATE jobs SET record=? WHERE id=?", (json.dumps(raw), raw["id"]))
        db.commit()
    before = snapshot(root)
    with pytest.raises(BridgeError) as error:
        open_store(tmp_path / "old-data", share, settings, import_history=True)
    assert error.value.code == "archive_import_nonempty" and snapshot(root) == before

