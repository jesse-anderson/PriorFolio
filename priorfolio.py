#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Jesse Anderson
"""PriorFolio: prepare IP disclosures and preserve supporting evidence.

Scans folders of projects, lets you pick which ones to include, and for each
one records the git commit (hash, date, tree) plus a SHA-256 content hash of
the files actually on disk. Read-only with respect to the scanned projects:
it never commits, stages, fetches or writes inside them.

Content hash method (reproducible by hand):
  1. List the project's files: everything git tracks, plus untracked files
     that are not gitignored. Dependency/build/venv folders are skipped, and
     links to other folders are not followed. Everything left out is recorded.
  2. SHA-256 each file's raw bytes.
  3. Write one line per file, "<sha256>  <relative/path>\\n", sorted by path
     (by Unicode code point). This is the manifest, in `sha256sum -c` format.
  4. The content hash is the SHA-256 of the manifest's UTF-8 bytes.

Standard library only. Requires Python 3.10+ and git on PATH.
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
import zipfile
from pathlib import Path, PureWindowsPath

VERSION = "0.1.0a3"

# RFC 3161 timestamp authorities. Only the SHA-256 of evidence.json is sent.
TSAS = [
    ("digicert", "http://timestamp.digicert.com"),
    ("sectigo", "http://timestamp.sectigo.com"),
    ("freetsa", "https://freetsa.org/tsr"),
]
TSA_MAX_RESPONSE = 1 << 20

# Untracked files under these folders are never hashed (tracked files always are).
SKIP_DIRS = {
    ".git", "node_modules", "target", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".quarto",
    ".build-local", ".idea", ".vs", ".gradle", ".next", ".cache",
}
SKIP_SUFFIXES = (".pyc", ".pyo")
NESTED_REPO_DEPTH = 3

# Settings that keep git read-only, complete and safe on folders we did not create:
# no index refresh, no 260-character path limit, no repo-configured helper program.
GIT_BASE = ["git", "--no-optional-locks", "-c", "core.quotepath=off",
            "-c", "core.longpaths=true", "-c", "core.fsmonitor=false"]
GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CEILING_DIRECTORIES")
REPARSE_LINK_TAGS = (0xA0000003, 0xA000000C)  # Windows junction, Windows symlink


class ToolError(Exception):
    """A problem shown to the user as a plain message, without a traceback."""


def require_git():
    if not shutil.which("git"):
        raise ToolError("git was not found. Install Git and make sure it is on PATH.")


def fs_path(path):
    """A path string that still works past Windows' 260-character limit."""
    text = os.path.abspath(str(path))
    if os.name == "nt" and not text.startswith("\\\\"):
        return "\\\\?\\" + text
    return text


def git(cwd, *args, git_dir=None):
    """Run git. Returns (exit code, stdout, stderr)."""
    cmd = list(GIT_BASE)
    if git_dir:
        cmd += ["--git-dir", str(git_dir), "--work-tree", str(cwd)]
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run(cmd + list(args), cwd=cwd, env=env, capture_output=True,
                       creationflags=flags)
    return (r.returncode, r.stdout.decode("utf-8", "replace").strip("\n"),
            r.stderr.decode("utf-8", "replace").strip())


def first_line(text):
    return text.splitlines()[0] if text else "unknown error"


def is_repo(path):
    return (Path(path) / ".git").exists()


def repo_problem(path):
    """Why git cannot use this folder as a repository, or None if it can."""
    rc, top, err = git(path, "rev-parse", "--show-toplevel")
    if rc != 0:
        return first_line(err).removeprefix("fatal: ")
    try:
        same = os.path.samefile(top, path)
    except OSError:
        same = False
    # A broken .git makes git fall through to whatever repo encloses the folder.
    return None if same else "its .git folder is not a usable repository"


# Repository settings that can make the git commands PriorFolio runs start another
# program. `git status` runs clean filters, and included files can define them.
RISKY_GIT_CONFIG = r"^(filter\..+\.(clean|process)|include\.path|includeif\..+\.path)$"


def risky_git_config(path):
    """Names of settings in a repository's own config that could run programs."""
    rc, out, _ = git(path, "config", "--local", "--get-regexp", RISKY_GIT_CONFIG)
    if rc != 0:  # 1 means none were found
        return []
    return sorted({line.split(" ", 1)[0] for line in out.splitlines() if line.strip()})


def strip_credentials(url):
    """Keep a useful remote identifier without authentication or query data."""
    if "://" in url:
        try:
            parts = urllib.parse.urlsplit(url)
            authority = parts.netloc.rsplit("@", 1)[-1]
            # Preserve conventional ssh://git@host URLs, never passwords.
            if parts.scheme.lower() == "ssh" and parts.username == "git" and parts.password is None:
                authority = "git@" + authority
            # Preserve the spelling of the scheme for existing reports.
            return url.split("://", 1)[0] + "://" + authority + parts.path
        except ValueError:
            return "[invalid remote URL omitted]"
    # SCP-style SSH remotes can reveal personal login names.
    clean = url.split("?", 1)[0].split("#", 1)[0]
    return re.sub(r"^[^/@]+@", "git@", clean)


def safe_child(base, relative):
    """Resolve an untrusted relative path without following links outside base."""
    if (not isinstance(relative, str) or not relative or "\0" in relative
            or PureWindowsPath(relative).drive or relative.startswith(("/", "\\"))
            or ".." in re.split(r"[/\\]", relative)):
        raise ValueError("unsafe relative path")
    root = Path(base).resolve()
    candidate = root / relative
    # Reject links even if they currently point inside the root.
    current = root
    for part in Path(relative).parts:
        current = current / part
        if is_link_dir(current):
            raise ValueError("links are not allowed in evidence paths")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("path leaves its expected folder")
    return candidate


def hash_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


def is_link_dir(path):
    """True for a symlink or Windows junction, which may point outside the project."""
    try:
        st = os.lstat(fs_path(path))
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in REPARSE_LINK_TAGS


def find_nested_repos(top, depth=NESTED_REPO_DEPTH):
    found = []

    def walk(d, level):
        if level > depth:
            return
        try:
            children = sorted(c for c in d.iterdir() if c.is_dir() and not is_link_dir(c))
        except OSError:
            return
        for c in children:
            if c.name in SKIP_DIRS or (c / "pyvenv.cfg").exists():
                continue
            if is_repo(c):
                found.append(c)
            else:
                walk(c, level + 1)

    walk(top, 1)
    return found


def discover(root):
    """Top-level folders, plus repos nested inside folders that aren't repos."""
    projects = []
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or is_link_dir(d):
            continue
        projects.append(d)
        if not is_repo(d):
            projects.extend(find_nested_repos(d))
    return projects


def describe(path, root):
    """Cheap status for the selection list. No hashing."""
    path, root = Path(path), Path(root)
    try:
        name = path.relative_to(root).as_posix()
    except ValueError:
        name = path.name
    info = {
        "name": name if name not in ("", ".") else (path.name or str(path)),
        "path": str(path),
        "root": str(root),
        "is_repo": is_repo(path),
        "git_error": None,
        "branch": None, "commit": None, "commit_date": None, "tree": None,
        "remote": None, "modified": 0, "untracked": 0, "unpushed": None,
        "config_warning": None,
    }
    if not path.is_dir():
        info.update(is_repo=False, state="FOLDER NOT FOUND")
        return info
    if not info["is_repo"]:
        info["state"] = "NOT A GIT REPO"
        return info

    problem = repo_problem(path)
    if problem:
        info["git_error"] = problem
        info["state"] = f"GIT ERROR: {problem}"
        return info

    rc, head, _ = git(path, "rev-parse", "HEAD")
    if rc == 0:
        info["commit"] = head
        info["commit_date"] = git(path, "log", "-1", "--format=%cI")[1]
        info["tree"] = git(path, "rev-parse", "HEAD^{tree}")[1]
        branch = git(path, "rev-parse", "--abbrev-ref", "HEAD")[1]
        info["branch"] = "(detached HEAD)" if branch == "HEAD" else branch

    remotes = git(path, "remote")[1].split()
    if remotes:
        chosen = "origin" if "origin" in remotes else remotes[0]
        rc, url, _ = git(path, "remote", "get-url", chosen)
        if rc == 0 and url:
            info["remote"] = strip_credentials(url)

    notes = []
    risky = risky_git_config(path)
    if risky:
        # Do not let a folder we were only asked to read start its own programs.
        info["config_warning"] = ", ".join(risky)
        notes.append(f"repo config can run programs ({info['config_warning']}); git status skipped")
    else:
        rc, status, err = git(path, "status", "--porcelain")
        if rc != 0:
            info["git_error"] = first_line(err).removeprefix("fatal: ")
            info["state"] = f"GIT ERROR: {info['git_error']}"
            return info
        status = status.splitlines()
        info["untracked"] = sum(1 for s in status if s.startswith("??"))
        info["modified"] = len(status) - info["untracked"]

    if not info["commit"]:
        notes.append("NO COMMITS")
    if info["modified"] or info["untracked"]:
        notes.append(f"uncommitted: {info['modified']} modified, {info['untracked']} untracked")
    if not info["remote"]:
        notes.append("no remote")
    elif info["commit"]:
        rc, count, _ = git(path, "rev-list", "--count", "--branches", "--not", "--remotes")
        info["unpushed"] = int(count) if rc == 0 and count.isdigit() else 0
        if info["unpushed"]:
            notes.append(f"{info['unpushed']} unpushed commit{'s' if info['unpushed'] != 1 else ''}")
    info["state"] = "; ".join(notes) if notes else "clean, pushed"
    return info


