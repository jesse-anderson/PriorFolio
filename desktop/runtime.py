# SPDX-License-Identifier: GPL-3.0-only
"""Prepare a frozen process to call the separately installed Git executable."""
import os
from pathlib import Path
import sys


def prepare():
    if not getattr(sys, "frozen", False):
        return
    # The launchers import the application and Tk before reaching this point,
    # so their native dependencies are already loaded. External Git must use
    # system libraries rather than inheriting PyInstaller's library search path.
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    elif sys.platform != "darwin":
        original = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = original
    if sys.platform == "darwin":
        # Finder does not inherit a user's shell PATH, including Homebrew Git.
        extra = [path for path in ("/opt/homebrew/bin", "/usr/local/bin") if Path(path, "git").is_file()]
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "/usr/bin:/bin")])
