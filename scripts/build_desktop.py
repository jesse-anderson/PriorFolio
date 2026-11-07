# SPDX-License-Identifier: GPL-3.0-only
"""Build and smoke-test native PyInstaller GUI/CLI bundles on the current OS."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tkinter
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import priorfolio


def run(args, cwd=ROOT):
    result = subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def notices(destination):
    destination.mkdir()
    shutil.copy2(ROOT / "LICENSE", destination / "PriorFolio-LICENSE.txt")
    for candidate in (Path(sys.base_prefix) / "LICENSE.txt", Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"):
        if candidate.is_file():
            shutil.copy2(candidate, destination / "Python-LICENSE.txt")
            break
    else:
        raise RuntimeError("Cannot locate the Python runtime license for redistribution.")
    library = Path(tkinter.Tcl().eval("info library"))
    tk_library = library.parent / library.name.replace("tcl", "tk") if "tcl" in library.name else None
    for name, path, version in (("Tcl", library, tkinter.TclVersion), ("Tk", tk_library, tkinter.TkVersion)):
        license_path = path / "license.terms" if path else None
        if not license_path or not license_path.is_file():
            if version != 8.6:
                raise RuntimeError(f"Cannot locate the {name} {version} runtime license.")
            # Some official Python installers omit these notices; preserve the
            # upstream 8.6 terms in the desktop distribution in that case.
            license_path = ROOT / "desktop/licenses" / f"{name}-license.terms"
        shutil.copy2(license_path, destination / f"{name}-license.terms")
    distribution = importlib.metadata.distribution("pyinstaller")
    matches = [path for path in distribution.files or [] if Path(path).name == "COPYING.txt"]
    if not matches:
        raise RuntimeError("Cannot locate PyInstaller's bootloader license.")
    shutil.copy2(distribution.locate_file(matches[0]), destination / "PyInstaller-COPYING.txt")


def build(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (ROOT / "build").mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="desktop-", dir=ROOT / "build"))
    bundles = work / "bundles"
    bundles.mkdir()
    for name, entry, windowed in (("PriorFolio", "gui_entry.py", True), ("PriorFolioCLI", "cli_entry.py", False)):
        args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--noupx",
                "--name", name, "--distpath", bundles, "--workpath", work / "pyinstaller",
                "--specpath", work, "--paths", ROOT, "--paths", ROOT / "desktop"]
        if windowed:
            args.append("--windowed")
            if sys.platform == "darwin":
                args += ["--osx-bundle-identifier", "net.jesse-anderson.priorfolio"]
        run(args + [ROOT / "desktop" / entry])
    suffix = ".exe" if sys.platform == "win32" else ""
    cli = bundles / "PriorFolioCLI" / ("PriorFolioCLI" + suffix)
    gui = (bundles / "PriorFolio.app/Contents/MacOS/PriorFolio" if sys.platform == "darwin"
           else bundles / "PriorFolio" / ("PriorFolio" + suffix))
    report = work / "gui-check.json"
    run([gui, "--smoke-test", report], work)
    if not json.loads(report.read_text())["started"]:
        raise RuntimeError("Frozen GUI did not start.")
    if run([cli, "--version"], work).strip() != f"PriorFolio {priorfolio.VERSION}":
        raise RuntimeError("Frozen CLI has the wrong version.")
    project = work / "source"
    project.mkdir()
    content = {"design.txt": b"Known design bytes.\n", "empty.bin": b""}
    for name, value in content.items():
        (project / name).write_bytes(value)
    run([cli, "--add", project, "--select", "all", "--out", work / "runs"], work)
    folder = next((work / "runs").iterdir())
    evidence = json.loads((folder / "evidence.json").read_text())
    with zipfile.ZipFile(folder / evidence["projects"][0]["snapshot"]) as archive:
        if {name: archive.read(name) for name in archive.namelist()} != content:
            raise RuntimeError("Frozen app snapshot differs from the source fixture.")
    backup = work / "backup.zip"
    run([cli, "--backup", folder, "--destination", backup], work)
    restored = work / "restored"
    with zipfile.ZipFile(backup) as archive:
        archive.extractall(restored)  # Our own synthetic output.
    digest = hashlib.sha256((folder / "evidence.json").read_bytes()).hexdigest()
    run([cli, "--verify", restored, "--expect", digest, "--against-disk"], work)
    notices(bundles / "licenses")
    (bundles / "README.txt").write_text(
        "PriorFolio " + priorfolio.VERSION + "\n\n"
        "Launch PriorFolio (or PriorFolio.app on macOS) for the desktop interface.\n"
        "PriorFolioCLI provides command-line use. Keep each application folder intact.\n"
        "Python and Tk are included. Install Git separately and put it on PATH.\n"
        "Private evidence and settings are saved in your home folder, outside this bundle.\n"
        "These initial builds are unsigned and are not notarized on macOS.\n"
        "Source and documentation: https://github.com/jesse-anderson/PriorFolio\n"
        "License notices are in licenses/.\n", encoding="utf-8")
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    name = f"PriorFolio-{priorfolio.VERSION}-{system}-{architecture}"
    # POSIX bundles contain symlinks; tar preserves them and executable modes.
    if sys.platform == "win32":
        archive_path = Path(shutil.make_archive(str(output / name), "zip", root_dir=bundles))
    else:
        archive_path = output / (name + ".tar.gz")
        with tarfile.open(archive_path, "w:gz", dereference=False) as archive:
            for path in sorted(bundles.iterdir()):
                if sys.platform == "darwin" and path.name == "PriorFolio":
                    continue  # The .app is the GUI distribution on macOS.
                archive.add(path, arcname=path.name)
    Path(str(archive_path) + ".sha256").write_text(
        hashlib.sha256(archive_path.read_bytes()).hexdigest() + "  " + archive_path.name + "\n", encoding="utf-8")
    print(f"Built and verified {archive_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "desktop")
    build(parser.parse_args().out)