def scan_roots(roots, singles=(), log=None):
    """Projects under each root folder. A root that is itself a git repo, and
    every folder in `singles`, counts as one project rather than a container."""
    require_git()
    pairs = []
    for root in roots:
        root = Path(root).resolve()
        if is_repo(root):
            pairs.append((root, root.parent))
        else:
            pairs.extend((path, root) for path in discover(root))
    pairs.extend((Path(s).resolve(), Path(s).resolve().parent) for s in singles)

    found, seen = [], set()
    for path, base in pairs:
        if path in seen:
            continue
        seen.add(path)
        if log:
            log(f"Scanning {len(found) + 1}/{len(pairs)}: {path.name}")
        found.append(describe(path, base))

    # Same project name under two roots: say which root each came from.
    counts = collections.Counter(p["name"].lower() for p in found)
    for p in found:
        if counts[p["name"].lower()] > 1:
            p["name"] = f"{Path(p['root']).name}/{p['name']}"
    # Roots can share a folder name too, so make every name unique for certain.
    taken = set()
    for p in found:
        base, n = p["name"], 1
        while p["name"].lower() in taken:
            n += 1
            p["name"] = f"{base} ({n})"
        taken.add(p["name"].lower())
    return found


def list_files(path, scratch_git_dir, as_repo, exclude=None):
    """Files to hash. Returns (files, notes, warnings); paths are posix-style
    and relative to the project. Notes record what was deliberately left out,
    warnings record anything that may have been missed."""
    notes, warnings = [], []

    def run(*args, **kwargs):
        rc, out, err = git(path, "ls-files", "-z", *args, **kwargs)
        for line in err.splitlines():
            if "could not open directory" in line:
                warnings.append("git could not open a folder: " + line.removeprefix("warning: "))
        return rc, [f for f in out.split("\0") if f], err

    tracked, others, rc = [], [], 1
    if as_repo:
        rc, tracked, err = run("--cached")
        if rc == 0:
            rc, others, err = run("--others", "--exclude-standard")
        if rc != 0:
            warnings.append(f"git could not list this repository ({first_line(err)}); "
                            "it was hashed as a plain folder instead")
            tracked = []
    if rc != 0:
        # Borrow an empty git dir so the folder's own .gitignore files still apply.
        rc, others, err = run("--others", "--exclude-standard", git_dir=scratch_git_dir)
        if rc != 0:
            warnings.append(f"could not list files: {first_line(err)}")

    out_prefix = None
    if exclude is not None:
        try:
            out_prefix = Path(exclude).resolve().relative_to(Path(path).resolve()).as_posix() + "/"
        except ValueError:
            pass

    link_cache = {}

    def linked_parent(rel):
        prefix = ""
        for part in rel.split("/")[:-1]:
            prefix += part + "/"
            if prefix not in link_cache:
                link_cache[prefix] = is_link_dir(Path(path) / prefix)
            if link_cache[prefix]:
                return prefix
        return None

    def skip_dir(rel):
        parts = rel.rstrip("/").split("/")
        for i, part in enumerate(parts[:-1] if not rel.endswith("/") else parts):
            if part in SKIP_DIRS:
                return "/".join(parts[:i + 1]) + "/"
        return None

    venvs = [f[: -len("pyvenv.cfg")] for f in others if f.endswith("/pyvenv.cfg")]
    left_out = collections.Counter()  # (reason, folder) -> number of files
    keep = set()
    for rel, is_tracked in [(f, True) for f in tracked] + [(f, False) for f in others]:
        if out_prefix and rel.startswith(out_prefix):
            left_out[("this tool's output folder not included", out_prefix)] += 1
            continue
        link = linked_parent(rel)
        if link:
            left_out[("link to another folder not followed", link)] += 1
            continue
        if not is_tracked:
            folder = skip_dir(rel)
            venv = next((v for v in venvs if rel.startswith(v)), None)
            if folder:
                left_out[("folder not included", folder)] += 1
                continue
            if venv:
                left_out[("virtualenv not included", venv)] += 1
                continue
            if rel.endswith("/"):
                notes.append(f"nested repo not included: {rel}")
                continue
            if rel.endswith(SKIP_SUFFIXES):
                left_out[("compiled Python files not included", "")] += 1
                continue
        keep.add(rel)

    for (reason, folder), count in sorted(left_out.items()):
        plural = "file" if count == 1 else "files"
        notes.append(f"{reason}: {folder} ({count} {plural})" if folder
                     else f"{reason}: {count} {plural}")
    return sorted(keep), notes, warnings


def sha256_file(fp):
    h = hashlib.sha256()
    with open(fs_path(fp), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_line(digest, rel):
    """One manifest line. Odd names are escaped the way sha256sum does it."""
    if "\\" in rel or "\n" in rel or "\r" in rel:
        escaped = rel.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
        return f"\\{digest}  {escaped}\n"
    return f"{digest}  {rel}\n"


def read_manifest(data):
    """{relative path: sha256} from the bytes of a manifest file."""
    unescape = {"n": "\n", "r": "\r", "\\": "\\"}
    entries = {}
    for line in data.decode("utf-8").split("\n"):
        if not line:
            continue
        escaped = line.startswith("\\")
        digest, _, rel = line[escaped:].partition("  ")
        if escaped:
            rel = re.sub(r"\\(.)", lambda m: unescape.get(m.group(1), m.group(1)), rel)
        entries[rel] = digest
    return entries


def zip_entry(rel, mtime):
    """Zip header for one file. Zip dates only cover 1980 to 2107."""
    stamp = time.localtime(mtime)[:6]
    stamp = max((1980, 1, 1, 0, 0, 0), min(stamp, (2107, 12, 31, 23, 59, 58)))
    entry = zipfile.ZipInfo(rel, date_time=stamp)
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o644 << 16
    return entry


def hash_project(info, scratch_git_dir, out_dir, stem, snapshot, exclude=None):
    """Hash one project into a manifest (and optionally a zip of the same bytes)."""
    path = Path(info["path"])
    if os.path.isdir(fs_path(path)):
        files, notes, warnings = list_files(
            path, scratch_git_dir, info["is_repo"] and not info["git_error"], exclude)
    else:
        files, notes, warnings = [], [], ["folder no longer exists; nothing was hashed"]
    if info["git_error"]:
        warnings.insert(0, f"git could not read this repository ({info['git_error']}); "
                           "no commit recorded, hashed as a plain folder")
    if info.get("config_warning"):
        notes.insert(0, f"repository config can run programs ({info['config_warning']}); "
                        "git status was not run, so uncommitted changes were not counted")

    zip_path = out_dir / "snapshots" / f"{stem}.zip"
    archive = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) if snapshot and files else None
    lines, total_bytes, newest, snapshot_broken = [], 0, 0.0, False
    try:
        for rel in files:
            full = fs_path(path / rel)
            try:
                st = os.lstat(full)
            except FileNotFoundError:
                notes.append(f"listed by git but not on disk: {rel}")
                continue
            except OSError as e:
                warnings.append(f"could not be read: {rel} ({e.strerror})")
                continue
            if stat.S_ISLNK(st.st_mode):
                notes.append(f"symbolic link not followed: {rel}")
                continue
            if stat.S_ISDIR(st.st_mode):
                notes.append(f"submodule or nested repo not included: {rel}/")
                continue
            if not stat.S_ISREG(st.st_mode):
                notes.append(f"not a regular file, skipped: {rel}")
                continue
            try:
                source = open(full, "rb")
            except OSError as e:
                warnings.append(f"could not be read: {rel} ({e.strerror})")
                continue

            # One pass feeds both the hash and the zip, so they cannot disagree.
            # Zip64 headers are only used where needed: forcing them on every
            # entry makes Info-ZIP's unzip warn about non-ASCII file names.
            digest, size, failure, target = hashlib.sha256(), 0, None, None
            try:
                if archive and not snapshot_broken:
                    target = archive.open(zip_entry(rel, st.st_mtime), "w",
                                          force_zip64=st.st_size > 0x7FFF0000)
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    if target:
                        target.write(chunk)
                if target:
                    target.close()
                    target = None
            except (OSError, RuntimeError, ValueError, zipfile.LargeZipFile) as e:
                failure = getattr(e, "strerror", None) or str(e)
            finally:
                source.close()
                if target:  # left open by a failure; the zip is discarded below
                    try:
                        target.close()
                    except Exception:
                        pass
            if failure:
                warnings.append(f"could not be read completely: {rel} ({failure})")
                snapshot_broken = snapshot_broken or bool(archive)
                continue
            lines.append(manifest_line(digest.hexdigest(), rel))
            total_bytes += size
            newest = max(newest, st.st_mtime)
    finally:
        if archive:
            archive.close()

    manifest = "".join(lines).encode("utf-8")
    manifest_path = out_dir / "manifests" / f"{stem}.sha256"
    manifest_path.write_bytes(manifest)

    info.update({
        "content_sha256": hashlib.sha256(manifest).hexdigest() if lines else None,
        "file_count": len(lines),
        "total_bytes": total_bytes,
        "newest_file_mtime": (
            datetime.datetime.fromtimestamp(newest, datetime.timezone.utc)
            .isoformat(timespec="seconds") if newest else None),
        "manifest": manifest_path.relative_to(out_dir).as_posix(),
        "notes": notes,
        "warnings": warnings,
    })

    if archive:
        if snapshot_broken or not lines:
            zip_path.unlink()
            if snapshot_broken:
                warnings.append("snapshot zip not written because a file could not be read completely")
        else:
            info["snapshot"] = zip_path.relative_to(out_dir).as_posix()
            info["snapshot_sha256"] = sha256_file(zip_path)
    return info


