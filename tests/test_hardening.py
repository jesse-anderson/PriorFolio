"""Release hardening: input-validation tables, CLI contracts, backup portability
and packaging consistency."""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import priorfolio as core
import priorfolio_exports as exports
from conftest import write
from scripts import check_dist
from test_core import cli

REPO = Path(__file__).resolve().parent.parent


def recorded_run(tmp_path, name="example", disclosure=None, files=None):
    project = tmp_path / "projects" / name
    write(project, files or {"design.txt": "Example design.\n"})
    chosen = core.scan_roots([], [project])
    if disclosure is not None:
        chosen[0]["disclosure"] = disclosure
    return core.generate(chosen, tmp_path / "runs", snapshot=True, log=lambda _: None)


# ------------------------------------------------------------ release metadata

def test_version_is_identical_everywhere_it_is_written():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    (declared,) = re.findall(r'(?m)^version = "([^"]+)"$', pyproject)
    assert core.VERSION == declared
    gui_source = (REPO / "priorfolio_gui.py").read_text(encoding="utf-8")
    assert f"PriorFolio {declared} - Copyright" in gui_source
    assert f'"priorfolio=={declared}"' in (REPO / "README.md").read_text(encoding="utf-8")
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"(?m)^## (\S+)", changelog).group(1) == declared


def test_classifiers_are_accepted_by_pypi():
    """PyPI rejects an upload with an unknown classifier; twine check does not."""
    known = pytest.importorskip("trove_classifiers").classifiers
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"(?s)^classifiers = \[(.*?)\]", pyproject, re.M).group(1)
    declared = re.findall(r'"([^"]+)"', block)
    assert declared and set(declared) <= known, set(declared) - known
    minimum = re.search(r'requires-python = ">=3\.(\d+)"', pyproject).group(1)
    assert f"Programming Language :: Python :: 3.{minimum}" in declared


def test_sdist_allowlist_names_every_shipped_source_file():
    """A new test, script or workflow must also be added to check_dist.py."""
    shipped = set()
    for pattern in ("tests/*.py", "scripts/*.py", "desktop/*.py", "desktop/licenses/*.terms",
                    ".github/workflows/*.yml", "docs/*.png"):
        shipped.update(p.relative_to(REPO).as_posix() for p in REPO.glob(pattern))
    assert shipped - check_dist.EXTRA_FILES == set()
    assert {p.name for p in REPO.glob("priorfolio*.py")} == check_dist.MODULES


