import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import priorfolio as sa
from conftest import git, write


def run(root, tmp_path, names=None, **options):
    """Scan `root`, generate for the named projects (default all), return the run."""
    projects = sa.scan_roots([root])
    if names is not None:
        projects = [p for p in projects if p["name"] in names]
        assert {p["name"] for p in projects} == set(names)
    return sa.generate(projects, tmp_path / "out", log=lambda m: None, **options)


def by_name(result):
    return {r["name"]: r for r in result["results"]}


def manifest_paths(result, name):
    raw = (result["out_dir"] / by_name(result)[name]["manifest"]).read_bytes()
    return list(sa.read_manifest(raw))


def tree_state(folder):
    """Every path under a folder with its size and mtime, .git included."""
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- round trip

def test_snapshot_round_trip_reproduces_every_content_hash(workspace, tmp_path):
    """Hash -> zip -> extract somewhere else -> rebuild the manifest by hand."""
    result = run(workspace, tmp_path, snapshot=True)
    restored = tmp_path / "restored"

    for r in result["results"]:
        if not r["file_count"]:
            continue
        target = restored / r["name"]
        with zipfile.ZipFile(result["out_dir"] / r["snapshot"]) as z:
            z.extractall(target)
        files = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
        rebuilt = "".join(
            f"{hashlib.sha256((target / rel).read_bytes()).hexdigest()}  {rel}\n" for rel in files
        ).encode("utf-8")

        assert hashlib.sha256(rebuilt).hexdigest() == r["content_sha256"], r["name"]
        assert rebuilt == (result["out_dir"] / r["manifest"]).read_bytes(), r["name"]


def test_restored_snapshot_hashes_the_same_as_the_original(workspace, tmp_path):
    """Feeding an extracted snapshot back through the tool gives the same hash."""
    first = run(workspace, tmp_path, names=["clean"], snapshot=True)
    original = by_name(first)["clean"]

    restored_root = tmp_path / "restored"
    with zipfile.ZipFile(first["out_dir"] / original["snapshot"]) as z:
        z.extractall(restored_root / "clean")

    second = sa.generate(sa.scan_roots([restored_root]), tmp_path / "out2", log=lambda m: None)
    assert by_name(second)["clean"]["content_sha256"] == original["content_sha256"]


def test_same_tree_hashes_identically_on_repeat_runs(workspace, tmp_path):
    first = run(workspace, tmp_path)
    second = run(workspace, tmp_path)
    assert first["out_dir"] != second["out_dir"]
    assert ({n: r["content_sha256"] for n, r in by_name(first).items()}
            == {n: r["content_sha256"] for n, r in by_name(second).items()})


def test_same_files_in_a_different_location_hash_identically(workspace, tmp_path):
    first = by_name(run(workspace, tmp_path, names=["plain"]))["plain"]
    moved_root = tmp_path / "elsewhere"
    shutil.copytree(workspace / "plain", moved_root / "plain")
    second = sa.generate(sa.scan_roots([moved_root]), tmp_path / "out2", log=lambda m: None)
    assert by_name(second)["plain"]["content_sha256"] == first["content_sha256"]


def test_fresh_run_verifies_clean(workspace, tmp_path):
    result = run(workspace, tmp_path, snapshot=True)
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_json_on_disk_matches_what_generate_returned(workspace, tmp_path):
    result = run(workspace, tmp_path, snapshot=True)
    saved = json.loads((result["out_dir"] / "evidence.json").read_text(encoding="utf-8"))
    assert saved["projects"] == result["results"]
    assert (result["out_dir"] / "BUNDLE_SHA256.txt").read_text().split()[0] == result["bundle"]
    assert hashlib.sha256((result["out_dir"] / "evidence.json").read_bytes()).hexdigest() == result["bundle"]