def parse_selection(text, projects):
    text = text.strip()
    if text.lower() in ("all", "*"):
        return list(projects)
    by_name = {p["name"].lower(): p for p in projects}
    chosen = []
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if token.lower() in by_name:  # a project literally named "2024" wins over row 2024
            picks = [by_name[token.lower()]]
        elif m:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            if not (1 <= lo <= hi <= len(projects)):
                raise ValueError(f"out of range: {token}")
            picks = projects[lo - 1:hi]
        else:
            raise ValueError(f"no such project: {token}")
        chosen.extend(p for p in picks if p not in chosen)
    return chosen


def print_list(projects):
    width = min(max(len(p["name"]) for p in projects), 52)
    for i, p in enumerate(projects, 1):
        commit = (p["commit"] or "-")[:10]
        date = (p["commit_date"] or "-")[:10]
        print(f"{i:>3}  {p['name']:<{width}}  {commit:<10}  {date:<10}  {p['state']}")


def write_markdown(results, meta, path):
    cell = lambda text: str(text).replace("|", "\\|")
    out = [
        "# PriorFolio - Private Evidence Record",
        "",
        f"- Generated (UTC): {meta['generated_utc']}",
        "- Scanned folders: " + ", ".join(f"`{r}`" for r in meta["roots"]),
        f"- Tool: PriorFolio {VERSION}",
        "- Content hash: SHA-256 of a manifest listing the SHA-256 of every "
        "tracked and non-ignored file on disk, sorted by path.",
        "",
        "| # | Project | Commit | Commit date | Content SHA-256 | Files |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        out.append(
            f"| {i} | {cell(r['name'])} | {r['commit'] or 'none'} | "
            f"{r['commit_date'] or 'n/a'} | {r['content_sha256'] or 'no files'} | "
            f"{r['file_count']} |")
    out.append("")
    for i, r in enumerate(results, 1):
        out += [
            f"## {i}. {r['name']}",
            "",
            f"- Remote: {r['remote'] or 'none'}",
            f"- Branch: {r['branch'] or 'n/a'}",
            f"- Commit: {r['commit'] or 'none'}",
            f"- Commit date: {r['commit_date'] or 'n/a'}",
            f"- Git tree: {r['tree'] or 'n/a'}",
            f"- Working tree state: {r['state']}",
            f"- Content SHA-256: {r['content_sha256'] or 'no files'}",
            f"- Files hashed: {r['file_count']} ({r['total_bytes']:,} bytes)",
            f"- Newest file modified (UTC): {r['newest_file_mtime'] or 'n/a'}",
        ]
        if r.get("snapshot_sha256"):
            out.append(f"- Snapshot zip SHA-256: {r['snapshot_sha256']}")
        out += [f"- WARNING: {w}" for w in r["warnings"][:20]]
        if len(r["warnings"]) > 20:
            out.append(f"- WARNING: {len(r['warnings']) - 20} more warnings in the JSON file")
        out += [f"- Note: {n}" for n in r["notes"][:20]]
        if len(r["notes"]) > 20:
            out.append(f"- Note: {len(r['notes']) - 20} more notes in the JSON file")
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8", newline="\n")


def timestamp_request(digest):
    """DER-encoded RFC 3161 TimeStampReq for a SHA-256 digest, with a nonce."""
    nonce = bytearray(os.urandom(8))
    nonce[0] = (nonce[0] & 0x7F) | 0x40  # positive, minimally encoded INTEGER
    imprint = bytes.fromhex("300d06096086480165030402010500") + b"\x04\x20" + digest
    body = (b"\x02\x01\x01" + b"\x30" + bytes([len(imprint)]) + imprint
            + b"\x02\x08" + bytes(nonce) + b"\x01\x01\xff")
    return b"\x30" + bytes([len(body)]) + body, bytes(nonce)


def timestamp_status(resp):
    """PKIStatus from a TimeStampResp: 0 = granted, 1 = granted with mods."""
    def header(i):
        tag, n, i = resp[i], resp[i + 1], i + 2
        if n & 0x80:
            k = n & 0x7F
            n, i = int.from_bytes(resp[i:i + k], "big"), i + k
        return tag, n, i

    try:
        t1, _, i = header(0)
        t2, _, i = header(i)
        t3, n, i = header(i)
    except IndexError:
        raise ValueError("not a timestamp response") from None
    if (t1, t2, t3) != (0x30, 0x30, 0x02) or n < 1 or i + n > len(resp):
        raise ValueError("not a timestamp response")
    return int.from_bytes(resp[i:i + n], "big")


def der_items(data, start=0, end=None):
    """(tag, element start, content start, content end) for each DER element in a range."""
    end = len(data) if end is None else end
    items, i = [], start
    while i < end:
        tag, n, j = data[i], data[i + 1], i + 2
        if n & 0x80:
            k = n & 0x7F
            if not 1 <= k <= 4:
                raise ValueError("unsupported DER length")
            n, j = int.from_bytes(data[j:j + k], "big"), j + k
        if j + n > end:
            raise ValueError("truncated DER element")
        items.append((tag, i, j, j + n))
        i = j + n
    return items


def der_child(data, item, tag, index):
    """The index-th element inside item, which must carry the given tag."""
    children = der_items(data, item[2], item[3])
    if index >= len(children) or children[index][0] != tag:
        raise ValueError("unexpected timestamp structure")
    return children[index]


SHA256_OID = bytes.fromhex("608648016503040201")


def timestamp_token(resp):
    """The CMS SignedData token and its signed TSTInfo bytes, from a TimeStampResp."""
    try:
        outer = der_items(resp)[0]
        token = der_child(resp, outer, 0x30, 1)
        token_bytes = resp[token[1]:token[3]]
        signed_data = der_child(token_bytes, der_child(token_bytes, der_items(token_bytes)[0], 0xA0, 1), 0x30, 0)
        encapsulated = der_child(token_bytes, signed_data, 0x30, 2)
        content = der_child(token_bytes, der_child(token_bytes, encapsulated, 0xA0, 1), 0x04, 0)
    except IndexError:
        raise ValueError("not a timestamp token") from None
    return token_bytes, token_bytes[content[2]:content[3]]


def parse_tst_info(tst):
    """The hash algorithm, hash and time recorded in a TSTInfo."""
    try:
        info = der_items(tst)[0]
        imprint = der_child(tst, info, 0x30, 2)
        algorithm = der_child(tst, der_child(tst, imprint, 0x30, 0), 0x06, 0)
        hashed = der_child(tst, imprint, 0x04, 1)
        when = der_child(tst, info, 0x18, 4)
    except IndexError:
        raise ValueError("not a timestamp token") from None
    text = tst[when[2]:when[3]].decode("ascii")
    m = re.fullmatch(r"(\d{14})(?:\.\d+)?Z", text)
    if not m:
        raise ValueError("unreadable timestamp time")
    stamp = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=datetime.timezone.utc)
    return {"sha256": tst[algorithm[2]:algorithm[3]] == SHA256_OID,
            "digest": tst[hashed[2]:hashed[3]],
            "time": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "epoch": int(stamp.timestamp())}


_OPENSSL = []


def find_openssl():
    """An OpenSSL executable that can verify CMS signatures against pinned roots, or None."""
    if not _OPENSSL:
        _OPENSSL.append(_locate_openssl())
    return _OPENSSL[0]


