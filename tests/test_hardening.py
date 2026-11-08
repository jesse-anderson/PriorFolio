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
    released = [v for v in re.findall(r"(?m)^## (\S+)", changelog) if v != "Unreleased"]
    assert released[0] == declared


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
                    ".github/workflows/*.yml", "docs/*.png", "tests/data/*"):
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
    (["--withhold-secrets"], "require --backup"),
    (["--withhold", "*.pem"], "require --backup"),
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


def test_gui_launcher_without_tkinter_gives_a_plain_message():
    code = "import sys; sys.modules['tkinter'] = None; import priorfolio; priorfolio.gui_main()"
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert done.returncode == 1
    assert "Tkinter" in done.stderr and "Traceback" not in done.stderr


def test_desktop_checksum_sidecars_use_lf_line_endings(tmp_path):
    pytest.importorskip("tkinter")  # imported by the build script
    from scripts.build_desktop import write_sidecar
    archive = tmp_path / "PriorFolio-test-windows-x64.zip"
    archive.write_bytes(b"archive bytes")
    content = write_sidecar(archive).read_bytes()
    assert b"\r" not in content and content.endswith(b"  PriorFolio-test-windows-x64.zip\n")
    if shutil.which("sha256sum"):
        checked = subprocess.run(["sha256sum", "-c", archive.name + ".sha256"], cwd=tmp_path,
                                 capture_output=True, text=True)
        assert checked.returncode == 0, checked.stdout + checked.stderr


# ------------------------------------------------------------ signed timestamps

DATA = REPO / "tests" / "data"
TSA_NAMES = [name for name, _ in core.TSAS]


def recorded_token(name):
    return (DATA / f"{name}.tsr").read_bytes()


def fixture_digest():
    return bytes.fromhex(core.sha256_file(DATA / "evidence.json"))


def needs_openssl():
    if not core.find_openssl():
        pytest.skip("OpenSSL 3 with CMS support is not installed")


@pytest.mark.parametrize("name", TSA_NAMES)
def test_recorded_tokens_verify_against_the_pinned_roots(name):
    needs_openssl()
    state, when, _ = core.check_timestamp_signature(recorded_token(name), fixture_digest())
    assert state == "verified"
    assert when == core.timestamp_time(recorded_token(name))


@pytest.mark.parametrize("name", TSA_NAMES)
def test_token_for_other_evidence_is_rejected(name):
    needs_openssl()
    state, _, detail = core.check_timestamp_signature(recorded_token(name), b"\x00" * 32)
    assert state == "failed" and "different evidence.json" in detail


@pytest.mark.parametrize("name", TSA_NAMES)
def test_altered_signed_content_is_rejected(name):
    needs_openssl()
    token = bytearray(recorded_token(name))
    _, tst = core.timestamp_token(bytes(token))
    inside = token.find(tst) + len(tst) - 3  # inside the signed TSTInfo, after the hash
    token[inside] ^= 0x01
    state, _, detail = core.check_timestamp_signature(bytes(token), fixture_digest())
    assert state == "failed" and "does not verify" in detail


@pytest.mark.parametrize("name", TSA_NAMES)
def test_altered_signature_is_rejected(name):
    needs_openssl()
    token = bytearray(recorded_token(name))
    token[-5] ^= 0x01  # the signer's signature value ends the token
    state, _, _ = core.check_timestamp_signature(bytes(token), fixture_digest())
    assert state == "failed"


def test_only_pinned_roots_are_trusted(monkeypatch):
    needs_openssl()
    freetsa_only = "-----BEGIN CERTIFICATE-----" + core.TSA_ROOTS.split("-----BEGIN CERTIFICATE-----")[3]
    monkeypatch.setattr(core, "TSA_ROOTS", freetsa_only)
    assert core.check_timestamp_signature(recorded_token("freetsa"), fixture_digest())[0] == "verified"
    for name in ("digicert", "sectigo"):
        state, _, detail = core.check_timestamp_signature(recorded_token(name), fixture_digest())
        assert state == "failed" and "does not verify" in detail


