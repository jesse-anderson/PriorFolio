import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def git(cwd, *args):
    done = subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=cwd, capture_output=True)
    assert done.returncode == 0, f"git {' '.join(map(str, args))}: {done.stderr.decode(errors='replace')}"


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Fixed identity and dates, and no influence from the machine's git config."""
    gc.collect()  # free any Tk window left by a GUI test, on the main thread
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in os.environ.copy():
        if (key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
                or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))):
            monkeypatch.delenv(key)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test Author")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
        monkeypatch.setenv(f"GIT_{role}_DATE", "2025-01-02T03:04:05+02:00")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path_factory, monkeypatch):
    """Keep the CLI, including subprocesses, away from the real ~/.priorfolio."""
    path = tmp_path_factory.mktemp("settings") / "settings.json"
    monkeypatch.setenv("PRIORFOLIO_SETTINGS", str(path))
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Timestamps are on by default. Route any accidental request, including from
    CLI subprocesses, to a closed local port. PRIORFOLIO_NETWORK=1 allows live tests."""
    if os.environ.get("PRIORFOLIO_NETWORK"):
        return
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def offscreen_windows(monkeypatch):
    """Keep real Tk windows out of the way during local automation."""
    if not os.environ.get("PRIORFOLIO_TEST_OFFSCREEN"):
        return
    try:
        import tkinter as tk
        import priorfolio_gui
    except ImportError:
        return
    for cls in (priorfolio_gui.App, tk.Toplevel):
        original = cls.__init__

        def initialize(window, *args, _original=original, **kwargs):
            _original(window, *args, **kwargs)
            window.geometry("+-32000+-32000")

        monkeypatch.setattr(cls, "__init__", initialize)


def write(base, files):
    for rel, content in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


@pytest.fixture
def workspace(tmp_path):
    """A projects folder holding one of each kind of project the tool meets.

    plain/      not a repo; has ignored, dependency, cache and venv files
    repo/       committed, then given a modified, an untracked and an ignored file
    clean/      committed and untouched
    fresh/      git init with files but no commits
    empty/      nothing in it
    group/inner a repo nested inside a folder that is not a repo
    """
    root = tmp_path / "projects"

    write(root / "plain", {
        "main.py": "print('hi')\n",
        "src/lib.py": "X = 1\n",
        "data/blob.bin": bytes(range(256)) * 4,
        "docs/café notes.txt": "accents and spaces\n",
        ".gitignore": "*.log\n",
        "debug.log": "ignored by .gitignore\n",
        "node_modules/dep/index.js": "dependency\n",
        "__pycache__/main.cpython-312.pyc": b"\x00cache",
        "myenv/pyvenv.cfg": "home = somewhere\n",
        "myenv/Lib/site.py": "virtualenv file\n",
    })

    repo = root / "repo"
    write(repo, {
        "app.py": "VERSION = 1\n",
        "README.md": "# repo\n",
        ".gitignore": "build/\n",
        "env/settings.py": "tracked file in a folder that is skipped when untracked\n",
    })
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    write(repo, {
        "app.py": "VERSION = 2\n",
        "notes.txt": "untracked\n",
        "build/out.bin": b"ignored build output",
    })

    clean = root / "clean"
    write(clean, {"one.txt": "1\n", "sub/two.txt": "2\n"})
    git(clean, "init", "-q", "-b", "main")
    git(clean, "add", "-A")
    git(clean, "commit", "-q", "-m", "only")

    fresh = root / "fresh"
    write(fresh, {"draft.rs": "fn main() {}\n"})
    git(fresh, "init", "-q", "-b", "main")

    (root / "empty").mkdir()

    inner = root / "group" / "inner"
    write(inner, {"lib.c": "int x;\n"})
    write(root / "group", {"NOTES.md": "wrapper folder\n"})
    git(inner, "init", "-q", "-b", "main")
    git(inner, "add", "-A")
    git(inner, "commit", "-q", "-m", "inner")

    return root


@pytest.fixture
def unsigned_tokens(monkeypatch):
    """Hand-made timestamp responses are not really signed. Check them as if
    OpenSSL were missing: kept, and marked as not checked."""
    import priorfolio
    monkeypatch.setattr(priorfolio, "find_openssl", lambda: None)
