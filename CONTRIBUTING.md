# Contributing

PriorFolio prepares disclosures and preserves exact private evidence. Keep those
two outputs separate when adding fields or features.

Use Python 3.10+ and Git, install `python -m pip install -e ".[dev]"`, then run
`python -m pytest`. Changes to hashing, verification, backup, or disclosure
privacy need regression tests using synthetic fixtures. GUI changes should
exercise the real window when a display is available.
Set `PRIORFOLIO_TEST_OFFSCREEN=1` to run those windows outside the visible desktop.
After packaging changes, also run `python -m build`,
`python -m twine check --strict dist/*`, and `python scripts/check_dist.py dist`.

Do not add real evidence bundles, credentials, project snapshots, local settings,
or screenshots of personal projects. Use invented names and example.com addresses.
Never make the scanned projects writable, fetch remotes during scanning, or
silently discard evidence to make a verification check pass.

Contributions are supplied under GPL-3.0-only, the same license as this project.
Describe the observed behavior, resulting change, and checks run in pull requests.
