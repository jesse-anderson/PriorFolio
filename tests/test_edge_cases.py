"""Edge cases: awkward files, awkward repos, hostile run folders, bad networks."""
import hashlib
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

import priorfolio as sa
from conftest import git, write
from test_core import FakeResponse, by_name, cli, edit_json, granted_response

WINDOWS = os.name == "nt"
QUIET = lambda message: None


def only(root, tmp_path, name, **options):
    """Generate a run for one project under `root` and return (run, its result)."""
    projects = [p for p in sa.scan_roots([root]) if p["name"] == name]
    assert len(projects) == 1, f"{name} not found"
    result = sa.generate(projects, tmp_path / "out", log=QUIET, **options)
    return result, result["results"][0]


def manifest(result, entry):
    return sa.read_manifest((result["out_dir"] / entry["manifest"]).read_bytes())


def refresh_bundle(run_dir):
    digest = hashlib.sha256((run_dir / "evidence.json").read_bytes()).hexdigest()
    (run_dir / "BUNDLE_SHA256.txt").write_text(f"{digest}  evidence.json\n")
    return digest


def sha(data):
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------- awkward content

def test_empty_file_is_hashed(tmp_path):
    write(tmp_path / "r" / "p", {"empty.txt": b"", "full.txt": b"x"})
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert manifest(result, entry)["empty.txt"] == sha(b"")
    assert entry["file_count"] == 2 and entry["total_bytes"] == 1
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


@pytest.mark.parametrize("size", [(1 << 20) - 1, 1 << 20, (1 << 20) + 1, 3 * (1 << 20) + 7])
def test_files_around_the_read_chunk_size(tmp_path, size):
    data = random.Random(size).randbytes(size)
    write(tmp_path / "r" / "p", {"big.bin": data})
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert manifest(result, entry)["big.bin"] == sha(data)
    assert entry["total_bytes"] == size
    with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
        assert z.read("big.bin") == data


def test_bytes_are_hashed_exactly_with_no_newline_translation(tmp_path):
    files = {"crlf.txt": b"a\r\nb\r\n", "lf.txt": b"a\nb\n", "cr.txt": b"a\rb\r",
             "zero.bin": b"\x00\x01\x02\xff\xfe", "bom.txt": b"\xef\xbb\xbfhi",
             "not-utf8.txt": b"\xff\xfe\xfa caf\xe9"}
    write(tmp_path / "r" / "p", files)
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert manifest(result, entry) == {name: sha(data) for name, data in files.items()}
    with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
        assert {n: z.read(n) for n in z.namelist()} == files


def test_text_file_in_a_repo_is_hashed_as_it_sits_on_disk_not_as_git_stores_it(tmp_path):
    repo = tmp_path / "r" / "p"
    write(repo, {"win.txt": b"line1\r\nline2\r\n"})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "true")  # git stores LF, disk keeps CRLF
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    result, entry = only(tmp_path / "r", tmp_path, "p")
    assert manifest(result, entry)["win.txt"] == sha(b"line1\r\nline2\r\n")


ODD_NAMES = [
    "with space.txt", " leading space.txt", "two  spaces  inside.txt", "plain.txt",
    "café.txt", "é combining.txt", "日本語.txt", "emoji \U0001f600.txt",
    "русский.py", "عربي.txt",
    "line separator.txt", "next\u0085line.txt", "nbsp name.txt", "zero​width.txt",
    "-leading-dash.txt", "--help", "#hash.txt", "semi;colon.txt", "amp&ersand.txt",
    "per%cent.txt", "(paren).txt", "[bracket].txt", "{brace}.txt", "equals=.txt", "comma,name.txt",
    "bang!.txt", "at@sign.txt", "plus+.txt", "tilde~.txt", "back`tick.txt", "quote's.txt",
    "dollar$var.txt", "caret^.txt", ".hidden", "..two-dots", "a..b", "no_extension",
    "UPPER.TXT", "MixedCase.Txt", "n" * 200 + ".txt",
    "deep/er/and/deeper/file.txt", "dir with space/sub dir/f.txt", "日本/語/f.txt",
]


@pytest.mark.parametrize("as_repo", [False, True], ids=["plain", "repo"])
def test_odd_file_names_survive_the_whole_round_trip(tmp_path, as_repo):
    files = {name: f"content of {i}\n".encode() for i, name in enumerate(dict.fromkeys(ODD_NAMES))}
    project = tmp_path / "r" / "p"
    write(project, files)
    if as_repo:
        git(project, "init", "-q", "-b", "main")
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", "c")
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)

    assert entry["warnings"] == []
    assert manifest(result, entry) == {name: sha(data) for name, data in files.items()}
    with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
        assert {i.filename: z.read(i) for i in z.infolist()} == files
    assert sa.verify_run(result["out_dir"], against_disk=True) == []

    restored = tmp_path / "restored"
    with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
        z.extractall(restored / "p")
    again, entry2 = only(restored, tmp_path / "second", "p")
    assert entry2["content_sha256"] == entry["content_sha256"]


def test_manifest_order_is_by_code_point_and_identical_for_plain_and_repo(tmp_path):
    names = ["b.txt", "a/z.txt", "a-b.txt", "a.txt", "C.txt", "Z.txt", "_x.txt", "a/a.txt",
             "é.txt", "a b.txt", "10.txt", "9.txt", "a0.txt"]
    plain, repo = tmp_path / "r" / "plain", tmp_path / "r" / "repo"
    for target in (plain, repo):
        write(target, {n: n.encode() for n in names})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    result = sa.generate(sa.scan_roots([tmp_path / "r"]), tmp_path / "out", log=QUIET)
    entries = by_name(result)
    assert list(manifest(result, entries["plain"])) == sorted(names)
    assert entries["plain"]["content_sha256"] == entries["repo"]["content_sha256"]


def test_manifest_lines_round_trip_for_any_name():
    rng = random.Random(7)
    alphabet = "ab /\\\n\r\t .-_ \u0085é日\U0001f600'\"|*?"
    names = {"".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30))) for _ in range(500)}
    names |= {"\\", "\\\\", "\n", "a\\nb", "a\nb", "\\n", "x  y", "  ", "trailing\\"}
    names = {n for n in names if "  " not in n or n in ("x  y", "  ")}
    digest = "ab" * 32
    text = "".join(sa.manifest_line(digest, n) for n in sorted(names))
    assert sa.read_manifest(text.encode("utf-8")) == dict.fromkeys(names, digest)


