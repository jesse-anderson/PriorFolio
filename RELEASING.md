# Preparing a public release

The public destination is `jesse-anderson/PriorFolio`. This repository starts
with fresh history. Keep the original private project's history, credentials,
outputs and archives outside it.

## Validate the release

1. Set the version in `pyproject.toml`, `priorfolio.py`, the GUI help text, the
   README install example and `CHANGELOG.md`. The initial alpha is `0.1.0a1`.
2. Install `python -m pip install -e ".[dev]"` and run `python -m pytest`.
   `PRIORFOLIO_TEST_OFFSCREEN=1` keeps real Tk test windows off the visible desktop.
3. Build into an empty output directory with `python -m build`, then run
   `python -m twine check --strict dist/*` and `python scripts/check_dist.py dist`.
   The last command checks the archive contents, creates an isolated environment,
   installs the wheel without an index, and exercises backup and restore.
   Keep older release artifacts in a separate directory.
4. Review `git status --short` and `git diff --cached --name-only`. Include only
   source, tests, scripts, documentation, licensing, packaging and CI files.
   Never upload a ZIP of the working directory or a private evidence archive.

## Prepare GitHub

Choose an intentional public Git identity before the first commit. If personal
email should remain private, copy the exact noreply address shown in your GitHub
email settings and set it for this repository with `git config --local user.email`.
Public copyright attribution is intentionally retained in LICENSE and source.

Create `https://github.com/jesse-anderson/PriorFolio`, set its remote, and push the
reviewed source. Check the complete Windows/Linux/macOS CI matrix before tagging
the release. Enable private vulnerability reporting in repository settings.
Screenshots and issue reproductions must use synthetic projects.

Create a tag matching the package version, for example `v0.1.0a1`. Publishing is
manual: pushing a commit or tag alone does not upload a package.

## Configure Trusted Publishing

Create a pending publisher on each index if the project does not yet exist:

| Field | TestPyPI | PyPI |
|---|---|---|
| Project | `priorfolio` | `priorfolio` |
| GitHub owner | `jesse-anderson` | `jesse-anderson` |
| Repository | `PriorFolio` | `PriorFolio` |
| Workflow filename | `publish.yml` | `publish.yml` |
| Environment | `testpypi` | `pypi` |

Create the matching GitHub environments. Accounts and publisher configuration
are separate on TestPyPI and PyPI. This workflow uses short-lived OIDC credentials;
do not add PyPI API tokens or personal credentials to the repository.

See the official [Trusted Publishing guide](https://docs.pypi.org/trusted-publishers/).

## Rehearse, then publish

1. In GitHub Actions, run **Publish package** from the version tag and select
   `testpypi`. The workflow runs the test matrix, rebuilds, checks metadata and
   contents, verifies the version tag, and only then uploads the package.
2. In a fresh virtual environment, install that exact TestPyPI version:

   ```console
   python -m pip install --index-url https://test.pypi.org/simple/ --no-deps "priorfolio==0.1.0a1"
   priorfolio --version
   priorfolio --gui
   ```

   Use a synthetic folder to check generation, export and restoration. Review
   the package description, image and links on TestPyPI.
3. Once the rehearsal and CI pass, run **Publish package** from the same tag with
   `pypi` selected. Verify installation from PyPI in another fresh environment.
   Attach only the reviewed wheel and source distribution to the GitHub release.

The GUI needs Tkinter and all workflows need Git. The package does not bundle
those system dependencies. Existing private evidence must remain backed up
separately throughout the release process.