def test_manifest_is_checkable_with_sha256sum(workspace, tmp_path):
    if not shutil.which("sha256sum"):
        pytest.skip("sha256sum not installed")
    result = run(workspace, tmp_path, names=["plain"])
    manifest = result["out_dir"] / by_name(result)["plain"]["manifest"]
    done = subprocess.run(["sha256sum", "-c", str(manifest)], cwd=workspace / "plain",
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stdout + done.stderr


# ------------------------------------------------- verify catches tampering

def edit_json(run_dir, change):
    path = run_dir / "evidence.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def tamper_json(run_dir):
    edit_json(run_dir, lambda d: d["projects"][0].update(commit_date="1999-01-01T00:00:00+00:00"))


def tamper_bundle_file(run_dir):
    (run_dir / "BUNDLE_SHA256.txt").write_text("0" * 64 + "  evidence.json\n")


def tamper_manifest(run_dir):
    path = run_dir / "manifests" / "plain.sha256"
    path.write_bytes(path.read_bytes().replace(b"main.py", b"mine.py"))


def remove_manifest(run_dir):
    (run_dir / "manifests" / "plain.sha256").unlink()


def swap_file_in_snapshot(run_dir):
    path = run_dir / "snapshots" / "plain.zip"
    with zipfile.ZipFile(path) as z:
        members = {i.filename: z.read(i) for i in z.infolist()}
    members["main.py"] = b"print('rewritten later')\n"
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)


def add_file_to_snapshot(run_dir):
    with zipfile.ZipFile(run_dir / "snapshots" / "plain.zip", "a") as z:
        z.writestr("added_later.py", "x = 1\n")


def corrupt_snapshot(run_dir):
    (run_dir / "snapshots" / "plain.zip").write_bytes(b"not a zip")


@pytest.mark.parametrize("tamper", [
    tamper_json, tamper_bundle_file, tamper_manifest, remove_manifest,
    swap_file_in_snapshot, add_file_to_snapshot, corrupt_snapshot,
])
def test_verify_reports_tampering(workspace, tmp_path, tamper):
    result = run(workspace, tmp_path, names=["plain", "clean"], snapshot=True)
    assert sa.verify_run(result["out_dir"]) == []
    tamper(result["out_dir"])
    assert sa.verify_run(result["out_dir"]) != []


def test_rewriting_a_content_hash_in_the_json_is_caught_two_ways(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["plain"])
    edit_json(result["out_dir"], lambda d: d["projects"][0].update(content_sha256="f" * 64))
    problems = sa.verify_run(result["out_dir"])
    assert any("BUNDLE_SHA256" in p for p in problems)
    assert any("manifest does not match" in p for p in problems)


def test_verify_against_disk_names_changed_and_missing_files(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["plain"])
    (workspace / "plain" / "main.py").write_text("print('edited afterwards')\n")
    (workspace / "plain" / "src" / "lib.py").unlink()

    assert sa.verify_run(result["out_dir"]) == []  # the evidence itself is still intact
    problems = sa.verify_run(result["out_dir"], against_disk=True)
    assert "plain: on disk changed: main.py" in problems
    assert "plain: on disk missing: src/lib.py" in problems
    assert len(problems) == 2


def test_verify_on_a_folder_that_is_not_a_run(tmp_path):
    assert len(sa.verify_run(tmp_path)) == 1


# -------------------------------------------------------------- sensitivity

def test_one_changed_byte_changes_the_content_hash(workspace, tmp_path):
    before = by_name(run(workspace, tmp_path, names=["plain"]))["plain"]["content_sha256"]
    blob = workspace / "plain" / "data" / "blob.bin"
    data = bytearray(blob.read_bytes())
    data[500] ^= 1
    blob.write_bytes(bytes(data))
    after = by_name(run(workspace, tmp_path, names=["plain"]))["plain"]["content_sha256"]
    assert before != after


@pytest.mark.parametrize("change", [
    lambda d: (d / "main.py").rename(d / "main2.py"),
    lambda d: (d / "extra.py").write_text("new\n"),
    lambda d: (d / "src" / "lib.py").unlink(),
    lambda d: (d / "src" / "lib.py").rename(d / "lib.py"),
], ids=["rename", "add", "delete", "move"])
def test_structural_changes_change_the_content_hash(workspace, tmp_path, change):
    before = by_name(run(workspace, tmp_path, names=["plain"]))["plain"]["content_sha256"]
    change(workspace / "plain")
    after = by_name(run(workspace, tmp_path, names=["plain"]))["plain"]["content_sha256"]
    assert before != after