def _locate_openssl():
    candidates = [shutil.which("openssl")]
    git_path = shutil.which("git")
    if os.name == "nt" and git_path:
        # Git for Windows includes OpenSSL beside its own tools.
        for folder in Path(git_path).resolve().parents[:3]:
            candidates += [folder / "mingw64" / "bin" / "openssl.exe",
                           folder / "usr" / "bin" / "openssl.exe", folder / "bin" / "openssl.exe"]
    if sys.platform == "darwin":  # the system LibreSSL cannot verify CMS
        candidates += ["/opt/homebrew/bin/openssl", "/opt/homebrew/opt/openssl@3/bin/openssl",
                       "/usr/local/opt/openssl@3/bin/openssl", "/usr/local/bin/openssl"]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            probe = subprocess.run([str(candidate), "cms", "-help"], capture_output=True,
                                   timeout=30, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            continue
        usage = probe.stdout + probe.stderr
        if all(option in usage for option in (b"-attime", b"-purpose", b"-no-CApath", b"-no-CAstore")):
            return str(candidate)
    return None


def check_timestamp_signature(resp, digest):
    """Check that a timestamp response is signed for this digest by a pinned authority.

    Returns (state, time, detail). state is "verified", "failed", or "unchecked"
    when no suitable OpenSSL is available. The certificate chain is checked at
    the time inside the token, so a token stays checkable after its
    certificates expire. Only the roots in TSA_ROOTS are trusted.
    """
    openssl = find_openssl()
    if not openssl:
        return "unchecked", None, "OpenSSL 3 was not found, so the signature was not checked"
    try:
        token, tst = timestamp_token(resp)
        info = parse_tst_info(tst)
    except (ValueError, UnicodeDecodeError) as e:
        return "failed", None, f"the token is malformed ({e})"
    if not info["sha256"] or info["digest"] != digest:
        return "failed", info["time"], "the signed token is for a different evidence.json"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="priorfolio-tsa-") as folder:
        folder = Path(folder)
        (folder / "token.der").write_bytes(token)
        (folder / "roots.pem").write_text(TSA_ROOTS, encoding="ascii", newline="\n")
        try:
            done = subprocess.run(
                [openssl, "cms", "-verify", "-inform", "DER", "-in", str(folder / "token.der"),
                 "-binary", "-CAfile", str(folder / "roots.pem"), "-no-CApath", "-no-CAstore",
                 "-purpose", "timestampsign", "-attime", str(info["epoch"]),
                 "-out", str(folder / "signed.der")],
                capture_output=True, timeout=60, creationflags=flags)
        except (OSError, subprocess.SubprocessError) as e:
            return "unchecked", info["time"], f"OpenSSL could not be run ({e})"
        signed = (folder / "signed.der").read_bytes() if done.returncode == 0 else None
    if done.returncode != 0:
        errors = [line for line in done.stderr.decode("utf-8", "replace").splitlines()
                  if "error" in line.lower()]
        detail = errors[-1].rsplit(":", 1)[-1].strip() if errors else "verification failed"
        return "failed", info["time"], f"the signature does not verify ({detail})"
    if signed != tst:
        return "failed", info["time"], "the verified content differs from the token"
    return "verified", info["time"], "signed by a pinned timestamp authority"


def timestamp_time(resp):
    """The time the authority put in its response, as ISO 8601 UTC, or None.

    Read without checking the signature; check_timestamp_signature checks it.
    """
    m = re.search(rb"\x18[\x0f-\x17](\d{14})(?:\.\d+)?Z", resp)
    if not m:
        return None
    t = m.group(1).decode("ascii")
    return f"{t[0:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"


