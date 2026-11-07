"""Regression tests for disclosure boundaries, recovery and untrusted input."""
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import priorfolio as core
import priorfolio_exports as exports
from conftest import write


@pytest.fixture
def recorded(tmp_path):
    project = tmp_path / "private-user" / "private-project"
    write(project, {"confidential-name.txt": "private source bytes\n"})
    chosen = core.scan_roots([], [project])
    chosen[0]["disclosure"] = {"title": "Example invention", "description": "A demonstration.",
                               "contribution": "Designed the example.", "period": "2025",
                               "references": "https://example.com", "unknown": "DO NOT EXPORT"}
    return core.generate(chosen, tmp_path / "runs", snapshot=True, log=lambda _: None)


def rewrite_record(run, change):
    path = run / "evidence.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    (run / "BUNDLE_SHA256.txt").write_text(core.sha256_file(path), encoding="utf-8")


def test_disclosure_omits_private_fields_and_includes_user_descriptions(recorded):
    run = recorded["out_dir"]
    with zipfile.ZipFile(run / "disclosure.zip") as z:
        assert set(z.namelist()) == {"disclosure.md", "disclosure.json"}
        for name in z.namelist():
            body = z.read(name).decode("utf-8")
            for private in ("private-user", "private-project", "confidential-name.txt", "private source bytes", "DO NOT EXPORT"):
                assert private not in body
            assert "Example invention" in body
            assert "Designed the example." in body
    private = json.loads((run / "evidence.json").read_text(encoding="utf-8"))
    assert private["projects"][0]["disclosure"]["title"] == "Example invention"


def test_metadata_bundle_requires_explicit_limited_verification(recorded, tmp_path):
    received = tmp_path / "received"
    with zipfile.ZipFile(recorded["out_dir"] / "evidence_bundle.zip") as z:
        z.extractall(received)
    assert any("snapshot" in p for p in core.verify_run(received))
    assert core.verify_run(received, metadata_only=True, expect_bundle=recorded["bundle"]) == []
    manifest = next((received / "manifests").iterdir())
    manifest.write_bytes(b"tampered")
    assert core.verify_run(received, metadata_only=True)


@pytest.mark.parametrize("relative", ["../outside", "a/../../outside", "..\\outside", "/etc/passwd",
                                      "C:\\outside", "C:outside", "\\\\server\\share", "\\outside"])
def test_untrusted_manifest_paths_are_rejected(recorded, relative):
    run = recorded["out_dir"]
    rewrite_record(run, lambda data: data["projects"][0].update(manifest=relative))
    assert any("unsafe" in p for p in core.verify_run(run))


def test_snapshot_path_is_checked_even_in_metadata_mode(recorded):
    run = recorded["out_dir"]
    rewrite_record(run, lambda data: data["projects"][0].update(snapshot="../outside.zip"))
    assert core.verify_run(run, metadata_only=True)


def test_manifest_entries_cannot_escape_live_project(recorded):
    run = recorded["out_dir"]
    p = recorded["results"][0]
    raw = core.manifest_line("0" * 64, "../outside.txt").encode()
    (run / p["manifest"]).write_bytes(raw)
    rewrite_record(run, lambda data: data["projects"][0].update(content_sha256=hashlib.sha256(raw).hexdigest()))
    assert any("unsafe" in p for p in core.verify_run(run, against_disk=True))


def test_evidence_symlink_cannot_read_external_json(recorded, tmp_path):
    run = recorded["out_dir"]
    external = tmp_path / "external.json"
    external.write_bytes((run / "evidence.json").read_bytes())
    target = run / "evidence.json"
    target.unlink()
    try:
        target.symlink_to(external)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert "links" in core.verify_run(run)[0]
    assert core.run_summary(run) is None


@pytest.mark.parametrize("url", ["https://someone:PRIVATE@example.com/r?access_token=PRIVATE#PRIVATE",
                               "ssh://someone:PRIVATE@example.com/r", "https://example.com/r?token=PRIVATE",
                               "someone@example.com:r?token=PRIVATE"])
def test_remote_credentials_and_queries_are_not_exported(url):
    assert "PRIVATE" not in core.strip_credentials(url)
    assert "someone" not in core.strip_credentials(url)