@pytest.mark.parametrize("ignored", [
    "debug.log", "other.log", "node_modules/dep/more.js",
    "__pycache__/x.pyc", "myenv/Lib/extra.py", "stray.pyc",
])
def test_ignored_files_do_not_affect_the_content_hash(workspace, tmp_path, ignored):
    before = by_name(run(workspace, tmp_path, names=["plain"]))["plain"]["content_sha256"]
    write(workspace / "plain", {ignored: "changed or new\n"})
    after = by_name(run(workspace, tmp_path, names=["plain"]))["plain"]["content_sha256"]
    assert before == after


def test_uncommitted_edit_changes_content_hash_but_not_commit(workspace, tmp_path):
    before = by_name(run(workspace, tmp_path, names=["clean"]))["clean"]
    (workspace / "clean" / "one.txt").write_text("edited, not committed\n")
    after = by_name(run(workspace, tmp_path, names=["clean"]))["clean"]
    assert after["commit"] == before["commit"]
    assert after["content_sha256"] != before["content_sha256"]
    assert before["state"] == "no remote"
    assert "uncommitted: 1 modified, 0 untracked" in after["state"]


def test_new_commit_of_the_same_files_changes_commit_but_not_content_hash(workspace, tmp_path):
    before = by_name(run(workspace, tmp_path, names=["clean"]))["clean"]
    git(workspace / "clean", "commit", "-q", "--allow-empty", "-m", "nothing changed")
    after = by_name(run(workspace, tmp_path, names=["clean"]))["clean"]
    assert after["commit"] != before["commit"]
    assert after["tree"] == before["tree"]
    assert after["content_sha256"] == before["content_sha256"]


# ---------------------------------------------------------- what gets hashed

def test_plain_folder_hashes_source_and_skips_ignored_dependencies_and_venvs(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["plain"])
    assert manifest_paths(result, "plain") == [
        ".gitignore", "data/blob.bin", "docs/café notes.txt", "main.py", "src/lib.py",
    ]
    assert by_name(result)["plain"]["notes"] == [
        "folder not included: __pycache__/ (1 file)",
        "folder not included: node_modules/ (1 file)",
        "virtualenv not included: myenv/ (2 files)",
    ]
    assert by_name(result)["plain"]["warnings"] == []


def test_repo_hashes_tracked_and_untracked_but_not_ignored(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["repo"])
    assert manifest_paths(result, "repo") == [
        ".gitignore", "README.md", "app.py", "env/settings.py", "notes.txt",
    ]


def test_repo_hashes_the_file_on_disk_not_the_committed_version(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["repo"])
    raw = (result["out_dir"] / by_name(result)["repo"]["manifest"]).read_bytes()
    assert sa.read_manifest(raw)["app.py"] == hashlib.sha256(b"VERSION = 2\n").hexdigest()


def test_file_deleted_from_disk_is_left_out_and_noted(workspace, tmp_path):
    (workspace / "clean" / "one.txt").unlink()
    result = run(workspace, tmp_path, names=["clean"])
    assert manifest_paths(result, "clean") == ["sub/two.txt"]
    assert any("one.txt" in n for n in by_name(result)["clean"]["notes"])


def test_nested_repo_is_its_own_project_and_excluded_from_its_parent(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["group", "group/inner"])
    assert manifest_paths(result, "group") == ["NOTES.md"]
    assert manifest_paths(result, "group/inner") == ["lib.c"]
    assert "nested repo not included: inner/" in by_name(result)["group"]["notes"]
    assert (result["out_dir"] / "manifests" / "group__inner.sha256").is_file()


def test_empty_project_has_no_content_hash(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["empty"], snapshot=True)
    empty = by_name(result)["empty"]
    assert empty["content_sha256"] is None and empty["file_count"] == 0
    assert "snapshot" not in empty
    assert sa.verify_run(result["out_dir"]) == []


