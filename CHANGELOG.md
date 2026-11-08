# Changelog

## 0.1.0a3 - signed timestamps and cloud backup copies

- Signed timestamps are on by default (`--no-timestamp` to stay offline), and
  the CLI and desktop app say that requesting them reveals your IP address.
- Timestamp signatures are now verified with OpenSSL 3 against pinned DigiCert,
  Sectigo and FreeTSA root certificates, when collected and on every
  verification. Responses that fail are discarded; without OpenSSL 3 they are
  kept and reported as not checked.
- Every new run is read back and verified before it is reported as finished.
- Scanning skips `git status` in repositories whose own config defines clean
  filters or includes other files, so it never starts programs they name.
- When a run holds files whose names suggest secrets (`.env`, `*.pem`,
  credentials), export can make two copies of the same timestamped record: a
  full copy to keep offline and a cloud copy without those files
  (`--withhold-secrets`, `--withhold PATTERN`). The cloud copy lists them in
  WITHHELD.txt and verifies every other file.
- Backup instructions, the email template and help text now say to put each
  copy in a password-protected 7-Zip archive with encrypted file names.
- The installed `priorfolio-gui` launcher explains how to install Tkinter when
  it is missing, instead of showing a traceback.
- Desktop build checksum sidecars use LF line endings on every platform, so
  `sha256sum -c` accepts the Windows sidecar.

## 0.1.0a2 - first PyPI release

- Exported backup ZIPs no longer add zip64 headers to small entries, so Info-ZIP
  `unzip` restores backups of projects with non-ASCII names without errors.
- `priorfolio --gui` explains how to install Tkinter when it is missing,
  instead of showing a traceback.
- Deleted run folders no longer keep the desktop app asking about a pending
  backup on every close; Previous runs labels them FOLDER NOT FOUND.
- Fuller PyPI classifiers, and tests that keep the version, classifiers and
  source-archive allowlist consistent.

## 0.1.0a1 - initial alpha

- Describe projects and export a disclosure separately from private evidence.
- Record source hashes, Git metadata, exclusions, and exact source snapshots.
- Export complete private backup ZIPs with checksums and email/cloud reminders.
- Restore and verify archives against a separately retained evidence hash.
- Navigate previous runs and track each run's user-confirmed remote backup.
- Provide desktop and command-line entry points under GPL-3.0-only.
- Build standalone GUI and CLI downloads for Windows, Linux, and macOS.
- Keep project rows and scroll controls usable on smaller displays.
- Check Python packages, native builds, and measured coverage in GitHub Actions.

The application does not upload backups, encrypt archives, validate timestamp
signatures, or establish ownership or authorship. Review warnings and keep a
separate remote copy of the complete private evidence.
