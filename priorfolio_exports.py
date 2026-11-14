"""Disclosure documents and explicit, user-confirmed private backups.

SPDX-License-Identifier: GPL-3.0-only
Copyright (c) 2026 Jesse Anderson
"""
import datetime
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path


DETAIL_FIELDS = ("title", "description", "contribution", "period", "references")
BACKUP_INSTRUCTIONS = """PriorFolio private backup

1. Export a private backup ZIP, including snapshots, using Export backup or
   priorfolio --backup RUN_FOLDER --destination PATH.zip.
   If the run holds files that may contain secrets (.env, keys, credentials),
   export two copies of the same record: a FULL copy to keep offline, such as
   on an external drive, and a CLOUD copy that withholds those files
   (priorfolio --backup RUN_FOLDER --destination CLOUD.zip --withhold-secrets).
   Both carry the same evidence.json and signed timestamps.
2. Before any copy leaves this computer, put it in a password-protected 7-Zip
   archive with encrypted file names. With the 7-Zip program, choose Add to
   archive, format 7z, encryption AES-256, and tick Encrypt file names; or run
       7z a -t7z -mhe=on -p PriorFolio-backup.7z PriorFolio-backup.zip
   Use a long password and keep it in a password manager. Without it the
   backup cannot be opened. Keep the .sha256 file's hash outside the archive,
   for example in your email.
3. Email the .7z to yourself, or save it to a cloud-sync folder and let the
   upload finish. Use email.txt as a starting point. Some mail providers
   reject large or password-protected attachments; use private cloud storage
   instead.
4. Check your mailbox's received attachment or your cloud provider's web
   interface. Download the .7z, extract it with your password, and compare the
   ZIP's SHA-256 with the hash from the .sha256 file. Ideally check from
   another device.
5. Record that check using Confirm remote backup, or
   priorfolio --confirm-backup RUN_FOLDER --backup-method email|cloud.
6. Keep the bundle hash outside the run folder. Periodically test a restore:
   unpack your own backup and run priorfolio --verify RESTORED --expect HASH.
   A cloud copy verifies every file except those listed in WITHHELD.txt.

Exported means written and hashed locally. Confirmed means you explicitly
reported checking the remote copy; PriorFolio cannot inspect your mail or cloud
account. An email containing only a hash is not a backup of your files.

The ZIP itself is not encrypted; the 7-Zip password is what protects it. It
includes source code, full local paths and remote URLs from the selected
projects. Withholding is based on file names, so a secret written inside an
ordinary source file stays in the cloud copy. Review disclosure.md or
disclosure.zip before sharing a disclosure with anyone else.

evidence_bundle.zip is a smaller, private metadata bundle. It excludes source
snapshots and is not a complete backup. Verify it with --metadata-only.
"""
WITHHELD_JSON, WITHHELD_TXT = "WITHHELD.json", "WITHHELD.txt"


# File names that commonly hold credentials or private keys.
SECRET_NAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd", ".git-credentials",
    ".dockercfg", "credentials", "credentials.json", "client_secret.json", "secrets.json",
    "secrets.yaml", "secrets.yml", "secrets.toml", "token.json", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", "terraform.tfstate",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk",
                   ".tfstate", ".tfvars")
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def looks_secret(relative):
    name = relative.rsplit("/", 1)[-1].lower()
    if name.endswith(TEMPLATE_SUFFIXES):
        return False
    return name in SECRET_NAMES or name.startswith(".env.") or name.endswith(SECRET_SUFFIXES)


def recorded_files(run_dir):
    """(project name, relative path) for every file in a run's manifests."""
    import priorfolio as core
    data = json.loads(core.safe_child(run_dir, "evidence.json").read_text(encoding="utf-8"))
    files = []
    for p in data["projects"]:
        entries = core.read_manifest(core.safe_child(run_dir, p["manifest"]).read_bytes())
        files += [(p["name"], rel) for rel in sorted(entries)]
    return files