def test_manifest_escaping_matches_sha256sum_convention():
    digest = "0" * 64
    assert sa.manifest_line(digest, "plain.txt") == f"{digest}  plain.txt\n"
    assert sa.manifest_line(digest, "a\nb") == f"\\{digest}  a\\nb\n"
    assert sa.manifest_line(digest, "a\\b") == f"\\{digest}  a\\\\b\n"


@pytest.mark.skipif(not WINDOWS, reason="Windows path-length limit")
def test_paths_longer_than_260_characters_are_hashed(tmp_path):
    project = tmp_path / "r" / "p"
    deep = project
    for i in range(10):
        deep = deep / f"{'d' * 28}{i}"
    try:
        os.makedirs(sa.fs_path(deep))
        with open(sa.fs_path(deep / "deep.txt"), "wb") as f:
            f.write(b"deep content")
        write(project, {"top.txt": "top\n"})
        rel = deep.relative_to(project).as_posix() + "/deep.txt"
        assert len(str(deep / "deep.txt")) > 300

        result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
        assert entry["warnings"] == []
        assert manifest(result, entry) == {rel: sha(b"deep content"), "top.txt": sha(b"top\n")}
        assert sa.verify_run(result["out_dir"], against_disk=True) == []
    finally:
        shutil.rmtree(sa.fs_path(project), ignore_errors=True)


@pytest.mark.parametrize("when", [0, 86400 * 365 * 5, 86400 * 365 * 250], ids=["1970", "1975", "2220"])
def test_file_dates_outside_the_zip_range_do_not_break_the_snapshot(tmp_path, when):
    write(tmp_path / "r" / "p", {"old.txt": "x\n"})
    try:
        os.utime(tmp_path / "r" / "p" / "old.txt", (when, when))
    except OSError:
        pytest.skip("this filesystem cannot store that date")
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert entry["file_count"] == 1 and "snapshot" in entry
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_zip_entry_dates_are_clamped():
    assert sa.zip_entry("a", 0).date_time == (1980, 1, 1, 0, 0, 0)
    assert sa.zip_entry("a", 86400 * 365 * 250).date_time == (2107, 12, 31, 23, 59, 58)
    assert sa.zip_entry("a", 1_700_000_000).date_time[0] == 2023


@pytest.mark.skipif(not WINDOWS, reason="Windows file attributes")
def test_hidden_and_read_only_files_are_hashed(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {"hidden.txt": "h\n", "readonly.txt": "r\n"})
    subprocess.run(["attrib", "+h", str(project / "hidden.txt")], check=True)
    os.chmod(project / "readonly.txt", stat.S_IREAD)
    try:
        result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
        assert sorted(manifest(result, entry)) == ["hidden.txt", "readonly.txt"]
        assert sa.verify_run(result["out_dir"], against_disk=True) == []
    finally:
        os.chmod(project / "readonly.txt", stat.S_IWRITE | stat.S_IREAD)


def test_many_files(tmp_path):
    files = {f"d{i % 37}/f{i}.txt": f"{i}\n" for i in range(1500)}
    write(tmp_path / "r" / "p", files)
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert entry["file_count"] == 1500
    assert list(manifest(result, entry)) == sorted(files)
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


# ------------------------------------------------- unreadable and vanishing

