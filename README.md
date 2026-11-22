# PriorFolio

[![Tests](https://github.com/jesse-anderson/PriorFolio/actions/workflows/tests.yml/badge.svg)](https://github.com/jesse-anderson/PriorFolio/actions/workflows/tests.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/jesse-anderson/PriorFolio/main/.github/coverage-badge.json)](https://github.com/jesse-anderson/PriorFolio/actions/workflows/tests.yml)
[![Desktop builds](https://github.com/jesse-anderson/PriorFolio/actions/workflows/desktop.yml/badge.svg)](https://github.com/jesse-anderson/PriorFolio/actions/workflows/desktop.yml)
[![PyPI](https://img.shields.io/pypi/v/priorfolio?include_prereleases)](https://pypi.org/project/priorfolio/)
[![License: GPL-3.0-only](https://img.shields.io/badge/license-GPL--3.0--only-blue)](https://github.com/jesse-anderson/PriorFolio/blob/main/LICENSE)

Prepare IP disclosures and preserve the evidence behind them.

![PriorFolio displaying a synthetic example disclosure](https://raw.githubusercontent.com/jesse-anderson/PriorFolio/main/docs/preview.png)

**Alpha release:** CI exercises the desktop and command-line workflows on
Windows, Linux, and macOS with Python 3.10, 3.12, and 3.15.

PriorFolio records local projects, lets you describe the work and your
contribution, and produces a disclosure plus private evidence. It hashes the
actual files on disk, including uncommitted edits. It never commits, stages,
fetches, or writes inside the projects being scanned.

## Run

Requires Python 3.10+ and Git. The desktop interface also requires Tkinter
(included with the standard Windows Python installer; some Linux installations
need their distribution's Tk package). Runtime code uses the Python standard
library. Windows is also exercised locally using real Tk windows.

From a source checkout, install in a virtual environment and launch:

```console
python -m pip install .
priorfolio --gui
priorfolio --help
```

`python gui.py` also works directly from a checkout. The installed desktop
launcher is `priorfolio-gui`; `python -m priorfolio --gui` is an alternative.

Install the pinned alpha release from PyPI:

```console
python -m pip install "priorfolio==0.1.0a3"
priorfolio --gui
```

Git must be installed separately and available on `PATH`. Tkinter is needed
only for the desktop interface; pip cannot install the system Tk libraries.

## Desktop downloads and builds

The **Desktop builds** workflow creates Windows x64, Linux x64 and macOS arm64
archives with a windowed **PriorFolio** app and a separate **PriorFolioCLI**.
Python and Tk are bundled; Git is still installed separately. Download an archive
from [GitHub releases](https://github.com/jesse-anderson/PriorFolio/releases),
extract it completely and keep each application folder intact.
Initial builds are unsigned and macOS builds are not notarized.

To build locally on the operating system you intend to distribute for:

```console
python -m pip install -e ".[desktop]"
python scripts/build_desktop.py
```

The build checks GUI startup without opening a visible window and exercises
generation, snapshots, backup export and restoration through the frozen CLI.
Archives and SHA-256 sidecars go to `dist/desktop/`. Linux builds use Ubuntu 22.04;
macOS and Linux archives preserve the bundle's symlinks and executable permissions.
Runtime license notices are included. The Tcl/Tk fallback notices come from the
official [Tcl](https://github.com/tcltk/tcl/blob/core-8-6-13/license.terms) and
[Tk](https://github.com/tcltk/tk/blob/core-8-6-13/license.terms) 8.6 distributions.

## Disclosure workflow

1. Add a folder of projects, or **Add single project** for an individual folder.
2. Focus a project row and select **Project details**. Enter a public title,
   description, your contribution, dates or period of work, and references.
3. Tick the projects to include. Space toggles the focused row. Keep **Zip
   snapshots** enabled to preserve the actual source bytes.
4. Generate, then review the **Disclosure** tab before sharing `disclosure.zip`.
5. **Export backup** to create the private ZIP. If the run holds files whose
   names suggest secrets (`.env`, `*.pem`, `id_rsa`, credentials), PriorFolio
   offers two copies of the same timestamped record: a **full copy** to keep
   offline, and a **cloud copy** that leaves those files out.
6. Put the copy you send in a password-protected 7-Zip archive with encrypted
   file names (format 7z, AES-256, **Encrypt file names**; or
   `7z a -t7z -mhe=on -p backup.7z backup.zip`). Keep the password in a password
   manager. Email the `.7z` to yourself using the **Backup email** text, or save
   it to a private cloud-sync folder.
7. Check the received email attachment or the completed upload through the
   provider's web interface, ideally from another device. Extract it, compare
   the ZIP's SHA-256 with its `.sha256` sidecar, then **Confirm remote backup**.

Saving a local ZIP is not a completed remote backup. PriorFolio distinguishes
**backup required**, **exported**, and **confirmed by you**. It does not send
email, upload files, or independently verify delivery. An email containing only
a hash does not back up the underlying source files. If the archive exceeds
your mail provider's attachment limit, or the provider rejects password-protected
attachments, use private cloud storage.

The desktop app remembers the latest run and its pending backup when reopened,
and remembers earlier runs through **Previous runs**. Confirming a newer backup
does not clear reminders for older unconfirmed runs. The app checks all saved
runs before closing.

Generated document previews are read-only and match the saved exports. Edit
**Project details** and generate a new run to change a disclosure. Opening another
run switches the documents and backup actions together.

Runs default to `~/PriorFolio/runs/`. GUI sources, disclosure details and the
list of runs are stored privately in `~/.priorfolio/settings.json`, which the
GUI and the command line share. There is no telemetry.

## What can be shared?

| Output | Contents and intended use |
|---|---|
| `disclosure.md`, `disclosure.json`, `disclosure.zip` | Project titles, descriptions, contributions, supplied dates/references, commit identifiers, content hashes and counts. Review before sharing. |
| `evidence.json`, `evidence.md`, `manifests/` | **Private** local paths, remote identifiers, source file names, hashes and warnings. |
| `snapshots/` | **Private** ZIPs containing the exact source bytes hashed. |
| `evidence_bundle.zip` | **Private metadata only**, without snapshots. Not a complete source backup. |
| Exported backup ZIP (full copy) | **Private complete evidence**, including snapshots, metadata, disclosure documents, and timestamp responses. Keep offline if it holds secrets. |
| Exported backup ZIP (cloud copy) | The same record and timestamps, with the files listed in `WITHHELD.txt` left out of the snapshots. Verifies everything except those files. |
| `email.txt`, `BACKUP.txt` | Email-to-yourself template and backup/restore instructions. |
| `backup_status.json` | Local export receipt and your remote-copy confirmation; excluded from disclosures and exported backups. |

Disclosure exports omit automatic local paths, remote URLs, file listings,
and source contents. Anything you type into the disclosure fields is shared
as entered. The backup ZIP itself is **not encrypted**; protect it with a 7-Zip
password before it leaves your computer. A cloud copy withholds files by name
only, so a secret written inside an ordinary source file stays in it. The
withheld files' hashes remain in the record, so the timestamps still cover
them; their contents exist only in the full copy, which is then your only copy
of those files. Use storage you control with suitable access restrictions. PriorFolio deliberately preserves exact evidence rather than
silently removing source files.

## Command line

```console
priorfolio --root D:\projects --list
priorfolio --add D:\projects\example --select all
priorfolio --root D:\projects --select "1,4-7,example-project" --out D:\records
priorfolio --backup RUN_FOLDER --destination E:\Offline\PriorFolio-full.zip
priorfolio --backup RUN_FOLDER --destination D:\CloudSync\PriorFolio-cloud.zip --withhold-secrets
priorfolio --confirm-backup RUN_FOLDER --backup-method cloud
priorfolio --runs
```

`--withhold-secrets` leaves out files whose names suggest secrets;
`--withhold PATTERN` (repeatable) leaves out files whose path or name matches,
such as `"*.pem"` or `"config/secrets/*"`. Exporting the full copy first lets
the receipt record both copies.

Snapshots and signed timestamps are on by default. `--no-snapshot` creates a
hash-only record that cannot provide a complete source backup. `--no-timestamp`
keeps the run offline. Every new run is read back and verified before it is
reported as finished. `--add` alone scans only the supplied
projects. With no roots or individual projects, the CLI scans the current folder.
`--runs` lists the runs remembered by the GUI and the CLI with their backup
state. Runs are remembered when they are generated, and when `--verify` finds
their evidence intact.

Disclosure details are shared with the GUI and used by every run. Set them for
one project, show them, or edit many at once in a JSON file:

```console
priorfolio --root D:\projects --details example-project --title "Example" --contribution "Sole author"
priorfolio --root D:\projects --details all
priorfolio --root D:\projects --export-details D:\details.json
priorfolio --import-details D:\details.json
```

`--details` takes the same selection syntax as `--select`. The other fields are
`--description`, `--period` and `--references`; an empty string clears a field.
`--import-details` saves the fields each entry lists and keeps the rest.

## Verify and restore

Unpack your own complete backup ZIP into an empty directory, then:

```console
priorfolio --verify RESTORED_FOLDER --expect RECORDED_BUNDLE_SHA256
priorfolio --verify RESTORED_FOLDER --expect RECORDED_BUNDLE_SHA256 --against-disk
priorfolio --verify UNPACKED_METADATA_BUNDLE --metadata-only --expect RECORDED_BUNDLE_SHA256
```

Full verification requires every recorded snapshot. A cloud copy verifies every
file it contains and reports the files listed in `WITHHELD.txt` as withheld,
never as checked. **Metadata only** explicitly skips snapshot checking; it never
certifies a complete backup. The GUI exposes
the same option. Project paths are read only when **Compare local projects** or
`--against-disk` is enabled. Enable that only on a run you created or trust.
Comparisons report changed/missing files, not newly added files.

The bundle hash is SHA-256 of `evidence.json`; keep it separately, such as in
your email. A folder containing its own hashes only proves self-consistency.
An independently retained expected hash ties verification to your earlier record.
The backup ZIP's sidecar hash is a separate checksum of the entire exported ZIP.

The content hash is SHA-256 of a UTF-8 manifest, sorted by relative path, with
one SHA-256 per file. Manifests follow `sha256sum -c` escaping conventions.
Line-ending conversions or Unicode filename normalization can change the bytes
on a new checkout. Keep the snapshots to reproduce the recorded state exactly.

## Signed timestamps

The content hash records *what* existed; a signed timestamp records *when*.
Timestamp requests are **on by default**. Each run sends the SHA-256 of
`evidence.json` and a random nonce to DigiCert, Sectigo and FreeTSA. No
project names, file names or source bytes are sent. **Like any web request,
this reveals your IP address to those services.** Untick **Request timestamps**
or pass `--no-timestamp` for a fully offline run.

Each response is checked when it arrives and again by every verification:

- the response must answer this request (status, hash and nonce);
- the signed token must contain the SHA-256 of this exact `evidence.json`;
- its signature must chain to that authority's root certificate, checked at the
  time inside the token so old runs stay verifiable after certificates expire.

The three root certificates are pinned in PriorFolio rather than taken from the
operating system. Their SHA-256 fingerprints and sources are listed beside
`TSA_ROOTS` in `priorfolio.py`. FreeTSA's root is self-signed and trusted as
its operator publishes it; DigiCert's and Sectigo's chain to roots in common
trust stores. A response whose signature fails is discarded and reported.

Signature checks use OpenSSL 3, which Git for Windows includes and most Linux
systems provide. On macOS, install it with Homebrew (`brew install openssl@3`);
the system LibreSSL cannot check these signatures. Without OpenSSL 3, responses
are still saved but reported as **signature not checked**, never as verified.
Two endpoints use plain HTTP, which is safe because the signature, not the
connection, is what PriorFolio verifies.

To check a response independently, save the certificates from `TSA_ROOTS` as
`tsa-roots.pem` (or use roots you trust), then:

```console
openssl ts -reply -in timestamps/digicert.tsr -text
openssl ts -reply -in timestamps/digicert.tsr -token_out -out token.der
openssl cms -verify -inform DER -in token.der -binary -CAfile tsa-roots.pem -purpose timestampsign -out tstinfo.der
openssl sha256 evidence.json
```

The first command shows the token's time and its "Message data" hash, which
must equal the SHA-256 of `evidence.json`. The `cms -verify` command checks the
signature and certificate chain. OpenSSL's `ts -verify` also works for DigiCert
and FreeTSA, but rejects Sectigo's tokens because of how they list certificates.

## Scope and limits

- Hashes identify bytes; they do not establish authorship, ownership or a date.
  Git commit dates and user-entered dates are recorded claims.
- Git-tracked files and untracked, non-ignored files are included. Dependency
  folders, caches and virtual environments are skipped when untracked.
- Symlinks, junctions, nested repositories and submodules are not followed.
  Add nested repositories separately. Every intentional exclusion is noted.
- A repository's own Git settings can name programs for Git to run, such as a
  clean filter. When they do, PriorFolio skips `git status` for that project so
  it never starts them, and says so in the project list. Its files are still
  hashed; only the uncommitted-change counts are left out.
- Read failures produce warnings. Review them; a verified incomplete record
  remains incomplete. This is a sequential snapshot, not an atomic filesystem freeze.
- PriorFolio is not legal advice. Review disclosure wording for your situation.

## Development and license

```console
python -m pip install -e ".[dev]"
python -m pytest
python -m pytest --cov --cov-report=term-missing --cov-report=json:coverage.json
python -m build
python -m twine check --strict dist/*
python scripts/check_dist.py dist
```

Tests create synthetic projects in temporary directories. GUI tests exercise
real Tk windows and skip when a display is unavailable. Set
`PRIORFOLIO_TEST_OFFSCREEN=1` to keep test windows outside the visible desktop;
this still requires a working display. `PRIORFOLIO_NETWORK=1`
enables live timestamp-service tests; `PRIORFOLIO_SLOW=1` enables a 2.2 GB fixture.
The coverage badge reports measured line coverage of the CLI, export logic and
GUI, including subprocess execution. CI also records branch coverage. Build and
launcher scripts are outside that application metric. The badge updates after
the full test matrix passes on `main`; PyPI's version badge resolves after the
first package upload.
See [Contributing](https://github.com/jesse-anderson/PriorFolio/blob/main/CONTRIBUTING.md),
[Security](https://github.com/jesse-anderson/PriorFolio/blob/main/SECURITY.md), and
[Releasing](https://github.com/jesse-anderson/PriorFolio/blob/main/RELEASING.md).

Copyright (c) 2026 Jesse Anderson. Licensed under the **GNU General Public
License, version 3 only (GPL-3.0-only)**; see
[LICENSE](https://github.com/jesse-anderson/PriorFolio/blob/main/LICENSE). Provided without
warranty. The application license does not change the license of your inputs.