def test_manifest_is_sorted_utf8_with_unix_newlines(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["plain"])
    raw = (result["out_dir"] / by_name(result)["plain"]["manifest"]).read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)
    paths = [line.split("  ", 1)[1] for line in lines]
    assert paths == sorted(paths)


def test_totals_and_newest_file_are_recorded(workspace, tmp_path):
    os.utime(workspace / "clean" / "one.txt", (1_700_000_000, 1_700_000_000))
    os.utime(workspace / "clean" / "sub" / "two.txt", (1_600_000_000, 1_600_000_000))
    clean = by_name(run(workspace, tmp_path, names=["clean"]))["clean"]
    assert clean["file_count"] == 2 and clean["total_bytes"] == 4
    assert clean["newest_file_mtime"] == "2023-11-14T22:13:20+00:00"


# --------------------------------------------------------- read-only promise

def test_generate_does_not_touch_the_projects(workspace, tmp_path):
    # This plain `git status` may itself refresh the index, so it runs before the baseline.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=workspace / "repo",
                            capture_output=True, text=True).stdout
    before = tree_state(workspace)
    run(workspace, tmp_path, snapshot=True, timestamp=False)
    assert tree_state(workspace) == before  # every file, .git internals included
    assert subprocess.run(["git", "status", "--porcelain"], cwd=workspace / "repo",
                          capture_output=True, text=True).stdout == status


# ------------------------------------------------------------ project states

def test_states_of_each_kind_of_project(workspace):
    found = {p["name"]: p for p in sa.scan_roots([workspace])}
    assert sorted(found) == ["clean", "empty", "fresh", "group", "group/inner", "plain", "repo"]

    assert found["plain"]["state"] == "NOT A GIT REPO"
    assert found["plain"]["commit"] is None

    assert found["fresh"]["state"] == "NO COMMITS; uncommitted: 0 modified, 1 untracked; no remote"
    assert found["fresh"]["commit"] is None

    assert found["repo"]["state"] == "uncommitted: 1 modified, 1 untracked; no remote"
    assert found["repo"]["branch"] == "main"
    assert len(found["repo"]["commit"]) == 40 and len(found["repo"]["tree"]) == 40
    assert found["repo"]["commit_date"] == "2025-01-02T03:04:05+02:00"


def test_pushed_then_unpushed(workspace, tmp_path):
    clean = workspace / "clean"
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(clean, "remote", "add", "origin", str(remote))
    git(clean, "push", "-q", "-u", "origin", "main")
    assert sa.describe(clean, workspace)["state"] == "clean, pushed"

    git(clean, "commit", "-q", "--allow-empty", "-m", "local only")
    info = sa.describe(clean, workspace)
    assert info["unpushed"] == 1 and info["state"] == "1 unpushed commit"


def test_generate_rereads_state_changed_since_the_scan(workspace, tmp_path):
    projects = [p for p in sa.scan_roots([workspace]) if p["name"] == "clean"]
    old_commit = projects[0]["commit"]
    git(workspace / "clean", "commit", "-q", "--allow-empty", "-m", "after the scan")
    result = sa.generate(projects, tmp_path / "out", log=lambda m: None)
    assert result["results"][0]["commit"] != old_commit


@pytest.mark.parametrize("url, expected", [
    ("https://user:github_pat_SECRET@github.com/u/r.git", "https://github.com/u/r.git"),
    ("https://ghp_SECRET@github.com/u/r.git", "https://github.com/u/r.git"),
    ("https://github.com/u/r.git", "https://github.com/u/r.git"),
    ("git@github.com:u/r.git", "git@github.com:u/r.git"),
    ("ssh://git@github.com/u/r.git", "ssh://git@github.com/u/r.git"),
    ("HTTPS://tok@Example.com/u/r.git", "HTTPS://Example.com/u/r.git"),
    ("https://github.com/u/r@v2.git", "https://github.com/u/r@v2.git"),
])
def test_credentials_are_stripped_from_remote_urls(url, expected):
    assert sa.strip_credentials(url) == expected