def lock_exclusively(path):
    """Open a file so that nothing else can read it. Returns a closer."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle not in (None, wintypes.HANDLE(-1).value), "could not lock the file"
    return lambda: kernel32.CloseHandle(handle)


@pytest.mark.skipif(not WINDOWS, reason="Windows file locking")
def test_file_locked_by_another_program_is_reported_not_silently_dropped(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {"ok.txt": "fine\n", "locked.db": "busy\n"})
    release = lock_exclusively(project / "locked.db")
    try:
        result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    finally:
        release()

    assert list(manifest(result, entry)) == ["ok.txt"]
    assert len(entry["warnings"]) == 1 and "locked.db" in entry["warnings"][0]
    assert result["warnings"] == [f"p: {entry['warnings'][0]}"]
    assert "WARNING" in result["markdown"] and "locked.db" in result["markdown"]
    assert "Incomplete: 1 warning(s)" in result["email"]
    assert sa.verify_run(result["out_dir"]) == []

    # Once the file is free, the hash changes to cover it.
    again, entry2 = only(tmp_path / "r", tmp_path / "second", "p")
    assert entry2["warnings"] == [] and entry2["content_sha256"] != entry["content_sha256"]


def test_read_failure_partway_through_a_file_drops_the_file_and_the_snapshot(tmp_path, monkeypatch):
    project = tmp_path / "r" / "p"
    write(project, {"a.txt": "a\n", "bad.bin": b"x" * (3 << 20), "z.txt": "z\n"})
    real_open = open

    class FailsAfterFirstChunk:
        def __init__(self, handle):
            self.handle, self.reads = handle, 0

        def read(self, size=-1):
            self.reads += 1
            if self.reads > 1:
                raise OSError(5, "Input/output error")
            return self.handle.read(size)

        def close(self):
            self.handle.close()

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return FailsAfterFirstChunk(handle) if str(path).endswith("bad.bin") else handle

    monkeypatch.setattr(sa, "open", flaky_open, raising=False)
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    monkeypatch.undo()

    assert list(manifest(result, entry)) == ["a.txt", "z.txt"]
    assert any("bad.bin" in w and "completely" in w for w in entry["warnings"])
    assert any("snapshot zip not written" in w for w in entry["warnings"])
    assert "snapshot" not in entry and not list((result["out_dir"] / "snapshots").iterdir())
    assert sa.verify_run(result["out_dir"]) == []


def test_project_folder_deleted_between_scan_and_generate(tmp_path):
    write(tmp_path / "r" / "gone", {"a.txt": "a\n"})
    write(tmp_path / "r" / "stays", {"b.txt": "b\n"})
    projects = sa.scan_roots([tmp_path / "r"])
    shutil.rmtree(tmp_path / "r" / "gone")

    result = sa.generate(projects, tmp_path / "out", snapshot=True, log=QUIET)
    entries = by_name(result)
    assert entries["gone"]["state"] == "FOLDER NOT FOUND"
    assert entries["gone"]["content_sha256"] is None
    assert entries["gone"]["warnings"] == ["folder no longer exists; nothing was hashed"]
    assert entries["stays"]["file_count"] == 1
    assert sa.verify_run(result["out_dir"]) == []


def test_file_deleted_between_listing_and_hashing_is_noted(tmp_path, monkeypatch):
    project = tmp_path / "r" / "p"
    write(project, {"keep.txt": "k\n", "vanish.txt": "v\n"})
    real = sa.list_files

    def list_then_delete(*args, **kwargs):
        listed = real(*args, **kwargs)
        (project / "vanish.txt").unlink()
        return listed

    monkeypatch.setattr(sa, "list_files", list_then_delete)
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert list(manifest(result, entry)) == ["keep.txt"]
    assert "listed by git but not on disk: vanish.txt" in entry["notes"]
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_snapshot_holds_the_same_bytes_that_were_hashed_even_if_the_file_changes_after(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {"f.txt": "version one\n"})
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    (project / "f.txt").write_text("version two\n")
    with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
        assert sha(z.read("f.txt")) == manifest(result, entry)["f.txt"] == sha(b"version one\n")


# ------------------------------------------------------- links and junctions

def make_junction(link, target):
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


@pytest.mark.skipif(not WINDOWS, reason="Windows junctions")
@pytest.mark.parametrize("as_repo", [False, True], ids=["plain", "repo"])
def test_junction_to_another_folder_is_not_followed(tmp_path, as_repo):
    outside = tmp_path / "outside"
    write(outside, {"private.txt": "must not be hashed\n", "sub/more.txt": "nor this\n"})
    project = tmp_path / "r" / "p"
    write(project, {"mine.txt": "mine\n"})
    if as_repo:
        git(project, "init", "-q", "-b", "main")
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", "c")
    make_junction(project / "shared", outside)
    try:
        result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
        assert list(manifest(result, entry)) == ["mine.txt"]
        assert "link to another folder not followed: shared/ (2 files)" in entry["notes"]
        with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
            assert z.namelist() == ["mine.txt"]
    finally:
        os.rmdir(project / "shared")
    assert (outside / "private.txt").exists()


@pytest.mark.skipif(not WINDOWS, reason="Windows junctions")
def test_junction_at_the_top_of_a_root_is_not_listed_as_a_project(tmp_path):
    write(tmp_path / "elsewhere", {"x.txt": "x\n"})
    write(tmp_path / "r" / "real", {"y.txt": "y\n"})
    make_junction(tmp_path / "r" / "linked", tmp_path / "elsewhere")
    try:
        assert [p["name"] for p in sa.scan_roots([tmp_path / "r"])] == ["real"]
    finally:
        os.rmdir(tmp_path / "r" / "linked")


def test_symlinks_are_noted_and_not_followed(tmp_path):
    outside = tmp_path / "outside"
    write(outside, {"secret.txt": "outside\n"})
    project = tmp_path / "r" / "p"
    write(project, {"real.txt": "real\n"})
    try:
        os.symlink(outside / "secret.txt", project / "file-link.txt")
        os.symlink(outside, project / "dir-link", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted here")
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert list(manifest(result, entry)) == ["real.txt"]
    assert any("file-link.txt" in n for n in entry["notes"])
    assert not any("secret" in line for line in manifest(result, entry))


# ------------------------------------------------------------- awkward repos

def committed(path, files, message="c"):
    write(path, files)
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                          text=True).stdout.strip()


def test_repo_git_refuses_to_read_is_flagged_and_still_hashed(tmp_path, monkeypatch):
    committed(tmp_path / "r" / "p", {"a.txt": "a\n", "sub/b.txt": "b\n", ".gitignore": "*.log\n"})
    write(tmp_path / "r" / "p", {"x.log": "ignored\n", "new.txt": "untracked\n"})
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")  # git's own "wrong owner" switch

    (info,) = sa.scan_roots([tmp_path / "r"])
    assert info["state"].startswith("GIT ERROR: detected dubious ownership")
    assert info["commit"] is None and info["git_error"]

    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert sorted(manifest(result, entry)) == [".gitignore", "a.txt", "new.txt", "sub/b.txt"]
    assert entry["commit"] is None
    assert any("git could not read this repository" in w for w in entry["warnings"])
    assert not any(n.startswith(".git/") for n in manifest(result, entry))
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_broken_git_folder_does_not_borrow_the_enclosing_repos_commit(tmp_path):
    outer_commit = committed(tmp_path / "r" / "outer", {"o.txt": "o\n"})
    inner = tmp_path / "r" / "outer" / "inner"
    write(inner, {"i.txt": "i\n"})
    (inner / ".git").mkdir()

    (info,) = sa.scan_roots([], singles=[inner])
    assert info["commit"] is None and info["commit"] != outer_commit
    assert info["state"] == "GIT ERROR: its .git folder is not a usable repository"

    result = sa.generate([info], tmp_path / "out", log=QUIET)
    entry = result["results"][0]
    assert list(manifest(result, entry)) == ["i.txt"] and entry["commit"] is None
    assert entry["warnings"]


def test_broken_git_folder_with_no_enclosing_repo(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {"f.txt": "f\n"})
    (project / ".git").mkdir()
    (info,) = sa.scan_roots([tmp_path / "r"])
    assert info["state"].startswith("GIT ERROR") and info["commit"] is None
    result = sa.generate([info], tmp_path / "out", log=QUIET)
    assert list(manifest(result, result["results"][0])) == ["f.txt"]


def test_subfolder_of_a_repo_added_as_a_single_project(tmp_path):
    committed(tmp_path / "r" / "mono", {"pkg/a.py": "a\n", "other/b.py": "b\n"})
    (info,) = sa.scan_roots([], singles=[tmp_path / "r" / "mono" / "pkg"])
    assert info["state"] == "NOT A GIT REPO" and info["name"] == "pkg"
    result = sa.generate([info], tmp_path / "out", log=QUIET)
    assert list(manifest(result, result["results"][0])) == ["a.py"]


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"])
def test_git_environment_variables_from_the_shell_are_ignored(tmp_path, monkeypatch, variable):
    wanted = committed(tmp_path / "r" / "p", {"a.txt": "a\n"})
    committed(tmp_path / "decoy", {"d.txt": "d\n"}, message="decoy")
    value = {"GIT_DIR": tmp_path / "decoy" / ".git", "GIT_WORK_TREE": tmp_path / "decoy",
             "GIT_INDEX_FILE": tmp_path / "decoy" / ".git" / "index"}[variable]
    monkeypatch.setenv(variable, str(value))
    result, entry = only(tmp_path / "r", tmp_path, "p")
    assert entry["commit"] == wanted and entry["state"] == "no remote"
    assert list(manifest(result, entry)) == ["a.txt"]


def test_detached_head(tmp_path):
    commit = committed(tmp_path / "r" / "p", {"a.txt": "a\n"})
    git(tmp_path / "r" / "p", "checkout", "-q", "--detach")
    (info,) = sa.scan_roots([tmp_path / "r"])
    assert info["branch"] == "(detached HEAD)" and info["commit"] == commit


def test_remote_not_called_origin_is_still_found_and_origin_is_preferred(tmp_path):
    repo = tmp_path / "r" / "p"
    committed(repo, {"a.txt": "a\n"})
    git(repo, "remote", "add", "github", "https://tok@github.com/me/p.git")
    assert sa.describe(repo, tmp_path / "r")["remote"] == "https://github.com/me/p.git"
    git(repo, "remote", "add", "origin", "https://github.com/me/origin.git")
    assert sa.describe(repo, tmp_path / "r")["remote"] == "https://github.com/me/origin.git"


def test_unpushed_counts_commits_on_every_local_branch(tmp_path):
    repo = tmp_path / "r" / "p"
    committed(repo, {"a.txt": "a\n"})
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    git(repo, "checkout", "-q", "main")
    info = sa.describe(repo, tmp_path / "r")
    assert info["unpushed"] == 2 and info["state"] == "2 unpushed commits"


def test_submodule_entry_is_noted_and_the_run_still_verifies(tmp_path):
    repo = tmp_path / "r" / "p"
    commit = committed(repo, {"a.txt": "a\n"})
    write(repo, {"vendor/lib.c": "int x;\n"})
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor")
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert "a.txt" in manifest(result, entry)
    assert "submodule or nested repo not included: vendor/" in entry["notes"]
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_merge_conflict_lists_each_file_once(tmp_path):
    repo = tmp_path / "r" / "p"
    committed(repo, {"f.txt": "base\n"})
    git(repo, "checkout", "-q", "-b", "other")
    write(repo, {"f.txt": "theirs\n"})
    git(repo, "commit", "-q", "-am", "theirs")
    git(repo, "checkout", "-q", "main")
    write(repo, {"f.txt": "ours\n"})
    git(repo, "commit", "-q", "-am", "ours")
    subprocess.run(["git", "merge", "other"], cwd=repo, capture_output=True)  # conflicts

    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert list(manifest(result, entry)) == ["f.txt"]
    assert manifest(result, entry)["f.txt"] == sha((repo / "f.txt").read_bytes())
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_ignored_file_that_was_force_added_is_hashed(tmp_path):
    repo = tmp_path / "r" / "p"
    write(repo, {".gitignore": "*.key\nbuild/\n", "a.txt": "a\n", "prod.key": "k\n",
                 "build/kept.js": "kept\n", "build/dropped.js": "dropped\n"})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "add", "-f", "prod.key", "build/kept.js")
    git(repo, "commit", "-q", "-m", "c")
    result, entry = only(tmp_path / "r", tmp_path, "p")
    assert list(manifest(result, entry)) == [".gitignore", "a.txt", "build/kept.js", "prod.key"]


def test_tracked_files_in_dependency_named_folders_are_hashed_untracked_ones_are_noted(tmp_path):
    repo = tmp_path / "r" / "p"
    committed(repo, {"target/keep.rs": "tracked\n", "env/config.py": "tracked\n", "x.pyc": b"tracked"})
    write(repo, {"target/debug/out.o": "built\n", "target/debug/more.o": "built\n",
                 "node_modules/a/i.js": "dep\n", "y.pyc": b"cache", "src/env/real.py": "source?\n"})
    result, entry = only(tmp_path / "r", tmp_path, "p")
    assert list(manifest(result, entry)) == ["env/config.py", "target/keep.rs", "x.pyc"]
    assert entry["notes"] == [
        "compiled Python files not included: 1 file",
        "folder not included: node_modules/ (1 file)",
        "folder not included: src/env/ (1 file)",
        "folder not included: target/ (2 files)",
    ]


def test_gitignore_rules_apply_in_plain_folders_including_nested_and_negated(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {
        ".gitignore": "*.tmp\n/secret/\n!keep.tmp\n",
        "a.tmp": "x", "keep.tmp": "kept", "secret/s.txt": "s", "src/secret/ok.txt": "ok",
        "sub/.gitignore": "local.txt\n", "sub/local.txt": "l", "sub/kept.txt": "k",
    })
    result, entry = only(tmp_path / "r", tmp_path, "p")
    assert list(manifest(result, entry)) == [
        ".gitignore", "keep.tmp", "src/secret/ok.txt", "sub/.gitignore", "sub/kept.txt"]


def test_worktree_checkout_is_a_repo(tmp_path):
    main = tmp_path / "main"
    commit = committed(main, {"a.txt": "a\n"})
    (tmp_path / "r").mkdir()
    git(main, "worktree", "add", "-q", "-b", "wt", str(tmp_path / "r" / "wt"))
    (info,) = sa.scan_roots([tmp_path / "r"])
    assert info["is_repo"] and info["commit"] == commit and info["branch"] == "wt"
    result = sa.generate([info], tmp_path / "out", log=QUIET)
    assert list(manifest(result, result["results"][0])) == ["a.txt"]


def test_bare_repo_folder_does_not_crash(tmp_path):
    (tmp_path / "r").mkdir()
    git(tmp_path / "r", "init", "-q", "--bare", "store.git")
    result = sa.generate(sa.scan_roots([tmp_path / "r"]), tmp_path / "out", snapshot=True, log=QUIET)
    assert result["results"][0]["commit"] is None
    assert sa.verify_run(result["out_dir"]) == []


def test_scanning_a_stale_repo_does_not_rewrite_its_index(tmp_path):
    repo = tmp_path / "r" / "p"
    committed(repo, {"a.txt": "a\n"})
    os.utime(repo / "a.txt", (1_800_000_000, 1_800_000_000))  # makes git want to refresh
    index = repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    sa.scan_roots([tmp_path / "r"])
    only(tmp_path / "r", tmp_path, "p", snapshot=True)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


# ------------------------------------------------------- discovery and names

def test_discovery_depth_and_skipped_folders(tmp_path):
    root = tmp_path / "r"
    committed(root / "group" / "a" / "b" / "found", {"f.txt": "f\n"})          # depth 3: found
    committed(root / "group" / "a" / "b" / "c" / "too_deep", {"f.txt": "f\n"})  # depth 4: not
    committed(root / "group" / "node_modules" / "dep", {"f.txt": "f\n"})        # dependency: not
    committed(root / "repo" / "vendored", {"f.txt": "f\n"})                     # inside a repo: not
    committed(root / "repo", {"top.txt": "t\n"})
    (root / "loose_file.txt").write_text("not a project\n")
    (root / "empty_dir").mkdir()
    names = [p["name"] for p in sa.scan_roots([root])]
    assert names == ["empty_dir", "group", "group/a/b/found", "repo"]


def test_empty_root_gives_no_projects(tmp_path):
    (tmp_path / "r").mkdir()
    assert sa.scan_roots([tmp_path / "r"]) == []


def test_two_roots_with_the_same_folder_name_get_distinct_names_and_files(tmp_path):
    for parent, content in (("one", "first\n"), ("two", "second\n")):
        write(tmp_path / parent / "GitHub" / "app", {"f.txt": content})
    projects = sa.scan_roots([tmp_path / "one" / "GitHub", tmp_path / "two" / "GitHub"])
    names = [p["name"] for p in projects]
    assert names == ["GitHub/app", "GitHub/app (2)"]

    result = sa.generate(projects, tmp_path / "out", snapshot=True, log=QUIET)
    entries = by_name(result)
    assert entries["GitHub/app"]["content_sha256"] != entries["GitHub/app (2)"]["content_sha256"]
    assert entries["GitHub/app"]["manifest"] != entries["GitHub/app (2)"]["manifest"]
    assert len(list((result["out_dir"] / "manifests").iterdir())) == 2
    assert len(list((result["out_dir"] / "snapshots").iterdir())) == 2
    assert sa.verify_run(result["out_dir"], against_disk=True) == []


def test_file_stems_never_collide():
    stems = sa.file_stems(["a/b", "a__b", "A__B", "x", "X", "con:fig", "trail. ", "", "a/b (2)"])
    assert len({s.lower() for s in stems}) == len(stems)
    assert all(s and not set(s) & set('<>:"/\\|?*') and s == s.rstrip(". ") for s in stems)


@pytest.mark.skipif(not WINDOWS, reason="case-insensitive filesystem")
def test_same_root_spelled_in_a_different_case_is_one_root(tmp_path):
    write(tmp_path / "Root" / "app", {"f.txt": "f\n"})
    found = sa.scan_roots([tmp_path / "Root", Path(str(tmp_path / "Root").upper()),
                           Path(str(tmp_path / "Root") + os.sep)])
    assert [p["name"] for p in found] == ["app"]


def test_root_inside_another_root_does_not_duplicate_projects(tmp_path):
    committed(tmp_path / "r" / "group" / "inner", {"f.txt": "f\n"})
    found = sa.scan_roots([tmp_path / "r", tmp_path / "r" / "group"])
    assert sorted(p["name"] for p in found) == ["group", "group/inner"]


def test_selection_prefers_an_exact_name_over_a_row_number():
    projects = [{"name": n} for n in ("alpha", "2", "gamma", "1-3")]
    pick = lambda text: [p["name"] for p in sa.parse_selection(text, projects)]
    assert pick("2") == ["2"]
    assert pick("1-3") == ["1-3"]
    assert pick("1") == ["alpha"]
    assert pick("  ALL ") == pick("*") == ["alpha", "2", "gamma", "1-3"]
    assert pick(",, 3 ,,") == ["gamma"]


def test_markdown_table_survives_a_pipe_in_a_name(tmp_path):
    entry = {"name": "a|b", "commit": None, "commit_date": None, "content_sha256": "f" * 64,
             "file_count": 1, "remote": None, "branch": None, "tree": None, "state": "NOT A GIT REPO",
             "total_bytes": 1, "newest_file_mtime": None, "notes": [], "warnings": []}
    sa.write_markdown([entry], {"generated_utc": "t", "roots": ["r"]}, tmp_path / "s.md")
    row = next(line for line in (tmp_path / "s.md").read_text().splitlines() if line.startswith("| 1 |"))
    assert row.replace("\\|", "").count("|") == 7


# ---------------------------------------------------------- output handling

def test_output_folder_inside_a_project_is_never_hashed(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {"src.py": "x = 1\n"})
    projects = sa.scan_roots([tmp_path / "r"])
    first = sa.generate(projects, project / "results", snapshot=True, log=QUIET)
    second = sa.generate(projects, project / "results", snapshot=True, log=QUIET)
    a, b = first["results"][0], second["results"][0]
    assert a["content_sha256"] == b["content_sha256"]
    assert list(manifest(second, b)) == ["src.py"]
    assert any("output folder not included: results/" in n for n in b["notes"])


def test_three_runs_in_the_same_second_get_three_folders(tmp_path):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})
    projects = sa.scan_roots([tmp_path / "r"])
    runs = [sa.generate(projects, tmp_path / "out", log=QUIET) for _ in range(3)]
    assert len({r["out_dir"] for r in runs}) == 3
    assert all(sa.verify_run(r["out_dir"]) == [] for r in runs)


def test_failed_run_is_marked_incomplete_and_never_looks_finished(tmp_path, monkeypatch):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})
    projects = sa.scan_roots([tmp_path / "r"])

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sa, "write_markdown", disk_full)
    with pytest.raises(OSError):
        sa.generate(projects, tmp_path / "out", log=QUIET)
    (left,) = (tmp_path / "out").iterdir()
    assert left.name.endswith("-INCOMPLETE")
    assert not (left / "BUNDLE_SHA256.txt").exists()
    assert sa.verify_run(left) != []


def test_generating_nothing_is_an_error(tmp_path):
    with pytest.raises(sa.ToolError):
        sa.generate([], tmp_path / "out", log=QUIET)
    assert not (tmp_path / "out").exists()


def test_missing_git_gives_a_plain_message(tmp_path, monkeypatch):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})
    projects = sa.scan_roots([tmp_path / "r"])
    monkeypatch.setattr(sa.shutil, "which", lambda name: None)
    for call in (lambda: sa.scan_roots([tmp_path / "r"]),
                 lambda: sa.generate(projects, tmp_path / "out", log=QUIET)):
        with pytest.raises(sa.ToolError, match="git was not found"):
            call()


def test_result_does_not_depend_on_the_current_directory(tmp_path, monkeypatch):
    committed(tmp_path / "r" / "p", {"a.txt": "a\n", "d/b.txt": "b\n"})
    first, a = only(tmp_path / "r", tmp_path, "p")
    monkeypatch.chdir(tmp_path / "r" / "p" / "d")
    second, b = only(Path("..") / "..", tmp_path / "second", "p")
    assert a["content_sha256"] == b["content_sha256"] and a["path"] == b["path"]


# --------------------------------------------------- hostile or damaged runs

@pytest.fixture
def good_run(tmp_path):
    write(tmp_path / "r" / "p", {"a.txt": "a\n", "b/c.txt": "c\n"})
    write(tmp_path / "r" / "q", {"z.txt": "z\n"})
    result = sa.generate(sa.scan_roots([tmp_path / "r"]), tmp_path / "out", snapshot=True, log=QUIET)
    assert sa.verify_run(result["out_dir"], against_disk=True) == []
    return result


@pytest.mark.parametrize("content", [
    b"", b"not json", b"[]", b"{}", b'{"projects": 5}', b'{"projects": "abc"}', b"\xff\xfe\x00",
    b'{"projects": null}',
], ids=["empty", "text", "list", "no-projects", "number", "string", "not-utf8", "null"])
def test_verify_survives_a_wrecked_json(good_run, content):
    (good_run["out_dir"] / "evidence.json").write_bytes(content)
    problems = sa.verify_run(good_run["out_dir"], against_disk=True)
    assert len(problems) == 1 and "unreadable" in problems[0]
    assert sa.run_summary(good_run["out_dir"]) is None or content == b'{"projects": "abc"}'


@pytest.mark.parametrize("damage", [
    lambda p: p.clear(),
    lambda p: p.pop("manifest"),
    lambda p: p.pop("content_sha256"),
    lambda p: p.pop("name"),
    lambda p: p.update(manifest=None),
    lambda p: p.update(manifest=["x"]),
    lambda p: p.update(snapshot=17),
    lambda p: p.pop("snapshot_sha256"),
    lambda p: p.update(path=None),
    lambda p: p.update(content_sha256=12345),
], ids=["emptied", "no-manifest", "no-hash", "no-name", "manifest-null", "manifest-list",
        "snapshot-number", "no-snapshot-hash", "path-null", "hash-number"])
def test_verify_reports_malformed_project_entries_without_crashing(good_run, damage):
    edit_json(good_run["out_dir"], lambda d: damage(d["projects"][0]))
    refresh_bundle(good_run["out_dir"])
    problems = sa.verify_run(good_run["out_dir"], against_disk=True)
    assert problems and all(isinstance(p, str) for p in problems)


def test_verify_handles_projects_that_are_not_objects(good_run):
    edit_json(good_run["out_dir"], lambda d: d.update(projects=[7, "x", None, [], d["projects"][1]]))
    refresh_bundle(good_run["out_dir"])
    problems = sa.verify_run(good_run["out_dir"])
    assert len(problems) == 4 and all("malformed" in p for p in problems)


@pytest.mark.parametrize("content", [b"", b"\xff\xfe", b"   \n", b"zz", b"0" * 64])
def test_verify_handles_a_wrecked_bundle_file(good_run, content):
    (good_run["out_dir"] / "BUNDLE_SHA256.txt").write_bytes(content)
    assert sa.verify_run(good_run["out_dir"]) == ["BUNDLE_SHA256.txt does not match evidence.json"]


def test_verify_handles_a_manifest_that_is_not_text(good_run):
    garbage = b"\xff\xfe\xfd not utf-8"
    (good_run["out_dir"] / "manifests" / "p.sha256").write_bytes(garbage)
    edit_json(good_run["out_dir"], lambda d: d["projects"][0].update(content_sha256=sha(garbage)))
    refresh_bundle(good_run["out_dir"])
    problems = sa.verify_run(good_run["out_dir"])
    assert any(p.startswith("p:") and "malformed" in p for p in problems)


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_zip_with_a_second_copy_of_a_file_is_caught_even_if_every_hash_is_rewritten(good_run):
    run_dir = good_run["out_dir"]
    zip_path = run_dir / "snapshots" / "p.zip"
    with zipfile.ZipFile(zip_path) as z:
        original = z.read("a.txt")
    with zipfile.ZipFile(zip_path, "a") as z:  # altered copy first on extract, original read last
        z.writestr("a.txt", original)
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("a.txt", b"altered\n")
        z.writestr("b/c.txt", b"c\n")
        z.writestr("a.txt", original)
    edit_json(run_dir, lambda d: d["projects"][0].update(
        snapshot_sha256=sha(zip_path.read_bytes())))
    refresh_bundle(run_dir)
    assert "p: snapshot zip holds the same file name more than once" in sa.verify_run(run_dir)


@pytest.mark.parametrize("damage", [
    lambda z: z.write_bytes(z.read_bytes()[: len(z.read_bytes()) // 2]),
    lambda z: z.write_bytes(b""),
    lambda z: z.write_bytes(b"PK\x03\x04" + b"\x00" * 40),
    lambda z: z.unlink(),
    lambda z: (z.unlink(), z.mkdir()),
], ids=["truncated", "empty", "header-only", "deleted", "is-a-folder"])
def test_verify_handles_a_damaged_snapshot_zip(good_run, damage):
    damage(good_run["out_dir"] / "snapshots" / "p.zip")
    problems = sa.verify_run(good_run["out_dir"])
    assert problems and all(p.startswith("p: snapshot") for p in problems)


def test_a_fully_rewritten_run_only_fails_against_the_hash_you_kept(good_run):
    """Why the bundle hash must live somewhere outside the folder."""
    run_dir, original = good_run["out_dir"], good_run["bundle"]
    assert sa.verify_run(run_dir, expect_bundle=original) == []
    assert sa.verify_run(run_dir, expect_bundle=f"  {original.upper()}\n") == []

    edit_json(run_dir, lambda d: d.update(generated_utc="2020-01-01T00:00:00+00:00"))
    forged = refresh_bundle(run_dir)

    assert sa.verify_run(run_dir) == []                       # self-consistent again
    problems = sa.verify_run(run_dir, expect_bundle=original)  # but not what was recorded
    assert len(problems) == 1 and original in problems[0] and forged in problems[0]


def test_verify_against_disk_when_the_project_folder_has_moved(good_run, tmp_path):
    shutil.move(tmp_path / "r" / "p", tmp_path / "r" / "renamed")
    assert sa.verify_run(good_run["out_dir"]) == []
    problems = sa.verify_run(good_run["out_dir"], against_disk=True)
    assert problems == ["p: on disk missing: a.txt", "p: on disk missing: b/c.txt"]


def test_verify_against_disk_ignores_files_added_later(good_run, tmp_path):
    write(tmp_path / "r" / "p", {"brand_new.txt": "later work\n"})
    assert sa.verify_run(good_run["out_dir"], against_disk=True) == []


@pytest.mark.parametrize("content", [b"", b"\x30", b"\x30\x80", b"garbage", b"\x30\x05\x30\x03\x02\x01\x00",
                                     b"\x30\x84\xff\xff\xff\xff", b"\x30\x05\x30\x03\x02\x09\x00"])
def test_verify_handles_garbage_timestamp_files(good_run, content):
    stamps = good_run["out_dir"] / "timestamps"
    stamps.mkdir()
    (stamps / "fake.tsr").write_bytes(content)
    assert sa.verify_run(good_run["out_dir"]) == [
        "timestamps/fake.tsr was not issued for this evidence.json"]
    assert sa.run_summary(good_run["out_dir"])["timestamps"] == [("fake", None)]


def test_run_summary_reports_what_the_folder_claims(good_run):
    summary = sa.run_summary(good_run["out_dir"])
    assert summary["bundle"] == good_run["bundle"] and summary["projects"] == 2
    assert summary["timestamps"] == []
    assert sa.run_summary(good_run["out_dir"] / "nope") is None


# ---------------------------------------------------------------- timestamps

def test_timestamp_parsers_never_raise_anything_unexpected_on_random_bytes():
    rng = random.Random(3161)
    for _ in range(5000):
        blob = rng.randbytes(rng.randint(0, 40))
        if rng.random() < 0.5:
            blob = b"\x30" + blob
        try:
            sa.timestamp_status(blob)
        except ValueError:
            pass
        assert sa.timestamp_time(blob) is None or len(sa.timestamp_time(blob)) == 20


@pytest.mark.parametrize("encoded, expected", [
    (b"\x18\x0f20261007142901Z", "2026-10-07T14:29:01Z"),
    (b"\x18\x1320261007142901.123Z", "2026-10-07T14:29:01Z"),
    (b"junk\x18\x0f20261007142901Zmore\x18\x0f20991231235959Z", "2026-10-07T14:29:01Z"),
    (b"\x17\x0d261007142901Z", None),
    (b"no time here", None),
])
def test_timestamp_time_parsing(encoded, expected):
    assert sa.timestamp_time(encoded) == expected


def stamped_response(request_der, when=b"20261007142901Z"):
    body = granted_response(request_der)
    extra = b"\x18" + bytes([len(when)]) + when
    return bytes([0x30, body[1] + len(extra)]) + body[2:] + extra


def test_timestamp_time_reaches_the_email_and_the_summary(tmp_path, monkeypatch):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})
    monkeypatch.setattr(sa.urllib.request, "urlopen",
                        lambda request, timeout: FakeResponse(stamped_response(request.data)))
    result, _ = only(tmp_path / "r", tmp_path, "p", timestamp=True)
    assert all(t["time"] == "2026-10-07T14:29:01Z" for t in result["timestamps"])
    assert "digicert (2026-10-07T14:29:01Z)" in result["email"]
    assert sa.run_summary(result["out_dir"])["timestamps"][0] == ("digicert", "2026-10-07T14:29:01Z")
    assert sa.verify_run(result["out_dir"]) == []


def test_one_authority_down_does_not_lose_the_others(tmp_path, monkeypatch):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})

    def flaky(request, timeout):
        if "digicert" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)
        if "sectigo" in request.full_url:
            raise TimeoutError()
        return FakeResponse(stamped_response(request.data))

    monkeypatch.setattr(sa.urllib.request, "urlopen", flaky)
    result, _ = only(tmp_path / "r", tmp_path, "p", timestamp=True)
    outcome = {t["tsa"]: t for t in result["timestamps"]}
    assert [outcome[n]["ok"] for n in ("digicert", "sectigo", "freetsa")] == [False, False, True]
    assert "503" in outcome["digicert"]["error"] and outcome["sectigo"]["error"] == "TimeoutError"
    assert "collected from: freetsa (2026" in result["email"]
    assert sorted(p.name for p in (result["out_dir"] / "timestamps").glob("*.tsr")) == ["freetsa.tsr"]
    assert sa.verify_run(result["out_dir"]) == []


def test_oversized_timestamp_response_is_not_read_without_limit(tmp_path, monkeypatch):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})
    asked = []

    class Endless(FakeResponse):
        def read(self, limit=-1):
            asked.append(limit)
            return b"<html>" + b"x" * 100

    monkeypatch.setattr(sa.urllib.request, "urlopen", lambda request, timeout: Endless(b""))
    result, _ = only(tmp_path / "r", tmp_path, "p", timestamp=True)
    assert asked == [sa.TSA_MAX_RESPONSE] * len(sa.TSAS)
    assert not any(t["ok"] for t in result["timestamps"])


# ------------------------------------------------------------- command line

def test_cli_handles_names_the_console_encoding_cannot_show(tmp_path):
    name = "日本語プロジェクト"
    write(tmp_path / "r" / name, {"f.txt": "f\n"})
    script = Path(sa.__file__)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    done = subprocess.run([sys.executable, str(script), "--root", str(tmp_path / "r"),
                           "--select", "1", "--out", str(tmp_path / "o")],
                          capture_output=True, env=env)
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    (run_dir,) = (tmp_path / "o").iterdir()
    saved = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    assert saved["projects"][0]["name"] == name
    assert sa.verify_run(run_dir, against_disk=True) == []


def test_cli_select_by_unicode_name(tmp_path):
    name = "café-日本"
    write(tmp_path / "r" / name, {"f.txt": "f\n"})
    write(tmp_path / "r" / "other", {"g.txt": "g\n"})
    done = cli("--root", tmp_path / "r", "--select", name, "--out", tmp_path / "o")
    assert done.returncode == 0, done.stderr
    (run_dir,) = (tmp_path / "o").iterdir()
    saved = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    assert [p["name"] for p in saved["projects"]] == [name]


def test_cli_prompt_reads_the_selection_from_stdin(tmp_path):
    write(tmp_path / "r" / "a", {"f.txt": "f\n"})
    write(tmp_path / "r" / "b", {"g.txt": "g\n"})
    script = Path(sa.__file__)
    base = [sys.executable, str(script), "--root", str(tmp_path / "r"), "--out", str(tmp_path / "o")]
    done = subprocess.run(base, input="2\n", capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    (run_dir,) = (tmp_path / "o").iterdir()
    assert [p["name"] for p in json.loads((run_dir / "evidence.json").read_text())["projects"]] == ["b"]

    closed = subprocess.run(base, input="", capture_output=True, text=True)
    assert closed.returncode != 0 and "nothing selected" in closed.stderr
    blank = subprocess.run(base, input="\n", capture_output=True, text=True)
    assert blank.returncode != 0 and "nothing selected" in blank.stderr


def test_cli_error_paths_exit_cleanly_with_a_message(tmp_path):
    (tmp_path / "empty").mkdir()
    cases = [
        (("--root", tmp_path / "missing", "--list"), "not a folder"),
        (("--root", tmp_path / "empty", "--list"), "no projects found"),
        (("--root", tmp_path / "empty", "--add", tmp_path / "nope", "--list"), "not a folder"),
    ]
    for args, message in cases:
        done = cli(*args)
        assert done.returncode != 0 and message in done.stderr and "Traceback" not in done.stderr

    missing = cli("--verify", tmp_path / "missing")
    assert missing.returncode == 1 and "unreadable" in missing.stdout and "Traceback" not in missing.stderr


def test_cli_warnings_are_printed(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {"f.txt": "f\n"})
    (project / ".git").mkdir()  # unusable repository
    done = cli("--root", tmp_path / "r", "--select", "all", "--out", tmp_path / "o")
    assert done.returncode == 0, done.stderr
    assert "GIT ERROR" in done.stdout and "WARNING(S)" in done.stdout


def test_cli_verify_with_expected_hash(tmp_path):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})
    made = cli("--root", tmp_path / "r", "--select", "all", "--out", tmp_path / "o")
    assert made.returncode == 0, made.stderr
    (run_dir,) = (tmp_path / "o").iterdir()
    bundle = (run_dir / "BUNDLE_SHA256.txt").read_text().split()[0]

    right = cli("--verify", run_dir, "--expect", bundle)
    assert right.returncode == 0 and f"Bundle SHA-256: {bundle}" in right.stdout
    wrong = cli("--verify", run_dir, "--expect", "0" * 64)
    assert wrong.returncode == 1 and "not the expected" in wrong.stdout
    plain = cli("--verify", run_dir)
    assert plain.returncode == 0 and "Compare the bundle hash" in plain.stdout


def test_cli_relative_paths_from_another_directory(tmp_path):
    write(tmp_path / "r" / "p", {"f.txt": "f\n"})
    script = Path(sa.__file__)
    done = subprocess.run([sys.executable, str(script), "--root", "r", "--select", "all", "--out", "o"],
                          cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    (run_dir,) = (tmp_path / "o").iterdir()
    assert sa.verify_run(run_dir, against_disk=True) == []


# ----------------------------------------------------- other tools can read it

def test_snapshot_zip_is_clean_for_info_zip_and_checkable_with_sha256sum(tmp_path):
    if not (shutil.which("unzip") and shutil.which("sha256sum")):
        pytest.skip("unzip and sha256sum not installed")
    files = {"a.txt": "hello\n", "empty.txt": "", "sub dir/b \u00e9.txt": "caf\u00e9\n",
             "\u65e5\u672c/\u8a9e.txt": "x\n"}
    write(tmp_path / "r" / "p", files)
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    archive = result["out_dir"] / entry["snapshot"]

    tested = subprocess.run(["unzip", "-tq", str(archive)], capture_output=True, text=True,
                            errors="replace")
    assert tested.returncode == 0, tested.stdout + tested.stderr

    restored = tmp_path / "restored"
    with zipfile.ZipFile(archive) as z:
        z.extractall(restored)
    checked = subprocess.run(["sha256sum", "-c", str(result["out_dir"] / entry["manifest"])],
                             cwd=restored, capture_output=True, text=True, encoding="utf-8")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.count(": OK") == len(files)


def test_a_later_file_is_still_hashed_after_the_snapshot_breaks(tmp_path, monkeypatch):
    project = tmp_path / "r" / "p"
    write(project, {"a.txt": "a\n", "m.bin": b"x" * (2 << 20), "z1.txt": "1\n", "z2.txt": "2\n"})
    real_open = open

    class Fails:
        def __init__(self, handle):
            self.handle, self.reads = handle, 0

        def read(self, size=-1):
            self.reads += 1
            if self.reads > 1:
                raise OSError(5, "Input/output error")
            return self.handle.read(size)

        def close(self):
            self.handle.close()

    monkeypatch.setattr(sa, "open", lambda path, mode="r", *a, **k: (
        Fails(real_open(path, mode, *a, **k)) if str(path).endswith("m.bin")
        else real_open(path, mode, *a, **k)), raising=False)
    result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
    monkeypatch.undo()
    assert list(manifest(result, entry)) == ["a.txt", "z1.txt", "z2.txt"]
    assert entry["file_count"] == 3 and "snapshot" not in entry


@pytest.mark.skipif(not os.environ.get("PRIORFOLIO_SLOW"),
                    reason="set PRIORFOLIO_SLOW=1 to hash a 2.2 GB file (needs the disk space)")
def test_file_larger_than_two_gigabytes(tmp_path):
    project = tmp_path / "r" / "p"
    write(project, {"small.txt": "s\n"})
    size, block = 2_200_000_000, b"\0" * (1 << 20)
    expected = hashlib.sha256()
    with open(project / "huge.bin", "wb") as f:
        written = 0
        while written < size:
            piece = block[: min(len(block), size - written)]
            f.write(piece)
            expected.update(piece)
            written += len(piece)
    try:
        result, entry = only(tmp_path / "r", tmp_path, "p", snapshot=True)
        assert entry["warnings"] == [] and entry["total_bytes"] == size + 2
        assert manifest(result, entry)["huge.bin"] == expected.hexdigest()
        with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
            assert z.getinfo("huge.bin").file_size == size
        assert sa.verify_run(result["out_dir"], against_disk=True) == []
    finally:
        (project / "huge.bin").unlink()
