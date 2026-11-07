"""Disclosure documents and explicit, user-confirmed private backups.

SPDX-License-Identifier: GPL-3.0-only
Copyright (c) 2026 Jesse Anderson
"""
import datetime
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path


DETAIL_FIELDS = ("title", "description", "contribution", "period", "references")
BACKUP_INSTRUCTIONS = """PriorFolio private backup

1. Export a complete private ZIP, including snapshots, using Export backup or
   priorfolio --backup RUN_FOLDER --destination PATH.zip.
2. Email the ZIP to yourself, or save it to a cloud-sync folder and let the
   upload finish. Use email.txt as a starting point. If the archive exceeds
   your mail provider's attachment limit, use private cloud storage.
3. Check your mailbox's received attachment or your cloud provider's web
   interface. Download the archive and compare its SHA-256 with the sidecar
   .sha256 file. Ideally check from another device.
4. Record that check using Confirm remote backup, or
   priorfolio --confirm-backup RUN_FOLDER --backup-method email|cloud.
5. Keep the bundle hash outside the run folder. Periodically test a restore:
   unpack your own backup and run priorfolio --verify RESTORED --expect HASH.

Exported means written and hashed locally. Confirmed means you explicitly
reported checking the remote copy; PriorFolio cannot inspect your mail or cloud
account. An email containing only a hash is not a backup of your files.

The private ZIP is not encrypted. It includes source code, full local paths,
remote URLs, and potentially credentials from the selected projects. Use an
account you control with appropriate access restrictions. Review disclosure.md
or disclosure.zip before sharing a disclosure with anyone else.

evidence_bundle.zip is a smaller, private metadata bundle. It excludes source
snapshots and is not a complete backup. Verify it with --metadata-only.
"""


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


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
        return data
    except (OSError, ValueError, TypeError):
        return {"state": "backup_required"}


def save_status(run_dir, status):
    import priorfolio as core
    target = core.safe_child(run_dir, "backup_status.json")
    temp = core.safe_child(run_dir, "backup_status.json.tmp")
    write_json(temp, status)
    os.replace(temp, target)


def export_backup(run_dir, destination):
    """Export verified private evidence exclusively; never overwrite a backup.

    Stream snapshot bytes into the outer ZIP and check against the recorded
    hashes while copying. Unknown files and local backup receipts stay out.
    """
    import priorfolio as core
    run_dir, destination = Path(run_dir).resolve(), Path(destination).absolute()
    problems = core.verify_run(run_dir)
    if problems:
        raise core.ToolError("Evidence verification failed before backup: " + "; ".join(problems[:5]))
    data = json.loads(core.safe_child(run_dir, "evidence.json").read_text(encoding="utf-8"))
    if any(p["file_count"] and not p.get("snapshot") for p in data["projects"]):
        raise core.ToolError("This run has no complete source snapshots. Generate again with Zip snapshots enabled.")
    bundle = core.sha256_file(core.safe_child(run_dir, "evidence.json"))
    expected = {"evidence.json": bundle}
    names = {"evidence.json", "BUNDLE_SHA256.txt"}
    for p in data["projects"]:
        names.add(p["manifest"])
        expected[p["manifest"]] = p["content_sha256"] or hashlib.sha256(b"").hexdigest()
        if p.get("snapshot"):
            names.add(p["snapshot"])
            expected[p["snapshot"]] = p["snapshot_sha256"]
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
    created = False
    sidecar_created = False
    try:
        with destination.open("xb") as output:
            created = True
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                for name, source_path in sorted(sources.items()):
                    with source_path.open("rb") as source, archive.open(name, "w", force_zip64=True) as target:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: source.read(1 << 20), b""):
                            target.write(chunk)
                            digest.update(chunk)
                    if name in expected and digest.hexdigest() != expected[name]:
                        raise core.ToolError("Evidence changed during backup: " + name)
            output.flush()
            os.fsync(output.fileno())
        digest = core.sha256_file(destination)
        with sidecar.open("x", encoding="utf-8", newline="\n") as checksum:
            sidecar_created = True
            checksum.write(core.manifest_line(digest, destination.name))
        status = {"state": "exported", "exported_utc": utc_now(), "archive": str(destination),
                  "archive_sha256": digest, "bundle_sha256": bundle}
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