def test_pinned_roots_match_their_documented_fingerprints():
    import base64
    import hashlib
    blocks = core.TSA_ROOTS.split("-----BEGIN CERTIFICATE-----")[1:]
    prints = {hashlib.sha256(base64.b64decode("".join(b.split("-----END")[0].split()))).hexdigest()
              for b in blocks}
    comment = (REPO / "priorfolio.py").read_text(encoding="utf-8").split("TSA_ROOTS = ")[0]
    assert len(prints) == 3 and all(p in comment for p in prints)


def test_without_openssl_tokens_are_kept_but_marked_unchecked(monkeypatch):
    monkeypatch.setattr(core, "find_openssl", lambda: None)
    state, when, detail = core.check_timestamp_signature(recorded_token("digicert"), fixture_digest())
    assert state == "unchecked" and when is None and "not checked" in detail


@pytest.mark.parametrize("garbage", [b"", b"\x30\x03\x02\x01\x00", b"\x30\x80" + b"\x00" * 20,
                                     bytes(range(256))])
def test_malformed_tokens_fail_without_running_openssl(monkeypatch, garbage):
    monkeypatch.setattr(core, "find_openssl", lambda: "openssl-is-not-run")
    state, _, detail = core.check_timestamp_signature(garbage, fixture_digest())
    assert state == "failed" and "malformed" in detail


def recorded_run_folder(tmp_path, names=TSA_NAMES):
    run = tmp_path / "run"
    (run / "timestamps").mkdir(parents=True)
    shutil.copyfile(DATA / "evidence.json", run / "evidence.json")
    (run / "BUNDLE_SHA256.txt").write_text(core.sha256_file(run / "evidence.json") + "  evidence.json\n")
    for name in names:
        shutil.copyfile(DATA / f"{name}.tsr", run / "timestamps" / f"{name}.tsr")
    return run


def test_verify_round_trips_real_signed_tokens(tmp_path):
    needs_openssl()
    run = recorded_run_folder(tmp_path)
    assert core.verify_run(run) == []
    summary = core.run_summary(run, signatures=True)
    assert [(name, state) for name, _, state in summary["timestamps"]] == \
        [(name, "verified") for name in sorted(TSA_NAMES)]
    done = cli("--verify", run, "--expect", summary["bundle"])
    assert done.returncode == 0, done.stdout
    assert done.stdout.count("(signature verified)") == 3


def test_verify_reports_a_token_whose_signature_was_broken(tmp_path):
    needs_openssl()
    run = recorded_run_folder(tmp_path, ["sectigo"])
    token = bytearray((run / "timestamps" / "sectigo.tsr").read_bytes())
    token[-5] ^= 0x01
    (run / "timestamps" / "sectigo.tsr").write_bytes(bytes(token))
    problems = core.verify_run(run)
    assert len(problems) == 1 and "sectigo.tsr" in problems[0]
    assert core.run_summary(run, signatures=True)["timestamps"][0][2] == "failed"
    done = cli("--verify", run)
    assert done.returncode == 1 and "SIGNATURE DOES NOT VERIFY" in done.stdout


def test_summary_without_signature_checks_does_not_claim_any(tmp_path):
    run = recorded_run_folder(tmp_path, ["freetsa"])
    ((name, when, state),) = core.run_summary(run)["timestamps"]
    assert name == "freetsa" and when and state is None


def test_timestamps_are_on_by_default_and_can_be_turned_off():
    assert core.build_parser().parse_args([]).timestamp is True
    assert core.build_parser().parse_args(["--no-timestamp"]).timestamp is False
    assert "IP address" in core.TIMESTAMP_NOTICE


def test_cli_announces_what_timestamps_send_and_offline_runs_stay_quiet(tmp_path):
    project = tmp_path / "p"
    write(project, {"a.txt": "a\n"})
    base = [sys.executable, str(REPO / "priorfolio.py"), "--add", str(project), "--select", "all"]
    online = subprocess.run(base + ["--out", str(tmp_path / "on")], capture_output=True, text=True)
    assert online.returncode == 0, online.stderr  # services are unreachable under test
    assert core.TIMESTAMP_NOTICE in online.stderr and "Timestamp digicert: FAILED" in online.stdout
    offline = subprocess.run(base + ["--no-timestamp", "--out", str(tmp_path / "off")],
                             capture_output=True, text=True)
    assert offline.returncode == 0 and "IP address" not in offline.stderr
    assert "Self-check: the new run was read back and verifies." in offline.stdout