def likely_secrets(run_dir):
    """'project: path' for each recorded file whose name suggests credentials or keys."""
    try:
        return [f"{name}: {rel}" for name, rel in recorded_files(run_dir) if looks_secret(rel)]
    except (OSError, ValueError, KeyError, TypeError):
        return []  # export_backup reports a damaged run in its own words


def select_withheld(run_dir, patterns=(), secrets=False):
    """'project: path' entries to leave out of a cloud copy.

    A pattern matches a file's relative path or its name (fnmatch, case
    sensitive), for example ".env", "*.pem" or "config/secrets/*".
    secrets=True adds every likely secret.
    """
    import fnmatch
    chosen = []
    for name, rel in recorded_files(run_dir):
        base = rel.rsplit("/", 1)[-1]
        if (secrets and looks_secret(rel)) or any(
                fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(base, p) for p in patterns):
            chosen.append(f"{name}: {rel}")
    return chosen


def withheld_record(run_dir):
    """The WITHHELD.json record of a cloud copy, or None for a complete run.

    Raises ValueError when the file exists but cannot be used.
    """
    import priorfolio as core
    path = core.safe_child(run_dir, WITHHELD_JSON)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get("format") != "priorfolio-withheld-v1"
            or not isinstance(data.get("bundle_sha256"), str)
            or not isinstance(files, list) or not files
            or not all(isinstance(f, dict) and all(isinstance(f.get(k), str) for k in
                                                   ("project", "manifest", "path", "sha256"))
                       for f in files)):
        raise ValueError(f"{WITHHELD_JSON} is not a PriorFolio withheld-file list")
    return data


def withheld_files(run_dir):
    """The withheld-file entries of a cloud copy, or [] for a complete run."""
    record = withheld_record(run_dir)
    return record["files"] if record else []


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


# ---- settings shared by the CLI and the desktop app ----

def settings_path():
    """~/.priorfolio/settings.json, or the file named by PRIORFOLIO_SETTINGS."""
    return Path(os.environ.get("PRIORFOLIO_SETTINGS") or Path.home() / ".priorfolio" / "settings.json")