def test_token_in_a_remote_never_reaches_any_output(workspace, tmp_path):
    git(workspace / "clean", "remote", "add", "origin",
        "https://someone:github_pat_SECRETTOKEN@github.com/someone/clean.git")
    result = run(workspace, tmp_path, names=["clean"])
    assert by_name(result)["clean"]["remote"] == "https://github.com/someone/clean.git"
    for path in result["out_dir"].rglob("*"):
        if path.is_file() and path.suffix != ".zip":
            assert b"SECRETTOKEN" not in path.read_bytes(), path.name
    with zipfile.ZipFile(result["out_dir"] / "evidence_bundle.zip") as z:
        assert all(b"SECRETTOKEN" not in z.read(n) for n in z.namelist())


# ------------------------------------------------------------------ sources

def test_root_that_is_itself_a_repo_is_one_project(workspace):
    found = sa.scan_roots([workspace / "clean"])
    assert [p["name"] for p in found] == ["clean"]


def test_single_project_can_be_any_folder(workspace):
    found = sa.scan_roots([], singles=[workspace / "plain" / "src"])
    assert [(p["name"], p["state"]) for p in found] == [("src", "NOT A GIT REPO")]


def test_project_reached_twice_is_listed_once(workspace):
    found = sa.scan_roots([workspace, workspace], singles=[workspace / "plain"])
    names = [p["name"] for p in found]
    assert len(names) == len(set(names)) == 7


def test_same_name_under_two_roots_is_told_apart(workspace, tmp_path):
    other = tmp_path / "second"
    write(other / "plain", {"different.py": "y = 2\n"})
    names = sorted(p["name"] for p in sa.scan_roots([workspace, other]))
    assert "projects/plain" in names and "second/plain" in names and "plain" not in names


def test_projects_from_several_roots_end_up_in_one_run(workspace, tmp_path):
    other = tmp_path / "second"
    write(other / "tool", {"t.py": "pass\n"})
    result = sa.generate(sa.scan_roots([workspace, other]), tmp_path / "out", log=lambda m: None)
    assert "tool" in by_name(result) and "plain" in by_name(result)
    saved = json.loads((result["out_dir"] / "evidence.json").read_text(encoding="utf-8"))
    assert saved["roots"] == sorted([str(workspace), str(other)])
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_selection_parsing():
    projects = [{"name": n} for n in ("Alpha", "beta", "gamma", "delta")]
    pick = lambda text: [p["name"] for p in sa.parse_selection(text, projects)]
    assert pick("all") == ["Alpha", "beta", "gamma", "delta"]
    assert pick("2") == ["beta"]
    assert pick("1, 3-4") == ["Alpha", "gamma", "delta"]
    assert pick("alpha,GAMMA") == ["Alpha", "gamma"]
    assert pick("2,1-2,beta") == ["beta", "Alpha"]
    assert pick("") == []
    for bad in ("5", "0", "3-2", "nope"):
        with pytest.raises(ValueError):
            sa.parse_selection(bad, projects)


# ---------------------------------------------------------- written outputs

def test_email_and_schedule_text_carry_the_real_values(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["plain", "clean"], snapshot=True)
    assert result["email"] == (result["out_dir"] / "email.txt").read_text(encoding="utf-8")
    assert result["bundle"] in result["email"]
    assert "To: [your personal address]" in result["email"]
    assert "BACKUP CHECK" in result["email"]
    assert "evidence_bundle.zip" in result["email"]
    for r in result["results"]:
        for text in (result["email"], result["markdown"]):
            assert r["name"] in text and r["content_sha256"] in text
    clean = by_name(result)["clean"]
    assert clean["commit"] in result["email"] and clean["commit"] in result["markdown"]
    assert "none (not under version control" in result["email"]


def test_evidence_bundle_holds_everything_but_the_snapshots(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["plain", "clean"], snapshot=True)
    with zipfile.ZipFile(result["out_dir"] / "evidence_bundle.zip") as z:
        names = sorted(z.namelist())
        packed_json = z.read("evidence.json")
    assert names == sorted(["BUNDLE_SHA256.txt", "manifests/clean.sha256", "manifests/plain.sha256",
                            "evidence.json", "evidence.md"])
    assert hashlib.sha256(packed_json).hexdigest() == result["bundle"]