def test_module_execution_reports_the_version():
    done = subprocess.run([sys.executable, "-m", "priorfolio", "--version"], cwd=REPO,
                          capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == f"PriorFolio {core.VERSION}"


def test_gui_without_tkinter_gives_a_plain_message():
    code = ("import sys; sys.modules['tkinter'] = None; sys.argv = ['priorfolio', '--gui']; "
            "import priorfolio; priorfolio.main()")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert done.returncode == 1
    assert "Tkinter" in done.stderr and "Traceback" not in done.stderr


# ------------------------------------------------------------ untrusted input

@pytest.mark.parametrize("url, expected", [
    ("https://user:secret@github.com/o/r.git", "https://github.com/o/r.git"),
    ("https://token@github.com/o/r.git?x=1#frag", "https://github.com/o/r.git"),
    ("ssh://git@example.com/o/r.git", "ssh://git@example.com/o/r.git"),
    ("ssh://alice:pw@example.com/o/r.git", "ssh://example.com/o/r.git"),
    ("ssh://alice@example.com:2222/o/r.git", "ssh://example.com:2222/o/r.git"),
    ("alice@example.com:o/r.git", "git@example.com:o/r.git"),
    ("git@example.com:o/r.git?token=abc", "git@example.com:o/r.git"),
    ("../relative/remote", "../relative/remote"),
    ("https://[::1/broken", "[invalid remote URL omitted]"),
])
def test_remote_identifiers_never_keep_credentials(url, expected):
    assert core.strip_credentials(url) == expected


@pytest.mark.parametrize("relative", [
    "", "/etc/passwd", "\\windows", "C:\\x", "c:relative", "//server/share/x",
    "a/../../b", "..", "a\\..\\b", "nul\0byte", None, 5,
])
def test_unsafe_evidence_paths_are_rejected(tmp_path, relative):
    with pytest.raises(ValueError):
        core.safe_child(tmp_path, relative)


def test_ordinary_nested_evidence_paths_are_accepted(tmp_path):
    assert core.safe_child(tmp_path, "a/b c/d.txt") == tmp_path.resolve() / "a" / "b c" / "d.txt"


@pytest.mark.parametrize("text, expected", [
    ("2", ["b"]), (" 1 , 3 ", ["a", "c"]), ("3,1,3", ["c", "a"]), ("1-3", ["a", "b", "c"]),
    ("2-2", ["b"]), ("*", ["a", "b", "c"]), ("ALL", ["a", "b", "c"]), (",,", []), ("B", ["b"]),
])
def test_selection_accepts(text, expected):
    projects = [{"name": n} for n in "abc"]
    assert [p["name"] for p in core.parse_selection(text, projects)] == expected


@pytest.mark.parametrize("text", ["0", "4", "3-1", "1-4", "-1", "1-", "x"])
def test_selection_rejects(text):
    with pytest.raises(ValueError):
        core.parse_selection(text, [{"name": n} for n in "abc"])


def test_file_stems_are_always_usable_file_names():
    stems = core.file_stems(["...", "a:b", "a_b", "x/y", "x__y", "trailing. ", "CON"])
    assert stems[0] == "project"
    assert all(not re.search(r'[<>:"/\\|?*]', s) and not s.endswith((".", " ")) for s in stems)
    assert len({s.lower() for s in stems}) == len(stems)


def test_disclosure_text_cannot_inject_html_or_links(tmp_path):
    hostile = "<script>alert(1)</script> [click](javascript:alert(1)) **bold** `code` # head"
    run = recorded_run(tmp_path, disclosure={"title": "# Title <b>", "description": hostile,
                                             "contribution": 7, "period": None})
    text = (run["out_dir"] / "disclosure.md").read_text(encoding="utf-8")
    assert "<script" not in text and "<b>" not in text
    assert not re.search(r"(?<!\\)\]\(", text), "user text must not form a Markdown link"
    assert "\\*\\*bold\\*\\*" in text and "\\`code\\`" in text
    data = json.loads((run["out_dir"] / "disclosure.json").read_text(encoding="utf-8"))
    entry = data["projects"][0]
    assert entry["description"] == hostile  # JSON keeps the exact text
    assert entry["contribution"] == "" and entry["period"] == ""  # non-text values dropped


def test_disclosure_falls_back_to_the_project_name_without_a_title(tmp_path):
    run = recorded_run(tmp_path, name="untitled-work")
    data = json.loads((run["out_dir"] / "disclosure.json").read_text(encoding="utf-8"))
    assert data["projects"][0]["title"] == "untitled-work"
    assert "[Not provided]" in (run["out_dir"] / "disclosure.md").read_text(encoding="utf-8")


# ------------------------------------------------------------ backup receipts

@pytest.fixture
def exported(tmp_path):
    run = recorded_run(tmp_path)
    status = exports.export_backup(run["out_dir"], tmp_path / "backup.zip")
    return run["out_dir"], status


@pytest.mark.parametrize("damage", [
    lambda s: s.update(state="delivered"),
    lambda s: s.update(archive_sha256="not-a-hash"),
    lambda s: s.update(archive_sha256="A" * 64),
    lambda s: s.update(bundle_sha256="0" * 64),
    lambda s: s.pop("exported_utc"),
    lambda s: s.update(archive=""),
    lambda s: s.update(state="confirmed_by_user"),
    lambda s: s.update(state="confirmed_by_user", method="carrier pigeon", confirmed_utc="now"),
    lambda s: s.update(state="confirmed_by_user", method="email", confirmed_utc=""),
])
def test_malformed_backup_receipts_mean_backup_required(exported, damage):
    run, status = exported
    status = dict(status)
    damage(status)
    (run / "backup_status.json").write_text(json.dumps(status), encoding="utf-8")
    assert exports.backup_status(run) == {"state": "backup_required"}


@pytest.mark.parametrize("content", ["[]", "null", "{", "\"text\""])
def test_unreadable_backup_receipts_mean_backup_required(exported, content):
    run, _ = exported
    (run / "backup_status.json").write_text(content, encoding="utf-8")
    assert exports.backup_status(run) == {"state": "backup_required"}


def test_confirmation_rejects_unknown_methods(exported):
    run, _ = exported
    with pytest.raises(core.ToolError):
        exports.confirm_backup(run, "fax")
    assert exports.backup_status(run)["state"] == "exported"


def test_backup_destination_inside_the_run_is_refused(tmp_path):
    run = recorded_run(tmp_path)["out_dir"]
    with pytest.raises(core.ToolError, match="outside this run folder"):
        exports.export_backup(run, run / "snapshots" / "backup.zip")
    assert not (run / "snapshots" / "backup.zip").exists()
    assert exports.backup_status(run) == {"state": "backup_required"}


def test_backup_with_non_ascii_names_restores_cleanly(tmp_path):
    run = recorded_run(tmp_path, name="café-日本", files={"naïve.txt": "x\n"})
    backup = tmp_path / "backup.zip"
    exports.export_backup(run["out_dir"], backup)
    restored = tmp_path / "restored"
    with zipfile.ZipFile(backup) as archive:
        assert all(info.flag_bits & 0x800 for info in archive.infolist() if not info.filename.isascii())
        archive.extractall(restored)
    assert core.verify_run(restored, expect_bundle=run["bundle"]) == []
    if shutil.which("unzip"):
        tested = subprocess.run(["unzip", "-tq", str(backup)], capture_output=True, text=True,
                                errors="replace")
        assert tested.returncode == 0, tested.stdout + tested.stderr


def local_extra_ids(path, info):
    """Extra-field header ids in one entry's local file header."""
    with open(path, "rb") as stream:
        stream.seek(info.header_offset)
        header = stream.read(30)
        name_length, extra_length = struct.unpack("<HH", header[26:30])
        stream.seek(name_length, 1)
        extra = stream.read(extra_length)
    ids = []
    while len(extra) >= 4:
        kind, size = struct.unpack("<HH", extra[:4])
        ids.append(kind)
        extra = extra[4 + size:]
    return ids


def test_backup_archive_only_uses_zip64_where_needed(tmp_path):
    run = recorded_run(tmp_path)
    backup = tmp_path / "backup.zip"
    exports.export_backup(run["out_dir"], backup)
    with zipfile.ZipFile(backup) as archive:
        # A local zip64 field (id 0x0001) on a small entry makes Info-ZIP warn.
        assert not any(0x0001 in local_extra_ids(backup, info) for info in archive.infolist())


# ------------------------------------------------------------ command line contract

@pytest.mark.parametrize("args, message", [
    (["--expect", "0" * 64], "require --verify"),
    (["--against-disk"], "require --verify"),
    (["--metadata-only"], "require --verify"),
    (["--backup", "x"], "must be used together"),
    (["--destination", "x.zip"], "must be used together"),
    (["--confirm-backup", "x"], "must be used together"),
    (["--backup-method", "email"], "must be used together"),
    (["--confirm-backup", "x", "--backup-method", "fax"], "invalid choice"),
    (["--gui", "--verify", "x"], "not allowed with"),
])
def test_cli_rejects_incomplete_option_combinations(args, message):
    done = cli(*args)
    assert done.returncode == 2 and message in done.stderr


def test_cli_confirmation_before_export_is_refused(tmp_path):
    run = recorded_run(tmp_path)["out_dir"]
    done = cli("--confirm-backup", run, "--backup-method", "email")
    assert done.returncode == 1 and "Export a complete backup" in done.stderr
    assert not (run / "backup_status.json").exists()


def test_cli_backup_refuses_to_overwrite(tmp_path):
    run = recorded_run(tmp_path)["out_dir"]
    existing = tmp_path / "taken.zip"
    existing.write_bytes(b"keep me")
    done = cli("--backup", run, "--destination", existing)
    assert done.returncode == 1 and "already exists" in done.stderr
    assert existing.read_bytes() == b"keep me"


def test_cli_verify_accepts_an_expected_hash_in_any_case_with_whitespace(tmp_path):
    run = recorded_run(tmp_path)
    done = cli("--verify", run["out_dir"], "--expect", f"  {run['bundle'].upper()}  ")
    assert done.returncode == 0, done.stdout
    wrong = cli("--verify", run["out_dir"], "--expect", "0" * 64)
    assert wrong.returncode == 1 and "not the expected" in wrong.stdout


def test_cli_hash_only_run_cannot_be_exported_as_a_complete_backup(tmp_path):
    project = tmp_path / "p"
    write(project, {"a.txt": "a\n"})
    made = cli("--add", project, "--select", "all", "--no-snapshot", "--out", tmp_path / "runs")
    assert made.returncode == 0, made.stderr
    (run,) = (tmp_path / "runs").iterdir()
    assert not (run / "snapshots").exists()
    done = cli("--backup", run, "--destination", tmp_path / "b.zip")
    assert done.returncode == 1 and "no complete source snapshots" in done.stderr
    assert not (tmp_path / "b.zip").exists()
