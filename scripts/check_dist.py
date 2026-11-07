# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Jesse Anderson
"""Inspect release contents and exercise an isolated installed wheel offline."""
import argparse
import email
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

MODULES = {"priorfolio.py", "priorfolio_exports.py", "priorfolio_gui.py"}
ROOT_FILES = MODULES | {"gui.py", "LICENSE", "README.md", "CHANGELOG.md", "CONTRIBUTING.md",
                        "RELEASING.md", "SECURITY.md", "MANIFEST.in", "pyproject.toml",
                        "setup.cfg", "PKG-INFO"}
EXTRA_FILES = {"docs/preview.png", "scripts/check_dist.py", ".github/workflows/tests.yml",
               ".github/workflows/publish.yml", "tests/conftest.py", "tests/test_core.py",
               "tests/test_edge_cases.py", "tests/test_gui.py", "tests/test_privacy_backup.py",
               "tests/test_workflows.py"}
EGG_INFO = {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
            "requires.txt", "top_level.txt"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(args, cwd, success=True):
    result = subprocess.run(list(map(str, args)), cwd=cwd, capture_output=True, text=True)
    if success and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def check(directory, expected_version=None):
    directory = Path(directory).resolve()
    wheels, sources = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    require(len(wheels) == len(sources) == 1, "Use a directory with exactly one wheel and one source archive.")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        require(len(metadata_paths) == 1, "Expected one package metadata record.")
        metadata = email.message_from_bytes(archive.read(metadata_paths[0]))
        version = metadata["Version"]
        require(metadata["Name"] == "priorfolio", "Unexpected package name.")
        require(not expected_version or version == expected_version, "Tag and package versions differ.")
        require(metadata["License-Expression"] == "GPL-3.0-only", "Missing SPDX license expression.")
        require(not metadata.get("Author-email"), "Unexpected public author email in package metadata.")
        require("Development Status :: 3 - Alpha" in metadata.get_all("Classifier", []), "Expected alpha status.")
        prefix = metadata_paths[0].rsplit("/", 1)[0]
        allowed = MODULES | {f"{prefix}/{name}" for name in (
            "licenses/LICENSE", "METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD")}
        require(set(names) == allowed and len(names) == len(allowed), "Unexpected or duplicate wheel files.")
        wheel_modules = {name: archive.read(name) for name in MODULES}
        entry_points = archive.read(f"{prefix}/entry_points.txt").decode()
        require("priorfolio-gui = priorfolio_gui:main" in entry_points, "GUI entry point uses the wrong module.")
        require("priorfolio = priorfolio:main" in entry_points, "Missing CLI entry point.")
        require(archive.read(f"{prefix}/licenses/LICENSE").startswith(b"Copyright"), "License missing.")
    with tarfile.open(sources[0]) as archive:
        members = archive.getmembers()
        source_root = f"priorfolio-{version}/"
        allowed = ROOT_FILES | EXTRA_FILES | {f"priorfolio.egg-info/{name}" for name in EGG_INFO}
        files = set()
        for member in members:
            if member.isdir():
                continue
            require(member.isfile() and member.name.startswith(source_root), "Unsafe source archive member.")
            name = member.name[len(source_root):]
            require(name in allowed and name not in files, f"Unexpected or duplicate source file: {name}")
            files.add(name)
        require(MODULES | {"LICENSE", "README.md", "pyproject.toml"} <= files, "Incomplete source archive.")
        for name, content in wheel_modules.items():
            require(archive.extractfile(source_root + name).read() == content, f"Source/wheel differ: {name}")

    with tempfile.TemporaryDirectory(prefix="priorfolio-release-") as temporary:
        case = Path(temporary)
        environment = case / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        binary = environment / ("Scripts" if sys.platform == "win32" else "bin")
        python = binary / ("python.exe" if sys.platform == "win32" else "python")
        command = binary / ("priorfolio.exe" if sys.platform == "win32" else "priorfolio")
        run([python, "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir", wheels[0]], case)
        require(run([command, "--version"], case).stdout.strip() == f"PriorFolio {version}", "Installed version mismatch.")
        # Loading the entry point imports Tk without opening any windows.
        run([python, "-I", "-c", "from importlib.metadata import distribution; "
             "d=distribution('priorfolio'); "
             "assert all(callable(e.load()) for e in d.entry_points)"], case)
        project = case / "example"
        project.mkdir()
        expected = {"design.txt": b"Example design.\n", "empty.bin": b"", "caf\u00e9.txt": b"UTF-8 filename.\n",
                    ".gitignore": b"ignored.txt\n"}
        for name, content in expected.items():
            (project / name).write_bytes(content)
        (project / "ignored.txt").write_text("Excluded by the project's ignore rule.")
        run([command, "--add", project, "--select", "all", "--out", case / "runs"], case)
        folders = list((case / "runs").iterdir())
        require(len(folders) == 1, "Expected one evidence run.")
        folder = folders[0]
        record = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
        require(record["tool_version"] == version, "Evidence uses the wrong tool version.")
        require(not record["projects"][0]["warnings"], "Fixture unexpectedly produced warnings.")
        with zipfile.ZipFile(folder / record["projects"][0]["snapshot"]) as snapshot:
            require({name: snapshot.read(name) for name in snapshot.namelist()} == expected, "Snapshot content differs.")
        backup = case / "backup.zip"
        run([command, "--backup", folder, "--destination", backup], case)
        checksum = hashlib.sha256(backup.read_bytes()).hexdigest()
        require(checksum in Path(str(backup) + ".sha256").read_text(), "Backup checksum differs.")
        restored = case / "restored"
        with zipfile.ZipFile(backup) as archive:
            archive.extractall(restored)  # Our own freshly generated synthetic archive.
        bundle = hashlib.sha256((folder / "evidence.json").read_bytes()).hexdigest()
        verification = [command, "--verify", restored, "--expect", bundle]
        run(verification + ["--against-disk"], case)
        (project / "design.txt").write_bytes(b"Changed after recording.\n")
        require(run(verification + ["--against-disk"], case, success=False).returncode == 1,
                "Verification missed a changed source file.")
        run(verification, case)
    print(f"PriorFolio {version}: package contents, entry points, snapshots, backup/restore and tamper checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-version")
    arguments = parser.parse_args()
    check(arguments.directory, arguments.expected_version)