# ------------------------------------------------------------ run self-check

def test_a_run_that_does_not_read_back_is_never_left_looking_finished(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "verify_run", lambda run_dir, *a, **k: ["snapshot zip is unreadable"])
    with pytest.raises(core.ToolError, match="did not pass its own verification"):
        recorded_run(tmp_path)
    (folder,) = (tmp_path / "runs").iterdir()
    assert folder.name.endswith("-INCOMPLETE")


# ------------------------------------------------------------ repository config

def evil_repo(tmp_path, setting):
    from conftest import git
    repo = tmp_path / "projects" / "evil"
    write(repo, {"a.txt": "data\n", ".gitattributes": "* filter=probe\n"})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    marker = tmp_path / "MARKER"
    command = "echo ran >> '" + marker.as_posix() + "'; cat"
    if setting == "filter":
        git(repo, "config", "--local", "filter.probe.clean", command)
    else:
        included = tmp_path / "included.config"
        included.write_text('[filter "probe"]\n\tclean = "' + command + '"\n')
        git(repo, "config", "--local", "include.path", included.as_posix())
    os.utime(repo / "a.txt", (0, 0))  # stat-dirty, so git status would run the filter
    return repo, marker


@pytest.mark.parametrize("setting", ["filter", "include"])
def test_scanning_never_runs_programs_named_in_repo_config(tmp_path, setting):
    repo, marker = evil_repo(tmp_path, setting)
    (info,) = core.scan_roots([repo.parent])
    assert not marker.exists(), "git status ran a repository-configured program"
    assert info["config_warning"] == ("filter.probe.clean" if setting == "filter" else "include.path")
    assert "git status skipped" in info["state"] and info["commit"]
    run = core.generate([info], tmp_path / "runs", snapshot=True, log=lambda _: None)
    assert not marker.exists()
    (entry,) = run["results"]
    assert entry["file_count"] == 2 and not entry["warnings"]
    assert "repository config can run programs" in entry["notes"][0]


def test_ordinary_repo_settings_do_not_trigger_the_config_warning(tmp_path):
    from conftest import git
    repo = tmp_path / "projects" / "husky"
    write(repo, {"a.txt": "a\n"})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "--local", "core.hooksPath", ".husky")
    git(repo, "config", "--local", "core.editor", "vim")
    (info,) = core.scan_roots([repo.parent])
    assert info["config_warning"] is None and info["untracked"] == 1


# ------------------------------------------------------------ secrets in backups

@pytest.mark.parametrize("path, expected", [
    (".env", True), ("app/.env.local", True), (".env.example", False), ("config/.env.sample", False),
    ("certs/server.pem", True), ("keys/deploy.key", True), ("store.p12", True), ("id_ed25519", True),
    ("home/.ssh/id_rsa", True), ("credentials.json", True), (".npmrc", True), ("prod.tfvars", True),
    ("README.md", False), ("src/keyboard.py", False), ("environment.yml", False), ("id_rsa.pub", False),
])
def test_secret_file_names(path, expected):
    assert exports.looks_secret(path) is expected


def test_backup_lists_likely_secrets_and_cli_warns_before_exporting(tmp_path):
    run = recorded_run(tmp_path, files={".env": "TOKEN=x\n", ".env.example": "TOKEN=\n",
                                        "deploy.pem": "key\n", "main.py": "print()\n"})["out_dir"]
    assert exports.likely_secrets(run) == ["example: .env", "example: deploy.pem"]
    done = cli("--backup", run, "--destination", tmp_path / "b.zip")
    assert done.returncode == 0, done.stderr
    assert "WARNING: 2 file(s) that may hold secrets" in done.stderr and "example: .env" in done.stderr
    assert (tmp_path / "b.zip").exists()


def test_damaged_run_lists_no_secrets_instead_of_crashing(tmp_path):
    assert exports.likely_secrets(tmp_path / "missing") == []


# ------------------------------------------------------------ full and cloud copies

SECRET_FILES = {".env": "TOKEN=private-value\n", "keys/deploy.pem": "PRIVATE KEY BYTES\n",
                "main.py": "print('hi')\n", "docs/readme.txt": "docs\n"}
SECRETS = ["example: .env", "example: keys/deploy.pem"]