def test_evidence_bundle_verifies_on_its_own_after_being_emailed(workspace, tmp_path):
    """The attachment alone, unpacked on another machine, must still check out."""
    result = run(workspace, tmp_path, names=["plain", "clean"])
    received = tmp_path / "received"
    with zipfile.ZipFile(result["out_dir"] / "evidence_bundle.zip") as z:
        z.extractall(received)
    assert sa.verify_run(received) == []


# ---------------------------------------------------------------- timestamps

class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, limit=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def granted_response(request_der, status=0):
    """A minimal TimeStampResp echoing the request's digest and nonce."""
    digest, nonce = request_der[24:56], request_der[-11:-3]
    token = b"\x04" + bytes([len(digest) + len(nonce)]) + digest + nonce
    body = b"\x30\x03\x02\x01" + bytes([status]) + token
    return b"\x30" + bytes([len(body)]) + body


def test_timestamp_request_is_a_valid_rfc3161_query():
    digest = hashlib.sha256(b"schedule").digest()
    query, nonce = sa.timestamp_request(digest)
    assert query[0] == 0x30 and query[1] == len(query) - 2
    assert query[2:5] == b"\x02\x01\x01"                       # version 1
    assert bytes.fromhex("0609608648016503040201") in query    # sha-256 OID
    assert query[24:56] == digest
    assert query[-11:-3] == nonce and query[-13:-11] == b"\x02\x08"
    assert query[-3:] == b"\x01\x01\xff"                       # ask for the TSA cert
    assert 0x40 <= nonce[0] <= 0x7F
    assert sa.timestamp_request(digest)[1] != nonce


def test_openssl_reads_our_timestamp_request(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    digest = hashlib.sha256(b"schedule").digest()
    query_file = tmp_path / "q.tsq"
    query_file.write_bytes(sa.timestamp_request(digest)[0])
    as_query = subprocess.run(["openssl", "ts", "-query", "-in", str(query_file), "-text"],
                              capture_output=True, text=True)
    assert as_query.returncode == 0, as_query.stderr
    assert "sha256" in as_query.stdout.lower()
    assert "Certificate required: yes" in as_query.stdout

    parsed = subprocess.run(["openssl", "asn1parse", "-inform", "DER", "-in", str(query_file)],
                            capture_output=True, text=True)
    assert parsed.returncode == 0, parsed.stderr
    assert f"[HEX DUMP]:{digest.hex().upper()}" in parsed.stdout


def test_timestamp_status_parsing():
    assert sa.timestamp_status(b"\x30\x05\x30\x03\x02\x01\x00") == 0
    assert sa.timestamp_status(b"\x30\x05\x30\x03\x02\x01\x02") == 2
    long_form = b"\x30\x82\x00\x05\x30\x03\x02\x01\x01"
    assert sa.timestamp_status(long_form) == 1
    with pytest.raises(ValueError):
        sa.timestamp_status(b"<html>error</html>")


def test_timestamps_round_trip_through_generate_and_verify(unsigned_tokens, workspace, tmp_path, monkeypatch):
    sent = []

    def fake_urlopen(request, timeout):
        sent.append(request)
        return FakeResponse(granted_response(request.data))

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake_urlopen)
    result = run(workspace, tmp_path, names=["plain"], timestamp=True)

    assert [t["ok"] for t in result["timestamps"]] == [True] * len(sa.TSAS)
    assert [r.full_url for r in sent] == [url for _, url in sa.TSAS]
    for request in sent:  # only the hash of evidence.json leaves the machine
        assert request.data[24:56].hex() == result["bundle"]
        assert len(request.data) == 69
        assert request.get_header("Content-type") == "application/timestamp-query"
    for name, _ in sa.TSAS:
        assert (result["out_dir"] / "timestamps" / f"{name}.tsr").is_file()
        assert name in result["email"]
    with zipfile.ZipFile(result["out_dir"] / "evidence_bundle.zip") as z:
        assert f"timestamps/{sa.TSAS[0][0]}.tsr" in z.namelist()
    assert sa.verify_run(result["out_dir"]) == []

    # A timestamp only covers the exact json it was issued for.
    tamper_json(result["out_dir"])
    tamper_bundle = hashlib.sha256((result["out_dir"] / "evidence.json").read_bytes()).hexdigest()
    (result["out_dir"] / "BUNDLE_SHA256.txt").write_text(f"{tamper_bundle}  evidence.json\n")
    problems = sa.verify_run(result["out_dir"])
    assert len(problems) == len(sa.TSAS) and all("timestamps/" in p for p in problems)