def test_backup_restores_exact_evidence_and_needs_remote_confirmation(recorded, tmp_path):
    run = recorded["out_dir"]
    assert exports.backup_status(run)["state"] == "backup_required"
    (run / "unrelated-private-file.txt").write_text("not part of the evidence")
    target = tmp_path / "cloud-sync" / "backup.zip"
    target.parent.mkdir()
    status = exports.export_backup(run, target)
    assert status["state"] == "exported"
    assert status["archive_sha256"] == core.sha256_file(target)
    assert status["archive_sha256"] in Path(str(target) + ".sha256").read_text()
    restored = tmp_path / "restored"
    with zipfile.ZipFile(target) as z:
        assert "snapshots/private-project.zip" in z.namelist()
        assert "unrelated-private-file.txt" not in z.namelist()
        assert "backup_status.json" not in z.namelist()
        assert "BACKUP.txt" in z.namelist()
        z.extractall(restored)
    assert core.verify_run(restored, expect_bundle=recorded["bundle"]) == []
    assert exports.backup_status(restored)["state"] == "backup_required"
    confirmed = exports.confirm_backup(run, "cloud")
    assert confirmed["state"] == "confirmed_by_user"
    assert confirmed["method"] == "cloud"


def test_confirmation_cannot_precede_export(recorded):
    with pytest.raises(core.ToolError, match="Export"):
        exports.confirm_backup(recorded["out_dir"], "email")


def test_confirmation_is_invalidated_if_evidence_record_changes(recorded, tmp_path):
    run = recorded["out_dir"]
    exports.export_backup(run, tmp_path / "private.zip")
    exports.confirm_backup(run, "email")
    rewrite_record(run, lambda data: data.update(generated_utc="changed"))
    assert exports.backup_status(run)["state"] == "backup_required"


def test_backup_never_overwrites_existing_archive(recorded, tmp_path):
    target = tmp_path / "existing.zip"
    target.write_bytes(b"keep me")
    with pytest.raises(core.ToolError, match="already exists"):
        exports.export_backup(recorded["out_dir"], target)
    assert target.read_bytes() == b"keep me"


def test_backup_rejects_hash_only_run(recorded, tmp_path):
    result = core.generate(recorded["results"], tmp_path / "hash-only", log=lambda _: None)
    with pytest.raises(core.ToolError, match="snapshots"):
        exports.export_backup(result["out_dir"], tmp_path / "partial.zip")


def test_backup_rejects_tampered_evidence(recorded, tmp_path):
    (recorded["out_dir"] / recorded["results"][0]["snapshot"]).write_bytes(b"broken")
    with pytest.raises(core.ToolError, match="verification failed"):
        exports.export_backup(recorded["out_dir"], tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_cli_generate_backup_restore_and_confirmation(tmp_path):
    project = tmp_path / "example"
    write(project, {"example.txt": "source"})
    def cli(*args):
        return subprocess.run([sys.executable, str(Path(core.__file__)), *map(str, args)],
                              capture_output=True, text=True, cwd=tmp_path)
    result = cli("--add", project, "--select", "all", "--out", tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    run = next((tmp_path / "runs").iterdir())
    assert list((run / "snapshots").glob("*.zip"))
    target = tmp_path / "private.zip"
    result = cli("--backup", run, "--destination", target)
    assert result.returncode == 0, result.stderr
    assert "Remote backup pending" in result.stdout
    assert cli("--confirm-backup", run, "--backup-method", "email").returncode == 0
    assert exports.backup_status(run)["state"] == "confirmed_by_user"

    duplicate = cli("--backup", run, "--destination", target)
    assert duplicate.returncode != 0
    assert "already exists" in duplicate.stderr and "Traceback" not in duplicate.stderr


def test_timestamp_collection_never_claims_signature_verification(recorded):
    email = core.build_email(recorded["results"], {"generated_utc": "2026-01-01"}, recorded["bundle"],
                            [{"tsa": "example", "ok": True, "time": "2026-01-01"}], True)
    assert "signatures have NOT been verified" in email
    assert "trusted timestamps" not in email.lower()