@pytest.fixture
def secret_run(tmp_path):
    return recorded_run(tmp_path, files=SECRET_FILES)


def unpack(archive, folder):
    with zipfile.ZipFile(archive) as z:
        z.extractall(folder)
    return folder


def snapshot_path(folder):
    data = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    return folder / data["projects"][0]["snapshot"]


def snapshot_members(folder):
    with zipfile.ZipFile(snapshot_path(folder)) as z:
        return {name: z.read(name) for name in z.namelist()}


def rewrite_snapshot(folder, change):
    members = snapshot_members(folder)
    change(members)
    with zipfile.ZipFile(snapshot_path(folder), "w") as z:
        for name, content in members.items():
            z.writestr(name, content)


def edit_withheld(folder, change):
    path = folder / "WITHHELD.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_files_to_withhold_are_chosen_by_name_or_pattern(secret_run):
    run = secret_run["out_dir"]
    assert exports.select_withheld(run, secrets=True) == SECRETS
    assert exports.select_withheld(run, ["*.txt"]) == ["example: docs/readme.txt"]
    assert exports.select_withheld(run, ["keys/*", ".env"]) == SECRETS
    assert exports.select_withheld(run, ["nothing-matches"]) == []


@pytest.fixture
def copies(secret_run, tmp_path):
    run = secret_run["out_dir"]
    exports.export_backup(run, tmp_path / "full.zip")
    status = exports.export_backup(run, tmp_path / "cloud.zip", SECRETS)
    return {"run": run, "bundle": secret_run["bundle"], "status": status,
            "full": unpack(tmp_path / "full.zip", tmp_path / "full"),
            "cloud": unpack(tmp_path / "cloud.zip", tmp_path / "cloud")}


def test_cloud_copy_leaves_out_secrets_and_verifies_everything_else(copies):
    cloud, full = copies["cloud"], copies["full"]
    assert sorted(snapshot_members(cloud)) == ["docs/readme.txt", "main.py"]
    for path in cloud.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert b"private-value" not in data and b"PRIVATE KEY BYTES" not in data, path
    assert core.verify_run(cloud, expect_bundle=copies["bundle"]) == []
    assert [f["path"] for f in exports.withheld_files(cloud)] == [".env", "keys/deploy.pem"]
    assert "example: keys/deploy.pem  sha256 " in (cloud / "WITHHELD.txt").read_text(encoding="utf-8")
    # The full copy is complete, and both share one timestamped record.
    assert sorted(snapshot_members(full)) == [".env", "docs/readme.txt", "keys/deploy.pem", "main.py"]
    assert core.verify_run(full, expect_bundle=copies["bundle"]) == []
    assert exports.withheld_files(full) == []
    for name in ("evidence.json", "BUNDLE_SHA256.txt"):
        assert (cloud / name).read_bytes() == (full / name).read_bytes()


def test_receipt_records_both_copies_through_confirmation(copies, tmp_path):
    status = exports.backup_status(copies["run"])
    assert status["withheld"] == SECRETS and status["archive"] == str(tmp_path / "cloud.zip")
    assert status["full_copy"]["archive"] == str(tmp_path / "full.zip")
    exports.confirm_backup(copies["run"], "cloud")
    confirmed = exports.backup_status(copies["run"])
    assert confirmed["state"] == "confirmed_by_user" and confirmed["full_copy"] == status["full_copy"]


def test_cloud_copy_alone_has_no_full_copy_in_its_receipt(secret_run, tmp_path):
    status = exports.export_backup(secret_run["out_dir"], tmp_path / "cloud.zip", SECRETS)
    assert status["withheld"] == SECRETS and "full_copy" not in status