def read_settings(path=None):
    try:
        data = json.loads(Path(path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_settings(change, path=None):
    """Re-read the file, apply change(data) in place and replace it atomically, so
    the CLI and an open desktop app only overwrite the keys they changed."""
    path = Path(path or settings_path())
    data = read_settings(path)
    change(data)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".priorfolio-settings-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def clean_details(value):
    """Only the known text fields of one project's disclosure details."""
    if not isinstance(value, dict):
        return {}
    return {field: text for field, text in value.items() if field in DETAIL_FIELDS and isinstance(text, str)}


def saved_details(path=None):
    """Disclosure details by project path."""
    saved = read_settings(path).get("details")
    if not isinstance(saved, dict):
        return {}
    return {key: clean_details(value) for key, value in saved.items() if isinstance(value, dict)}


def save_details(changes, path=None):
    """Store {project path: details}, replacing the details of those projects only."""
    def change(data):
        details = data.get("details")
        if not isinstance(details, dict):
            details = data["details"] = {}
        for key, value in changes.items():
            details[key] = clean_details(value)
    update_settings(change, path)


def saved_runs(path=None):
    """Remembered run folders, most recent first."""
    saved = read_settings(path)
    values = saved.get("runs")
    values = [saved.get("last_run")] + (values if isinstance(values, list) else [])
    return list(dict.fromkeys(Path(value).resolve() for value in values if isinstance(value, str) and value))


def record_runs(runs, path=None):
    """Put these run folders first in the remembered runs, keeping all others."""
    runs = [Path(run).resolve() for run in runs]

    def change(data):
        old = data.get("runs")
        old = [Path(value).resolve() for value in (old if isinstance(old, list) else [])
               if isinstance(value, str) and value]
        data["runs"] = [str(run) for run in dict.fromkeys(runs + old)]
    update_settings(change, path)


def public_text(value):
    """Make user-authored text literal Markdown; do not render embedded HTML."""
    value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in ("\\", "`", "*", "_", "[", "]", "#"):
        value = value.replace(char, "\\" + char)
    return value


def write_disclosure(out_dir, projects, meta, bundle):
    """Allowlist public fields; never copy the private project dictionary."""
    entries = []
    for p in projects:
        details = p.get("disclosure", {})
        entries.append({
            "title": details.get("title") or p["name"],
            **{key: details.get(key, "") for key in DETAIL_FIELDS if key != "title"},
            "commit": p["commit"], "commit_date": p["commit_date"],
            "content_sha256": p["content_sha256"], "file_count": p["file_count"],
            "has_warnings": bool(p["warnings"]),
        })
    data = {"format": "priorfolio-disclosure-v1", "generated_utc": meta["generated_utc"],
            "private_evidence_sha256": bundle, "projects": entries}
    write_json(out_dir / "disclosure.json", data)
    lines = ["# PriorFolio IP disclosure", "", f"Prepared: {meta['generated_utc']}", "",
             "Review the descriptions, contributions, dates, and project titles before sharing.",
             "This document records the information supplied by its author; hashes do not establish ownership.",
             "", f"Private evidence record SHA-256: `{bundle}`", ""]
    for i, p in enumerate(entries, 1):
        lines += [f"## {i}. {public_text(p['title'])}", ""]
        for key, label in (("description", "Description"), ("contribution", "My contribution"),
                           ("period", "Work dates or period"), ("references", "References")):
            lines += [f"**{label}:** {public_text(p[key]) if p[key] else '[Not provided]'}", ""]
        lines += [f"- Content SHA-256: `{p['content_sha256'] or 'no files'}`",
                  f"- Git commit: `{p['commit'] or 'not available'}`",
                  f"- Git commit date: {public_text(p['commit_date'] or 'not available')}",
                  f"- Files recorded: {p['file_count']}", ""]
        if p["has_warnings"]:
            lines += ["WARNING: the private evidence record reports missing or unreadable content.", ""]
    text = "\n".join(lines)
    (out_dir / "disclosure.md").write_text(text, encoding="utf-8", newline="\n")
    with zipfile.ZipFile(out_dir / "disclosure.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("disclosure.json", "disclosure.md"):
            archive.write(out_dir / name, name)
    return text


def write_backup_instructions(out_dir):
    (out_dir / "BACKUP.txt").write_text(BACKUP_INSTRUCTIONS, encoding="utf-8", newline="\n")


def backup_status(run_dir):
    import priorfolio as core
    try:
        data = json.loads(core.safe_child(run_dir, "backup_status.json").read_text(encoding="utf-8"))
        if (not isinstance(data, dict)
                or data.get("state") not in ("exported", "confirmed_by_user")
                or not all(isinstance(data.get(key), str) and data[key]
                           for key in ("archive", "archive_sha256", "exported_utc", "bundle_sha256"))
                or not re.fullmatch(r"[0-9a-f]{64}", data["archive_sha256"])
                or data.get("bundle_sha256") != core.sha256_file(
                core.safe_child(run_dir, "evidence.json"))):
            return {"state": "backup_required"}
        if data["state"] == "confirmed_by_user" and (
                data.get("method") not in ("email", "cloud")
                or not isinstance(data.get("confirmed_utc"), str) or not data["confirmed_utc"]):
            return {"state": "backup_required"}
        # A cloud copy lists what it withholds, and may name the full copy exported before it.
        withheld, full = data.get("withheld"), data.get("full_copy")
        if withheld is not None and not (isinstance(withheld, list) and withheld
                                         and all(isinstance(w, str) and w for w in withheld)):
            return {"state": "backup_required"}
        if full is not None and not (
                isinstance(full, dict)
                and all(isinstance(full.get(k), str) and full[k] for k in ("archive", "archive_sha256", "exported_utc"))
                and re.fullmatch(r"[0-9a-f]{64}", full["archive_sha256"])):
            return {"state": "backup_required"}
        return data
    except (OSError, ValueError, TypeError):
        return {"state": "backup_required"}


def save_status(run_dir, status):
    import priorfolio as core
    target = core.safe_child(run_dir, "backup_status.json")
    temp = core.safe_child(run_dir, "backup_status.json.tmp")
    write_json(temp, status)
    os.replace(temp, target)


def copy_stream(source, target):
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1 << 20), b""):
        target.write(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def write_redacted_snapshot(source_path, keep, target_path):
    """Copy a snapshot zip without withheld files, checking every kept file's hash."""
    import priorfolio as core
    seen = set()
    with zipfile.ZipFile(source_path) as original, \
            zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as copy:
        for info in original.infolist():
            if info.is_dir() or info.filename not in keep:
                continue
            entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            entry.compress_type, entry.external_attr = zipfile.ZIP_DEFLATED, info.external_attr
            with original.open(info) as source, \
                    copy.open(entry, "w", force_zip64=info.file_size > 0x7FFF0000) as target:
                if copy_stream(source, target) != keep[info.filename]:
                    raise core.ToolError(f"Evidence changed during backup: {source_path.name}: {info.filename}")
            seen.add(info.filename)
    if seen != set(keep):
        raise core.ToolError(f"Snapshot {source_path.name} is missing recorded files.")


def export_backup(run_dir, destination, withhold=()):
    """Export verified private evidence exclusively; never overwrite a backup.

    Stream snapshot bytes into the outer ZIP and check against the recorded
    hashes while copying. Unknown files and local backup receipts stay out.

    withhold lists "project: path" entries (see select_withheld) to leave out
    of the source snapshots, for a copy stored in the cloud. That copy keeps
    the same evidence.json, manifests and timestamps, and lists what it
    withholds in WITHHELD.json and WITHHELD.txt.
    """
    import tempfile
    import priorfolio as core
    run_dir, destination = Path(run_dir).resolve(), Path(destination).absolute()
    try:
        already_redacted = bool(withheld_files(run_dir))
    except (OSError, ValueError):
        already_redacted = True
    if already_redacted:
        raise core.ToolError("This folder is a cloud copy with withheld files. Export from the original run.")
    problems = core.verify_run(run_dir)
    if problems:
        raise core.ToolError("Evidence verification failed before backup: " + "; ".join(problems[:5]))
    data = json.loads(core.safe_child(run_dir, "evidence.json").read_text(encoding="utf-8"))
    if any(p["file_count"] and not p.get("snapshot") for p in data["projects"]):
        raise core.ToolError("This run has no complete source snapshots. Generate again with Zip snapshots enabled.")
    bundle = core.sha256_file(core.safe_child(run_dir, "evidence.json"))

    # Resolve what to withhold against the manifests: project -> {path: sha256}.
    requested, withheld = set(withhold), {}
    by_project = {p["name"]: p for p in data["projects"]}
    for item in sorted(requested):
        name, _, rel = item.partition(": ")
        project = by_project.get(name)
        entries = core.read_manifest(core.safe_child(run_dir, project["manifest"]).read_bytes()) if project else {}
        if rel not in entries:
            raise core.ToolError(f"Cannot withhold {item}: it is not recorded in this run.")
        withheld.setdefault(name, {})[rel] = entries[rel]

    expected = {"evidence.json": bundle}
    names = {"evidence.json", "BUNDLE_SHA256.txt"}
    redact = {}  # snapshot path -> files to keep, for projects with withheld files
    for p in data["projects"]:
        names.add(p["manifest"])
        expected[p["manifest"]] = p["content_sha256"] or hashlib.sha256(b"").hexdigest()
        if p.get("snapshot"):
            names.add(p["snapshot"])
            if p["name"] in withheld:
                entries = core.read_manifest(core.safe_child(run_dir, p["manifest"]).read_bytes())
                redact[p["snapshot"]] = {rel: digest for rel, digest in entries.items()
                                         if rel not in withheld[p["name"]]}
            else:
                expected[p["snapshot"]] = p["snapshot_sha256"]
    extras = {}
    if withheld:
        listed = [{"project": name, "manifest": by_project[name]["manifest"], "path": rel, "sha256": digest}
                  for name in sorted(withheld) for rel, digest in sorted(withheld[name].items())]
        extras[WITHHELD_JSON] = (json.dumps({"format": "priorfolio-withheld-v1", "bundle_sha256": bundle,
                                             "files": listed}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        extras[WITHHELD_TXT] = ("This is a cloud copy of a PriorFolio run. These files were left out of its\n"
                                "source snapshots. Their hashes remain in the manifests and evidence.json,\n"
                                "so the timestamps still cover them, but their contents are only in the\n"
                                "full copy kept elsewhere.\n\n"
                                + "".join(f"{f['project']}: {f['path']}  sha256 {f['sha256']}\n" for f in listed)
                                ).encode("utf-8")
    for name in ("evidence.md", "email.txt", "BACKUP.txt", "disclosure.json", "disclosure.md",
                 "disclosure.zip", "evidence_bundle.zip", "timestamps/timestamps.json"):
        if core.safe_child(run_dir, name).is_file():
            names.add(name)
    for pattern in ("*.tsr", "*.tsq"):
        names.update(p.relative_to(run_dir).as_posix()
                     for p in core.safe_child(run_dir, "timestamps").glob(pattern))
    sources = {name: core.safe_child(run_dir, name) for name in names}
    if destination.resolve().is_relative_to(run_dir):
        raise core.ToolError("Choose a backup destination outside this run folder.")
    sidecar = Path(str(destination) + ".sha256")
    if destination.exists() or destination.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise core.ToolError("That backup or checksum already exists. Choose a new filename.")
    previous = backup_status(run_dir)
    created = False
    sidecar_created = False
    try:
        with tempfile.TemporaryDirectory(prefix="priorfolio-backup-") as work, destination.open("xb") as output:
            created = True
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                for name, source_path in sorted(sources.items()):
                    if name in redact:
                        source_path = Path(work) / "snapshot.zip"
                        write_redacted_snapshot(sources[name], redact[name], source_path)
                    with source_path.open("rb") as source:
                        # Zip64 headers only where needed: forcing them on every entry
                        # makes Info-ZIP's unzip report errors for non-ASCII names.
                        large = os.fstat(source.fileno()).st_size > 0x7FFF0000
                        with archive.open(name, "w", force_zip64=large) as target:
                            digest = copy_stream(source, target)
                    if name in expected and digest != expected[name]:
                        raise core.ToolError("Evidence changed during backup: " + name)
                for name, content in sorted(extras.items()):
                    archive.writestr(name, content)
            output.flush()
            os.fsync(output.fileno())
        digest = core.sha256_file(destination)
        with sidecar.open("x", encoding="utf-8", newline="\n") as checksum:
            sidecar_created = True
            checksum.write(core.manifest_line(digest, destination.name))
        status = {"state": "exported", "exported_utc": utc_now(), "archive": str(destination),
                  "archive_sha256": digest, "bundle_sha256": bundle}
        if withheld:
            status["withheld"] = sorted(requested)
            # Remember the full copy this cloud copy complements.
            if previous.get("state") in ("exported", "confirmed_by_user"):
                full = previous.get("full_copy") if previous.get("withheld") else {
                    key: previous[key] for key in ("archive", "archive_sha256", "exported_utc")}
                if full:
                    status["full_copy"] = full
    except BaseException:
        if sidecar_created:
            sidecar.unlink(missing_ok=True)
        if created:
            destination.unlink(missing_ok=True)
        raise
    # The backup is complete. A failure to save local bookkeeping must not
    # destroy the verified archive or its checksum.
    try:
        save_status(run_dir, status)
    except (OSError, ValueError) as e:
        raise core.ToolError(f"Backup saved to {destination} (SHA-256 {digest}), but the local "
                             f"export receipt could not be saved: {e}. Keep the ZIP and its checksum.") from e
    return status


def confirm_backup(run_dir, method):
    """Record the user's remote-copy check, not an automatic delivery claim."""
    import priorfolio as core
    if method not in ("email", "cloud"):
        raise core.ToolError("Choose email or cloud as the backup method.")
    status = backup_status(run_dir)
    if status.get("state") not in ("exported", "confirmed_by_user"):
        raise core.ToolError("Export a complete backup before confirming its remote copy.")
    status.update(state="confirmed_by_user", method=method, confirmed_utc=utc_now())
    save_status(run_dir, status)
    return status