@pytest.mark.parametrize("respond, reason", [
    (lambda req: FakeResponse(granted_response(req.data, status=2)), "rejected"),
    (lambda req: FakeResponse(granted_response(sa.timestamp_request(b"\0" * 32)[0])), "does not match"),
    (lambda req: FakeResponse(b"<html>503</html>"), "not a timestamp response"),
], ids=["rejected", "wrong-hash", "garbage"])
def test_bad_timestamp_responses_are_not_saved(unsigned_tokens, workspace, tmp_path, monkeypatch, respond, reason):
    monkeypatch.setattr(sa.urllib.request, "urlopen", lambda request, timeout: respond(request))
    result = run(workspace, tmp_path, names=["plain"], timestamp=True)
    assert all(not t["ok"] and reason in t["error"] for t in result["timestamps"])
    assert list((result["out_dir"] / "timestamps").glob("*.tsr")) == []
    assert "trusted timestamps" not in result["email"]


def test_run_still_completes_when_offline(unsigned_tokens, workspace, tmp_path, monkeypatch):
    def offline(request, timeout):
        raise OSError("network unreachable")

    monkeypatch.setattr(sa.urllib.request, "urlopen", offline)
    result = run(workspace, tmp_path, names=["plain"], timestamp=True)
    assert all(not t["ok"] for t in result["timestamps"])
    assert (result["out_dir"] / "email.txt").is_file()
    assert sa.verify_run(result["out_dir"]) == []


@pytest.mark.skipif(not os.environ.get("PRIORFOLIO_NETWORK"),
                    reason="set PRIORFOLIO_NETWORK=1 to contact the real timestamp authorities")
def test_real_timestamp_authorities(workspace, tmp_path):
    result = run(workspace, tmp_path, names=["plain"], timestamp=True)
    assert any(t["ok"] for t in result["timestamps"]), result["timestamps"]
    assert sa.verify_run(result["out_dir"]) == []


# ------------------------------------------------------------- command line

def cli(*args):
    script = Path(sa.__file__)
    args = [str(a) for a in args]
    if "--select" in args and "--timestamp" not in args:
        args.append("--no-timestamp")  # timestamps are on by default; tests stay offline
    return subprocess.run([os.sys.executable, str(script), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_cli_generate_then_verify_round_trip(workspace, tmp_path):
    out = tmp_path / "cli-out"
    made = cli("--root", workspace, "--select", "plain,clean", "--snapshot", "--out", out)
    assert made.returncode == 0, made.stderr
    (run_dir,) = out.iterdir()

    ok = cli("--verify", run_dir, "--against-disk")
    assert ok.returncode == 0 and "OK" in ok.stdout

    (workspace / "plain" / "main.py").write_text("changed\n")
    changed = cli("--verify", run_dir, "--against-disk")
    assert changed.returncode == 1 and "plain: on disk changed: main.py" in changed.stdout
    assert cli("--verify", run_dir).returncode == 0


def test_cli_list_writes_nothing(workspace, tmp_path):
    out = tmp_path / "cli-out"
    listed = cli("--root", workspace, "--list", "--out", out)
    assert listed.returncode == 0 and "NOT A GIT REPO" in listed.stdout
    assert not out.exists()


def test_cli_rejects_unknown_selection(workspace, tmp_path):
    bad = cli("--root", workspace, "--select", "nope", "--out", tmp_path / "o")
    assert bad.returncode != 0 and "no such project" in bad.stderr