def timestamp_bundle(json_path, out_dir, log):
    """Ask each TSA to sign the hash of evidence.json. Returns one dict per TSA."""
    ts_dir = out_dir / "timestamps"
    ts_dir.mkdir(exist_ok=True)
    digest = bytes.fromhex(sha256_file(json_path))
    outcomes = []
    for name, url in TSAS:
        log(f"timestamping with {name} ...")
        outcome = {"tsa": name, "url": url, "ok": False, "signature_verified": False}
        try:
            query, nonce = timestamp_request(digest)
            req = urllib.request.Request(url, data=query, headers={
                "Content-Type": "application/timestamp-query",
                "User-Agent": f"PriorFolio/{VERSION}",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = r.read(TSA_MAX_RESPONSE)
            status = timestamp_status(resp)
            if status not in (0, 1):
                raise ValueError(f"request rejected (status {status})")
            if digest not in resp or nonce not in resp:
                raise ValueError("response does not match the request")
            state, signed_time, detail = check_timestamp_signature(resp, digest)
            if state == "failed":
                raise ValueError(detail)
            (ts_dir / f"{name}.tsq").write_bytes(query)
            (ts_dir / f"{name}.tsr").write_bytes(resp)
            outcome.update(ok=True, file=f"timestamps/{name}.tsr",
                           time=signed_time or timestamp_time(resp),
                           signature_verified=state == "verified", signature=detail)
        except Exception as e:  # network, HTTP or parse failure: record and move on
            outcome["error"] = str(e) or type(e).__name__
            log(f"  {name} failed: {outcome['error']}")
        outcomes.append(outcome)
    (ts_dir / "timestamps.json").write_text(json.dumps(outcomes, indent=2) + "\n",
                                            encoding="utf-8", newline="\n")
    return outcomes


def build_email(results, meta, bundle, timestamps, snapshot):
    date = meta["generated_utc"][:10]
    lines = [
        f"Subject: Record of pre-existing software - {date} - SHA-256 {bundle[:16]}",
        "To: [your personal address]",
        "Attach: the password-protected .7z of the backup ZIP saved with Export backup",
        "        (the cloud copy, if the run holds secrets; see BACKUP.txt)",
        "",
        f"This is my private backup of the work recorded on {date} (UTC).",
        "The attachment contains source snapshots and private project metadata.",
        "I have reviewed the disclosure descriptions and my contributions separately.",
        "",
        "SHA-256 of evidence.json (inside the private backup):",
        bundle,
        "",
        f"Projects ({len(results)}):",
        "",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['name']}")
        if r["commit"]:
            lines.append(f"   Commit: {r['commit']} ({r['commit_date']})")
        else:
            lines.append("   Commit: none (not under version control at this time)")
        lines.append(f"   Content SHA-256: {r['content_sha256'] or 'no files'}")
        lines.append(f"   Files: {r['file_count']}, newest modified "
                     f"{r['newest_file_mtime'] or 'n/a'}")
        if r["remote"]:
            lines.append(f"   Remote: {r['remote']}")
        if r["warnings"]:
            lines.append(f"   Incomplete: {len(r['warnings'])} warning(s), listed in evidence.json")
        lines.append("")
    lines += [
        "Method: each content hash is the SHA-256 of a manifest that lists the",
        "SHA-256 of every tracked and non-ignored file in the project, sorted by",
        "path. The manifests are in the attachment, and evidence.json records",
        "every value above, so the single hash at the top covers all of them.",
    ]
    def described(stamps):
        return ", ".join(f"{t['tsa']} ({t['time']})" if t.get("time") else t["tsa"] for t in stamps)

    verified = [t for t in timestamps if t["ok"] and t.get("signature_verified")]
    unchecked = [t for t in timestamps if t["ok"] and not t.get("signature_verified")]
    if verified:
        lines += ["", "Signed timestamps of the hash above, each verified against the "
                      "authority's root certificate: " + described(verified) + "."]
    if unchecked:
        lines += ["", "Timestamp responses whose signatures could NOT be checked "
                      "(OpenSSL 3 was not found): " + described(unchecked) + "."]
    if verified or unchecked:
        lines.append("The responses are in timestamps/. Re-check them with priorfolio --verify.")
    if snapshot:
        lines += [
            "",
            "The complete backup includes snapshots of the exact bytes hashed.",
            "Their SHA-256 hashes are recorded in evidence.json.",
        ]
    lines += ["", "BACKUP CHECK: open the received attachment from your mailbox, or confirm",
              "the ZIP has finished uploading in your cloud provider's web interface.",
              "Keep its SHA-256 alongside this email. A local export or a queued sync",
              "is not confirmation of a remote backup. Record confirmation in PriorFolio.",
              "For a recipient-facing disclosure, review disclosure.md or disclosure.zip.",
              "The metadata-only evidence_bundle.zip does not contain source snapshots."]
    return "\n".join(lines) + "\n"


def file_stems(names):
    """A distinct, filesystem-safe file name stem for each project name."""
    stems, taken = [], set()
    for name in names:
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.replace("/", "__")).rstrip(". ") or "project"
        stem, n = base, 1
        while stem.lower() in taken:
            n += 1
            stem = f"{base}-{n}"
        taken.add(stem.lower())
        stems.append(stem)
    return stems


def generate(chosen, out_base, snapshot=False, timestamp=False, log=print):
    """Hash the chosen projects and write a run folder. Returns a summary dict."""
    require_git()
    if not chosen:
        raise ToolError("No projects were selected.")
    out_base = Path(out_base).resolve()
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out_dir, attempt = out_base / stamp, 1
    while out_dir.exists():  # a second run within the same second
        attempt += 1
        out_dir = out_base / f"{stamp}-{attempt}"
    (out_dir / "manifests").mkdir(parents=True)
    if snapshot:
        (out_dir / "snapshots").mkdir()

    try:
        return _generate(chosen, out_base, out_dir, now, snapshot, timestamp, log)
    except BaseException:
        # Never leave a half-written folder that looks like a finished run.
        try:
            out_dir.rename(out_dir.with_name(out_dir.name + "-INCOMPLETE"))
        except OSError:
            pass
        raise


def _generate(chosen, out_base, out_dir, now, snapshot, timestamp, log):
    results = []
    stems = file_stems([p["name"] for p in chosen])
    with tempfile.TemporaryDirectory() as tmp:
        rc, _, err = git(tmp, "init", "-q")
        if rc != 0:
            raise ToolError(f"git could not create a scratch repository: {first_line(err)}")
        scratch_git_dir = Path(tmp) / ".git"
        for i, (old, stem) in enumerate(zip(chosen, stems), 1):
            log(f"[{i}/{len(chosen)}] hashing {old['name']} ...")
            info = describe(Path(old["path"]), Path(old["root"]))  # state may have changed
            info["name"] = old["name"]
            info["disclosure"] = {key: value for key, value in old.get("disclosure", {}).items()
                                  if key in ("title", "description", "contribution", "period", "references")
                                  and isinstance(value, str)}
            results.append(hash_project(info, scratch_git_dir, out_dir, stem, snapshot,
                                        exclude=out_base))

    meta = {
        "generated_utc": now.isoformat(timespec="seconds"),
        "roots": sorted({p["root"] for p in chosen}),
        "tool_version": VERSION,
    }
    json_path = out_dir / "evidence.json"
    json_path.write_text(json.dumps({**meta, "projects": results}, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    write_markdown(results, meta, out_dir / "evidence.md")

    bundle = sha256_file(json_path)
    (out_dir / "BUNDLE_SHA256.txt").write_text(f"{bundle}  evidence.json\n",
                                               encoding="utf-8", newline="\n")
    timestamps = timestamp_bundle(json_path, out_dir, log) if timestamp else []

    # Everything small enough to email, in one attachment. Snapshots stay out.
    with zipfile.ZipFile(out_dir / "evidence_bundle.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out_dir.rglob("*")):
            rel = f.relative_to(out_dir).as_posix()
            if f.is_file() and not rel.startswith("snapshots/") and rel != "evidence_bundle.zip":
                z.write(f, rel)

    email = build_email(results, meta, bundle, timestamps, snapshot)
    (out_dir / "email.txt").write_text(email, encoding="utf-8", newline="\n")
    from priorfolio_exports import write_disclosure, write_backup_instructions
    disclosure = write_disclosure(out_dir, results, meta, bundle)
    write_backup_instructions(out_dir)
    # Round trip: read back everything just written, as a later verification would.
    problems = verify_run(out_dir)
    if problems:
        raise ToolError("The new run did not pass its own verification: " + "; ".join(problems[:5]))
    return {
        "out_dir": out_dir, "bundle": bundle, "results": results,
        "timestamps": timestamps, "email": email,
        "markdown": (out_dir / "evidence.md").read_text(encoding="utf-8"),
        "disclosure": disclosure,
        "warnings": [f"{r['name']}: {w}" for r in results for w in r["warnings"]],
    }


def compare_hashes(expected, actual, report_extra=True):
    """Differences between two {path: sha256} dicts, as short messages."""
    found = []
    for rel, digest in expected.items():
        if rel not in actual:
            found.append(f"missing: {rel}")
        elif actual[rel] != digest:
            found.append(f"changed: {rel}")
    if report_extra:
        found += [f"not in manifest: {rel}" for rel in actual if rel not in expected]
    return found


def verify_run(run_dir, against_disk=False, expect_bundle=None, metadata_only=False):
    """Re-check a run folder. Returns a list of problems; empty means it all matches.

    Checks that BUNDLE_SHA256.txt matches evidence.json, that every manifest
    matches its recorded content hash, that every snapshot zip matches its
    recorded hash and holds exactly the files in its manifest, and that every
    timestamp response was issued for this evidence.json. With against_disk,
    also checks the project folders as they are now against the manifests
    (changed and missing files; files added since are not reported).

    A run folder is only self-consistent evidence: someone who rewrites it can
    rewrite BUNDLE_SHA256.txt too. Pass expect_bundle, the hash from your email
    or signed agreement, to tie it to something outside the folder.

    Timestamp signatures are checked against the pinned authority roots when
    OpenSSL 3 is available; without it they are reported by run_summary as
    unchecked rather than counted as problems.
    """
    run_dir = Path(run_dir).resolve()
    try:
        json_path = safe_child(run_dir, "evidence.json")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        projects = data["projects"]
        if not isinstance(projects, list):
            raise TypeError("'projects' is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"evidence.json is unreadable: {e}"]

    problems = []
    bundle = sha256_file(json_path)
    try:
        recorded = safe_child(run_dir, "BUNDLE_SHA256.txt").read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError, ValueError):
        recorded = None
    if recorded != bundle:
        problems.append("BUNDLE_SHA256.txt does not match evidence.json")
    if expect_bundle and expect_bundle.strip().lower() != bundle:
        problems.append(f"evidence.json hashes to {bundle}, not the expected "
                        f"{expect_bundle.strip().lower()}")

    # A cloud copy leaves some files out of its snapshots and lists them.
    withheld = {}
    try:
        from priorfolio_exports import withheld_record
        record = withheld_record(run_dir)
        if record:
            if record["bundle_sha256"] != bundle:
                problems.append("WITHHELD.json belongs to a different evidence.json")
            for f in record["files"]:
                withheld.setdefault(f["manifest"], {})[f["path"]] = f["sha256"]
            known = {p.get("manifest") for p in projects if isinstance(p, dict)}
            problems += [f"WITHHELD.json names a manifest outside this run: {m}"
                         for m in sorted(set(withheld) - known)]
    except (OSError, ValueError) as e:
        problems.append(f"WITHHELD.json is unreadable: {e}")

    for index, p in enumerate(projects, 1):
        try:
            problems += _verify_project(run_dir, p, against_disk, metadata_only,
                                        withheld.get(p.get("manifest") if isinstance(p, dict) else None, {}))
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            name = p.get("name") if isinstance(p, dict) else None
            problems.append(f"{name or 'project ' + str(index)}: entry in evidence.json "
                            f"is malformed ({type(e).__name__}: {e})")

    try:
        timestamp_dir = safe_child(run_dir, "timestamps")
        for tsr in sorted(timestamp_dir.glob("*.tsr")):
            with safe_child(run_dir, tsr.relative_to(run_dir).as_posix()).open("rb") as source:
                resp = source.read(TSA_MAX_RESPONSE + 1)
            try:
                granted = (len(resp) <= TSA_MAX_RESPONSE and timestamp_status(resp) in (0, 1)
                           and bytes.fromhex(bundle) in resp)
            except ValueError:
                granted = False
            if not granted:
                problems.append(f"timestamps/{tsr.name} was not issued for this evidence.json")
                continue
            state, _, detail = check_timestamp_signature(resp, bytes.fromhex(bundle))
            if state == "failed":
                problems.append(f"timestamps/{tsr.name}: {detail}")
    except (OSError, ValueError) as e:
        problems.append(f"timestamps are unreadable: {e}")
    return problems


def _verify_project(run_dir, p, against_disk, metadata_only=False, withheld=None):
    """Problems with one project. withheld maps paths a cloud copy left out to their hashes."""
    name, problems, withheld = p["name"], [], withheld or {}
    try:
        raw = safe_child(run_dir, p["manifest"]).read_bytes()
    except OSError:
        return [f"{name}: manifest is missing"]
    if (hashlib.sha256(raw).hexdigest() if raw else None) != p["content_sha256"]:
        return [f"{name}: manifest does not match the recorded content hash"]
    entries = read_manifest(raw)
    for rel in entries:
        # Validate names even when not comparing to live projects.
        safe_child(run_dir, rel)
    problems += [f"{name}: withheld file {rel} does not match the manifest"
                 for rel, digest in sorted(withheld.items()) if entries.get(rel) != digest]

    if p.get("snapshot"):
        zip_path = safe_child(run_dir, p["snapshot"])
    if p.get("snapshot") and not metadata_only:
        try:
            # A redacted snapshot differs from the original zip, but every
            # file it keeps must still match the manifest exactly.
            if not withheld and sha256_file(zip_path) != p["snapshot_sha256"]:
                problems.append(f"{name}: snapshot zip does not match its recorded hash")
            with zipfile.ZipFile(zip_path) as z:
                members = [i for i in z.infolist() if not i.is_dir()]
                inside = {}
                for member in members:
                    safe_child(run_dir, member.filename)
                    with z.open(member) as source:
                        inside[member.filename] = hash_stream(source)
            if len(inside) != len(members):
                problems.append(f"{name}: snapshot zip holds the same file name more than once")
            kept = {rel: digest for rel, digest in entries.items() if rel not in withheld}
            problems += [f"{name}: snapshot {m}" for m in compare_hashes(kept, inside)]
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError,
                NotImplementedError, EOFError) as e:
            problems.append(f"{name}: snapshot zip is unreadable: {e}")

    if against_disk:
        base, on_disk = Path(p["path"]), {}
        for rel in entries:
            try:
                on_disk[rel] = sha256_file(safe_child(base, rel))
            except OSError:
                pass
        problems += [f"{name}: on disk {m}"
                     for m in compare_hashes(entries, on_disk, report_extra=False)]
    return problems


def run_summary(run_dir, signatures=False):
    """What a run folder claims: its hash, date, project count and timestamps.

    Each timestamp is (authority, time, signature state). The state is None
    unless signatures=True, which runs the OpenSSL check for every response.
    """
    run_dir = Path(run_dir).resolve()
    try:
        json_path = safe_child(run_dir, "evidence.json")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        count = len(data["projects"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    bundle = sha256_file(json_path)
    stamps = []
    try:
        for tsr in sorted(safe_child(run_dir, "timestamps").glob("*.tsr")):
            with safe_child(run_dir, tsr.relative_to(run_dir).as_posix()).open("rb") as source:
                resp = source.read(TSA_MAX_RESPONSE)
            state, signed_time = None, None
            if signatures:
                state, signed_time, _ = check_timestamp_signature(resp, bytes.fromhex(bundle))
            stamps.append((tsr.stem, signed_time if state == "verified" else timestamp_time(resp), state))
    except (OSError, ValueError):
        pass  # verify_run reports unsafe or damaged timestamp paths.
    return {"bundle": bundle, "generated_utc": data.get("generated_utc"),
            "projects": count, "timestamps": stamps}


SIGNATURE_LABELS = {"verified": "signature verified", "failed": "SIGNATURE DOES NOT VERIFY",
                    "unchecked": "signature not checked: OpenSSL 3 not found", None: "signature not checked"}


TIMESTAMP_NOTICE = ("Timestamps: the SHA-256 of evidence.json is sent to " + ", ".join(n for n, _ in TSAS)
                    + ". Like any web request, this reveals your IP address to those services.")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"PriorFolio {VERSION}")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--gui", action="store_true", help="open the desktop interface")
    ap.add_argument("--root", type=Path, action="append",
                    help="folder containing your projects, or a single git repo; "
                         "repeatable (default: current folder, unless --add is used)")
    ap.add_argument("--list", action="store_true", help="show projects and exit")
    ap.add_argument("--select", help='skip the prompt: "all", numbers/ranges, or names, '
                                     'comma separated (e.g. "1,4-7,example-project")')
    ap.add_argument("--add", type=Path, action="append", default=[],
                    help="a folder to treat as one project, repo or not (repeatable)")
    ap.add_argument("--snapshot", action="store_true",
                    default=True, help="save source snapshots (default)")
    ap.add_argument("--no-snapshot", action="store_false", dest="snapshot",
                    help="hash only; this run cannot provide a complete source backup")
    ap.add_argument("--timestamp", action="store_true", default=True,
                    help="request signed timestamps (default): sends the evidence hash, and "
                         "reveals your IP address, to " + ", ".join(n for n, _ in TSAS))
    ap.add_argument("--no-timestamp", action="store_false", dest="timestamp",
                    help="make the run entirely offline, without timestamps")
    mode.add_argument("--verify", type=Path, metavar="RUN_FOLDER",
                    help="re-check an earlier run folder instead of making a new one")
    ap.add_argument("--against-disk", action="store_true",
                    help="with --verify: read the local project paths recorded in a run you trust")
    ap.add_argument("--metadata-only", action="store_true",
                    help="with --verify: verify manifests but skip source snapshot archives")
    ap.add_argument("--expect", metavar="SHA256",
                    help="with --verify: the bundle hash from your email or agreement")
    ap.add_argument("--out", type=Path, default=Path.home() / "PriorFolio" / "runs",
                    help="where to write results (default: ~/PriorFolio/runs)")
    mode.add_argument("--backup", type=Path, metavar="RUN_FOLDER",
                      help="export a complete private backup ZIP")
    ap.add_argument("--destination", type=Path, help="new ZIP path, e.g. in a cloud-sync folder")
    ap.add_argument("--withhold-secrets", action="store_true",
                    help="with --backup: make a cloud copy that leaves out files whose names "
                         "suggest secrets (.env, keys, credentials); keep a full copy offline")
    ap.add_argument("--withhold", action="append", default=[], metavar="PATTERN",
                    help="with --backup: also leave out files whose path or name matches "
                         'PATTERN, e.g. "*.pem" or "config/secrets/*" (repeatable)')
    mode.add_argument("--confirm-backup", type=Path, metavar="RUN_FOLDER",
                      help="record that you checked the received email or uploaded cloud copy")
    ap.add_argument("--backup-method", choices=("email", "cloud"))
    return ap


def main():
    for stream in (sys.stdout, sys.stderr):  # project names may not fit the console's encoding
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    ap = build_parser()
    args = ap.parse_args()
    if (args.metadata_only or args.against_disk or args.expect) and not args.verify:
        ap.error("--metadata-only, --against-disk and --expect require --verify")
    if bool(args.backup) != bool(args.destination):
        ap.error("--backup and --destination must be used together")
    if (args.withhold or args.withhold_secrets) and not args.backup:
        ap.error("--withhold and --withhold-secrets require --backup")
    if bool(args.confirm_backup) != bool(args.backup_method):
        ap.error("--confirm-backup and --backup-method must be used together")

    try:
        run_cli(args)
    except ToolError as e:
        sys.exit(str(e))
    except OSError as e:
        sys.exit(f"error: {e}")


def open_gui():
    """Open the desktop interface, with a plain message when Tkinter is missing."""
    try:
        from priorfolio_gui import main as start
    except ImportError as e:
        if (e.name or "").split(".")[0] not in ("tkinter", "_tkinter"):
            raise
        raise ToolError("The desktop interface needs Tkinter, which this Python installation "
                        "does not include. Install your system's Tk package (for example "
                        "python3-tk), or use the command-line options shown by priorfolio --help.") from None
    start()


def gui_main():
    """Entry point of the installed priorfolio-gui launcher."""
    try:
        open_gui()
    except ToolError as e:
        sys.exit(str(e))


def run_cli(args):
    if args.gui:
        open_gui()
        return
    if args.backup or args.confirm_backup:
        from priorfolio_exports import export_backup, confirm_backup, likely_secrets, select_withheld

        def listed(items):
            for item in items[:20]:
                print("  " + item, file=sys.stderr)
            if len(items) > 20:
                print(f"  ... and {len(items) - 20} more", file=sys.stderr)

        if args.backup:
            withhold = []
            if args.withhold or args.withhold_secrets:
                try:
                    withhold = select_withheld(args.backup, args.withhold, args.withhold_secrets)
                except (OSError, ValueError, KeyError, TypeError):
                    raise ToolError(f"{args.backup} has no readable PriorFolio evidence record.") from None
                if not withhold:
                    raise ToolError("No recorded files match the files to withhold; nothing was exported.")
                print(f"Cloud copy: withholding {len(withhold)} file(s):", file=sys.stderr)
                listed(withhold)
            else:
                secrets = likely_secrets(args.backup)
                if secrets:
                    print(f"WARNING: {len(secrets)} file(s) that may hold secrets will be in this "
                          "full backup:", file=sys.stderr)
                    listed(secrets)
                    print("Keep this full copy offline. For a cloud copy without them, export again "
                          "with --withhold-secrets.", file=sys.stderr)
            status = export_backup(args.backup, args.destination, withhold)
            print(f"Exported: {status['archive']}\nArchive SHA-256: {status['archive_sha256']}")
            print("Before it leaves this computer, put it in a password-protected 7-Zip archive "
                  "with encrypted file names (see BACKUP.txt).")
            print("Remote backup pending: email the .7z to yourself or let the cloud upload finish.")
            print("Check the received/uploaded copy, then record it with --confirm-backup.")
        else:
            confirm_backup(args.confirm_backup, args.backup_method)
            print("Remote backup check recorded as confirmed by you; delivery was not verified automatically.")
        return
    if args.verify:
        summary = run_summary(args.verify, signatures=True)
        if summary:
            print(f"Bundle SHA-256: {summary['bundle']}")
            print(f"Generated (UTC): {summary['generated_utc']}, {summary['projects']} project(s)")
            for name, when, state in summary["timestamps"]:
                print(f"Timestamp {name}: {when or 'time not readable'} ({SIGNATURE_LABELS[state]})")
        problems = verify_run(args.verify, args.against_disk, args.expect, args.metadata_only)
        for problem in problems:
            print(problem)
        try:
            from priorfolio_exports import withheld_files
            withheld = withheld_files(args.verify)
        except (OSError, ValueError):
            withheld = []  # verify_run reports it
        scope = "metadata only; source snapshots not checked" if args.metadata_only else "evidence hashes"
        if withheld and not args.metadata_only:
            scope += f" except {len(withheld)} withheld file(s)"
        print(f"{len(problems)} problem(s) found." if problems else f"OK: {scope} match.")
        if withheld:
            print(f"This is a cloud copy. {len(withheld)} file(s) were left out of its snapshots "
                  "(see WITHHELD.txt). Their hashes are still in evidence.json, but their contents "
                  "can only be checked with the full copy:")
            for f in withheld:
                print(f"  {f['project']}: {f['path']}")
        if summary and not args.expect:
            print("Compare the bundle hash above with the one in your email or agreement, "
                  "or pass it with --expect.")
        sys.exit(1 if problems else 0)

    roots = [r.resolve() for r in (args.root or ([] if args.add else [Path.cwd()]))]
    for folder in roots + args.add:
        if not folder.is_dir():
            raise ToolError(f"not a folder: {folder}")

    print("Scanning " + ", ".join(str(r) for r in roots) + " ...", file=sys.stderr)
    projects = scan_roots(roots, args.add)
    if not projects:
        raise ToolError("no projects found")
    print_list(projects)
    if args.list:
        return

    selection = args.select
    if selection is None:
        print('\nSelect projects: "all", numbers, ranges or names, comma separated.')
        try:
            selection = input("> ")
        except EOFError:
            raise ToolError("nothing selected") from None
    try:
        chosen = parse_selection(selection, projects)
    except ValueError as e:
        raise ToolError(str(e)) from None
    if not chosen:
        raise ToolError("nothing selected")

    if args.timestamp:
        print(TIMESTAMP_NOTICE + " Use --no-timestamp to skip.", file=sys.stderr)
    run = generate(chosen, args.out, args.snapshot, args.timestamp,
                   log=lambda m: print(m, file=sys.stderr))
    print(f"\nWrote {run['out_dir']}")
    print(f"Bundle SHA-256 (evidence.json): {run['bundle']}")
    print("Self-check: the new run was read back and verifies.")
    for t in run["timestamps"]:
        if not t["ok"]:
            outcome = "FAILED - " + t["error"]
        else:
            state = "verified" if t.get("signature_verified") else "unchecked"
            outcome = f"{t.get('time') or 'time unknown'} ({SIGNATURE_LABELS[state]})"
        print(f"Timestamp {t['tsa']}: {outcome}")
    if run["warnings"]:
        print(f"\n{len(run['warnings'])} WARNING(S): some content may be missing from the hashes")
        for w in run["warnings"]:
            print("  " + w)
    print("Review disclosure.md before sharing. Private backup: REQUIRED.")
    print("Export the complete ZIP with --backup and --destination, then email it to yourself or upload it.")
    print("email.txt and BACKUP.txt explain the remote backup check.")


# Root certificates of the timestamp authorities in TSAS, pinned so verification
# does not depend on the operating system's trust store. SHA-256 fingerprints:
#   DigiCert Trusted Root G4               552f7bdcf1a7af9e6ce672017f4f12abf77240c78e761ac203d1d9d20ac89988
#     cacerts.digicert.com; identical to the copy in the Windows root store.
#   Sectigo Public Time Stamping Root R46  4941b001b8a97e961b7817c9d9e960ec4b056bfc915a8c1aabf6ef6b3ac046a5
#     crt.sectigo.com; its key is cross-signed by USERTrust RSA Certification Authority.
#   Free TSA Root CA                       a6379e7cecc05faa3cbf076013d745e327bbbaa38c0b9af22469d4701d18aabc
#     freetsa.org over HTTPS; self-signed, so trusted as published by its operator.
# Each key matches the root key in the chain the authority returns with its tokens.
TSA_ROOTS = """\
-----BEGIN CERTIFICATE-----
MIIFkDCCA3igAwIBAgIQBZsbV56OITLiOQe9p3d1XDANBgkqhkiG9w0BAQwFADBi
MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3
d3cuZGlnaWNlcnQuY29tMSEwHwYDVQQDExhEaWdpQ2VydCBUcnVzdGVkIFJvb3Qg
RzQwHhcNMTMwODAxMTIwMDAwWhcNMzgwMTE1MTIwMDAwWjBiMQswCQYDVQQGEwJV
UzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3d3cuZGlnaWNlcnQu
Y29tMSEwHwYDVQQDExhEaWdpQ2VydCBUcnVzdGVkIFJvb3QgRzQwggIiMA0GCSqG
SIb3DQEBAQUAA4ICDwAwggIKAoICAQC/5pBzaN675F1KPDAiMGkz7MKnJS7JIT3y
ithZwuEppz1Yq3aaza57G4QNxDAf8xukOBbrVsaXbR2rsnnyyhHS5F/WBTxSD1If
xp4VpX6+n6lXFllVcq9ok3DCsrp1mWpzMpTREEQQLt+C8weE5nQ7bXHiLQwb7iDV
ySAdYyktzuxeTsiT+CFhmzTrBcZe7FsavOvJz82sNEBfsXpm7nfISKhmV1efVFiO
DCu3T6cw2Vbuyntd463JT17lNecxy9qTXtyOj4DatpGYQJB5w3jHtrHEtWoYOAMQ
jdjUN6QuBX2I9YI+EJFwq1WCQTLX2wRzKm6RAXwhTNS8rhsDdV14Ztk6MUSaM0C/
CNdaSaTC5qmgZ92kJ7yhTzm1EVgX9yRcRo9k98FpiHaYdj1ZXUJ2h4mXaXpI8OCi
EhtmmnTK3kse5w5jrubU75KSOp493ADkRSWJtppEGSt+wJS00mFt6zPZxd9LBADM
fRyVw4/3IbKyEbe7f/LVjHAsQWCqsWMYRJUadmJ+9oCw++hkpjPRiQfhvbfmQ6QY
uKZ3AeEPlAwhHbJUKSWJbOUOUlFHdL4mrLZBdd56rF+NP8m800ERElvlEFDrMcXK
chYiCd98THU/Y+whX8QgUWtvsauGi0/C1kVfnSD8oR7FwI+isX4KJpn15GkvmB0t
9dmpsh3lGwIDAQABo0IwQDAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIB
hjAdBgNVHQ4EFgQU7NfjgtJxXWRM3y5nP+e6mK4cD08wDQYJKoZIhvcNAQEMBQAD
ggIBALth2X2pbL4XxJEbw6GiAI3jZGgPVs93rnD5/ZpKmbnJeFwMDF/k5hQpVgs2
SV1EY+CtnJYYZhsjDT156W1r1lT40jzBQ0CuHVD1UvyQO7uYmWlrx8GnqGikJ9yd
+SeuMIW59mdNOj6PWTkiU0TryF0Dyu1Qen1iIQqAyHNm0aAFYF/opbSnr6j3bTWc
fFqK1qI4mfN4i/RN0iAL3gTujJtHgXINwBQy7zBZLq7gcfJW5GqXb5JQbZaNaHqa
sjYUegbyJLkJEVDXCLG4iXqEI2FCKeWjzaIgQdfRnGTZ6iahixTXTBmyUEFxPT9N
cCOGDErcgdLMMpSEDQgJlxxPwO5rIHQw0uA5NBCFIRUBCOhVMt5xSdkoF1BN5r5N
0XWs0Mr7QbhDparTwwVETyw2m+L64kW4I1NsBm9nVX9GtUw/bihaeSbSpKhil9Ie
4u1Ki7wb/UdKDd9nZn6yW0HQO+T0O/QEY+nvwlQAUaCKKsnOeMzV6ocEGLPOr0mI
r/OSmbaz5mEP0oUA51Aa5BuVnRmhuZyxm7EAHu/QD09CbMkKvO5D+jpxpchNJqU1
/YldvIViHTLSoCtU7ZpXwdv6EM8Zt4tKG48BtieVU+i2iW1bvGjUI+iLUaJW+fCm
gKDWHrO8Dw9TdSmq6hN35N6MgSGtBxBHEa2HPQfRdbzP82Z+
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIFejCCA2KgAwIBAgIQeD0FbPqDLn5p+FYidp8CuTANBgkqhkiG9w0BAQwFADBX
MQswCQYDVQQGEwJHQjEYMBYGA1UEChMPU2VjdGlnbyBMaW1pdGVkMS4wLAYDVQQD
EyVTZWN0aWdvIFB1YmxpYyBUaW1lIFN0YW1waW5nIFJvb3QgUjQ2MB4XDTIxMDMy
MjAwMDAwMFoXDTQ2MDMyMTIzNTk1OVowVzELMAkGA1UEBhMCR0IxGDAWBgNVBAoT
D1NlY3RpZ28gTGltaXRlZDEuMCwGA1UEAxMlU2VjdGlnbyBQdWJsaWMgVGltZSBT
dGFtcGluZyBSb290IFI0NjCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIB
AIid2LlFZ50d3ei5JoGaVFTAfEkFm8xaFQ/ZlBBEtEFAgXcUmanU5HYsyAhTXiDQ
kiUvpVdYqZ1uYoZEMgtHES1l1Cc6HaqZzEbOOp6YiTx63ywTon434aXVydmhx7Dx
4IBrAou7hNGsKioIBPy5GMN7KmgYmuu4f92sKKjbxqohUSfjk1mJlAjthgF7Hjx4
vvyVDQGsd5KarLW5d73E3ThobSkob2SL48LpUR/O627pDchxll+bTSv1gASn/hp6
IuHJorEu6EopoB1CNFp/+HpTXeNARXUmdRMKbnXWflq+/g36NJXB35ZvxQw6zid6
1qmrlD/IbKJA6COw/8lFSPQwBP1ityZdwuCysCKZ9ZjczMqbUcLFyq6KdOpuzVDR
3ZUwxDKL1wCAxgL2Mpz7eZbrb/JWXiOcNzDpQsmwGQ6Stw8tTCqPumhLRPb7YkzM
8/6NnWH3T9ClmcGSF22LEyJYNWCHrQqYubNeKolzqUbCqhSqmr/UdUeb49zYHr7A
LL8bAJyPDmubNqMtuaobKASBqP84uhqcRY/pjnYd+V5/dcu9ieERjiRKKsxCG1t6
tG9oj7liwPddXEcYGOUiWLm742st50jGwTzxbMpepmOP1mLnJskvZaN5e45NuzAH
teORlsSuDt5t4BBRCJL+5EZnnw0ezntk9R8QJyAkL6/bAgMBAAGjQjBAMB0GA1Ud
DgQWBBT2d2rdP/0BE/8WoWyCAi/QCj0UJTAOBgNVHQ8BAf8EBAMCAYYwDwYDVR0T
AQH/BAUwAwEB/zANBgkqhkiG9w0BAQwFAAOCAgEACv68sZqZvmHk7JoU7AfTEZ7b
X45u40OrLX/6vzHQAwDg1TB00bWbiwj+7+we60BP8AMsw1h5NCXWxeyvrEVC54Mi
rwVrcF6SFGjRwRoelRuiaZ5FDO4oWhaePjZx3/jkQ7j653CA5WscYqlneLXBEZiY
o6rRNqnsZHZTQ606mlkNpsy3TFMADv5whZmilVEZ83OvydfH2DNL3FTkpZG7+lHS
1DFHMSUB5UtSSeSRg2UWTXwU5oObGjFk/35fC/dlF+nJIXdqsw0TZSS86bi5GRCJ
VjqjnkpZ0Jut7ucEv4PNOIU8ijkMRj17QPjHMtdy+WxBDDSdas/UFTVB/GF+Fofn
OD3iZ4tXxFjPU3EWRcMWx8fcGyzlBfcjeoPNbNC7wfyV9Qkzfk2Bd48jGxG7OThY
Wolc56vmBHqDEfguDwYc9AeWirMVRDi+WYlsktzAEObiFoPqs+LWU5q7+Q1+nEcs
tNuDIedeBRcHmtjL2hV3luuEWwDSnRhSjhPLPXzqpJ1rG3r4yqm1NjKg5A5QO9Az
veHqRQldluTSKuu96oPEPusL2oF+4MxkJ+SQMGFWTw/PNCblUN8GXeL5+mR20diP
5LiJMq8U3IpM/0Q7OFFQ+lXuYFIuDvUprWBPsp5La2WhL2+iLVVXItSl7up5yQqX
XftzTXTX48FRiJ6PA9o=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIH/zCCBeegAwIBAgIJAMHphhYNqOmAMA0GCSqGSIb3DQEBDQUAMIGVMREwDwYD
VQQKEwhGcmVlIFRTQTEQMA4GA1UECxMHUm9vdCBDQTEYMBYGA1UEAxMPd3d3LmZy
ZWV0c2Eub3JnMSIwIAYJKoZIhvcNAQkBFhNidXNpbGV6YXNAZ21haWwuY29tMRIw
EAYDVQQHEwlXdWVyemJ1cmcxDzANBgNVBAgTBkJheWVybjELMAkGA1UEBhMCREUw
HhcNMTYwMzEzMDE1MjEzWhcNNDEwMzA3MDE1MjEzWjCBlTERMA8GA1UEChMIRnJl
ZSBUU0ExEDAOBgNVBAsTB1Jvb3QgQ0ExGDAWBgNVBAMTD3d3dy5mcmVldHNhLm9y
ZzEiMCAGCSqGSIb3DQEJARYTYnVzaWxlemFzQGdtYWlsLmNvbTESMBAGA1UEBxMJ
V3VlcnpidXJnMQ8wDQYDVQQIEwZCYXllcm4xCzAJBgNVBAYTAkRFMIICIjANBgkq
hkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAtgKODjAy8REQ2WTNqUudAnjhlCrpE6ql
mQfNppeTmVvZrH4zutn+NwTaHAGpjSGv4/WRpZ1wZ3BRZ5mPUBZyLgq0YrIfQ5Fx
0s/MRZPzc1r3lKWrMR9sAQx4mN4z11xFEO529L0dFJjPF9MD8Gpd2feWzGyptlel
b+PqT+++fOa2oY0+NaMM7l/xcNHPOaMz0/2olk0i22hbKeVhvokPCqhFhzsuhKsm
q4Of/o+t6dI7sx5h0nPMm4gGSRhfq+z6BTRgCrqQG2FOLoVFgt6iIm/BnNffUr7V
DYd3zZmIwFOj/H3DKHoGik/xK3E82YA2ZulVOFRW/zj4ApjPa5OFbpIkd0pmzxzd
EcL479hSA9dFiyVmSxPtY5ze1P+BE9bMU1PScpRzw8MHFXxyKqW13Qv7LWw4sbk3
SciB7GACbQiVGzgkvXG6y85HOuvWNvC5GLSiyP9GlPB0V68tbxz4JVTRdw/Xn/XT
FNzRBM3cq8lBOAVt/PAX5+uFcv1S9wFE8YjaBfWCP1jdBil+c4e+0tdywT2oJmYB
BF/kEt1wmGwMmHunNEuQNzh1FtJY54hbUfiWi38mASE7xMtMhfj/C4SvapiDN837
gYaPfs8x3KZxbX7C3YAsFnJinlwAUss1fdKar8Q/YVs7H/nU4c4Ixxxz4f67fcVq
M2ITKentbCMCAwEAAaOCAk4wggJKMAwGA1UdEwQFMAMBAf8wDgYDVR0PAQH/BAQD
AgHGMB0GA1UdDgQWBBT6VQ2MNGZRQ0z357OnbJWveuaklzCBygYDVR0jBIHCMIG/
gBT6VQ2MNGZRQ0z357OnbJWveuakl6GBm6SBmDCBlTERMA8GA1UEChMIRnJlZSBU
U0ExEDAOBgNVBAsTB1Jvb3QgQ0ExGDAWBgNVBAMTD3d3dy5mcmVldHNhLm9yZzEi
MCAGCSqGSIb3DQEJARYTYnVzaWxlemFzQGdtYWlsLmNvbTESMBAGA1UEBxMJV3Vl
cnpidXJnMQ8wDQYDVQQIEwZCYXllcm4xCzAJBgNVBAYTAkRFggkAwemGFg2o6YAw
MwYDVR0fBCwwKjAooCagJIYiaHR0cDovL3d3dy5mcmVldHNhLm9yZy9yb290X2Nh
LmNybDCBzwYDVR0gBIHHMIHEMIHBBgorBgEEAYHyJAEBMIGyMDMGCCsGAQUFBwIB
FidodHRwOi8vd3d3LmZyZWV0c2Eub3JnL2ZyZWV0c2FfY3BzLmh0bWwwMgYIKwYB
BQUHAgEWJmh0dHA6Ly93d3cuZnJlZXRzYS5vcmcvZnJlZXRzYV9jcHMucGRmMEcG
CCsGAQUFBwICMDsaOUZyZWVUU0EgdHJ1c3RlZCB0aW1lc3RhbXBpbmcgU29mdHdh
cmUgYXMgYSBTZXJ2aWNlIChTYWFTKTA3BggrBgEFBQcBAQQrMCkwJwYIKwYBBQUH
MAGGG2h0dHA6Ly93d3cuZnJlZXRzYS5vcmc6MjU2MDANBgkqhkiG9w0BAQ0FAAOC
AgEAaK9+v5OFYu9M6ztYC+L69sw1omdyli89lZAfpWMMh9CRmJhM6KBqM/ipwoLt
nxyxGsbCPhcQjuTvzm+ylN6VwTMmIlVyVSLKYZcdSjt/eCUN+41K7sD7GVmxZBAF
ILnBDmTGJmLkrU0KuuIpj8lI/E6Z6NnmuP2+RAQSHsfBQi6sssnXMo4HOW5gtPO7
gDrUpVXID++1P4XndkoKn7Svw5n0zS9fv1hxBcYIHPPQUze2u30bAQt0n0iIyRLz
aWuhtpAtd7ffwEbASgzB7E+NGF4tpV37e8KiA2xiGSRqT5ndu28fgpOY87gD3ArZ
DctZvvTCfHdAS5kEO3gnGGeZEVLDmfEsv8TGJa3AljVa5E40IQDsUXpQLi8G+UC4
1DWZu8EVT4rnYaCw1VX7ShOR1PNCCvjb8S8tfdudd9zhU3gEB0rxdeTy1tVbNLXW
99y90xcwr1ZIDUwM/xQ/noO8FRhm0LoPC73Ef+J4ZBdrvWwauF3zJe33d4ibxEcb
8/pz5WzFkeixYM2nsHhqHsBKw7JPouKNXRnl5IAE1eFmqDyC7G/VT7OF669xM6hb
Ut5G21JE4cNK6NNucS+fzg1JPX0+3VhsYZjj7D5uljRvQXrJ8iHgr/M6j2oLHvTA
I2MLdq2qjZFDOCXsxBxJpbmLGBx9ow6ZerlUxzws2AWv2pk=
-----END CERTIFICATE-----
"""


if __name__ == "__main__":
    # Keep export/GUI imports on the same module (and ToolError class) as the CLI.
    sys.modules.setdefault("priorfolio", sys.modules[__name__])
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required.")
    main()