@pytest.mark.parametrize("tamper, expected", [
    (lambda c: rewrite_snapshot(c, lambda m: m.pop("main.py")), "snapshot missing: main.py"),
    (lambda c: rewrite_snapshot(c, lambda m: m.update({".env": b"TOKEN=private-value\n"})),
     "snapshot not in manifest: .env"),
    (lambda c: rewrite_snapshot(c, lambda m: m.update({"main.py": b"changed"})), "snapshot changed: main.py"),
    (lambda c: edit_withheld(c, lambda d: d["files"][0].update(sha256="0" * 64)),
     "withheld file .env does not match the manifest"),
    (lambda c: edit_withheld(c, lambda d: d["files"][0].update(path="ghost.txt")),
     "withheld file ghost.txt does not match the manifest"),
    (lambda c: edit_withheld(c, lambda d: d.update(bundle_sha256="0" * 64)), "different evidence.json"),
    (lambda c: edit_withheld(c, lambda d: d["files"][0].update(manifest="manifests/other.sha256")),
     "manifest outside this run"),
    (lambda c: (c / "WITHHELD.json").write_text("{", encoding="utf-8"), "WITHHELD.json is unreadable"),
    (lambda c: (c / "WITHHELD.json").write_text('{"format": "x"}', encoding="utf-8"),
     "WITHHELD.json is unreadable"),
])
def test_cloud_copy_tampering_is_reported(copies, tamper, expected):
    tamper(copies["cloud"])
    problems = core.verify_run(copies["cloud"])
    assert any(expected in p for p in problems), problems


def test_a_cloud_copy_cannot_be_exported_again(copies, tmp_path):
    with pytest.raises(core.ToolError, match="cloud copy"):
        exports.export_backup(copies["cloud"], tmp_path / "again.zip")
    assert not (tmp_path / "again.zip").exists()


def test_withholding_a_file_that_is_not_recorded_is_refused(secret_run, tmp_path):
    for item in ("example: missing.txt", "other: .env", "no separator"):
        with pytest.raises(core.ToolError, match="not recorded"):
            exports.export_backup(secret_run["out_dir"], tmp_path / "c.zip", [item])
        assert not (tmp_path / "c.zip").exists()


@pytest.mark.parametrize("damage", [
    lambda s: s.update(withheld=[]),
    lambda s: s.update(withheld=[3]),
    lambda s: s.update(withheld="example: .env"),
    lambda s: s.update(full_copy="full.zip"),
    lambda s: s.update(full_copy={"archive": "full.zip", "archive_sha256": "bad", "exported_utc": "now"}),
    lambda s: s.update(full_copy={"archive": "", "archive_sha256": "0" * 64, "exported_utc": "now"}),
])
def test_malformed_cloud_receipts_mean_backup_required(copies, damage):
    status = dict(exports.backup_status(copies["run"]))
    damage(status)
    (copies["run"] / "backup_status.json").write_text(json.dumps(status), encoding="utf-8")
    assert exports.backup_status(copies["run"]) == {"state": "backup_required"}


def test_cli_exports_a_cloud_copy_and_verify_names_what_it_withholds(secret_run, tmp_path):
    run = secret_run["out_dir"]
    done = cli("--backup", run, "--destination", tmp_path / "cloud.zip", "--withhold-secrets",
               "--withhold", "docs/*")
    assert done.returncode == 0, done.stderr
    assert "Cloud copy: withholding 3 file(s)" in done.stderr and "7-Zip" in done.stdout
    cloud = unpack(tmp_path / "cloud.zip", tmp_path / "cloud")
    checked = cli("--verify", cloud, "--expect", secret_run["bundle"])
    assert checked.returncode == 0, checked.stdout
    assert "OK: evidence hashes except 3 withheld file(s) match." in checked.stdout
    assert "This is a cloud copy" in checked.stdout and "example: keys/deploy.pem" in checked.stdout


def test_cli_full_export_points_to_the_cloud_copy_option(secret_run, tmp_path):
    done = cli("--backup", secret_run["out_dir"], "--destination", tmp_path / "full.zip")
    assert done.returncode == 0 and "--withhold-secrets" in done.stderr


def test_cli_refuses_a_withhold_pattern_that_matches_nothing(secret_run, tmp_path):
    done = cli("--backup", secret_run["out_dir"], "--destination", tmp_path / "c.zip", "--withhold", "*.none")
    assert done.returncode == 1 and "No recorded files match" in done.stderr
    assert not (tmp_path / "c.zip").exists()


def test_instructions_say_to_password_protect_with_7zip(tmp_path):
    run = recorded_run(tmp_path)
    for text in ((run["out_dir"] / "BACKUP.txt").read_text(encoding="utf-8"), run["email"]):
        assert "7z" in text
    assert "-mhe=on" in (run["out_dir"] / "BACKUP.txt").read_text(encoding="utf-8")
