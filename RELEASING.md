# Releasing TokenAlarm

The release workflow (`.github/workflows/release.yml`) runs when a tag `vX.Y.Z` is pushed:
tests → build (sdist + wheel, `twine check`) → GitHub Release (notes from `CHANGELOG.md`) → PyPI.

## One-time setup

1. **Choose a license and add a `LICENSE` file.** The PyPI job refuses to publish without one —
   an upload cannot be undone, and code without a license may not legally be used by others.
   (MIT is the common choice for small tools.) Also add `license = "MIT"` (or yours) to `pyproject.toml`.
2. **Reserve the PyPI name.** On https://pypi.org/manage/account/publishing/ add a *pending publisher*:
   project `tokenalarm`, owner `JMH-Chem`, repository `ai-token-alarm-and-auto-start`,
   workflow `release.yml`, environment `pypi`. (Do this early — until then anyone could take the name.)
3. In the GitHub repository settings create the environments `pypi` and `testpypi`
   (optionally with required reviewers).
4. Optional rehearsal: add the same pending publisher on https://test.pypi.org (environment `testpypi`)
   and run the workflow manually ("Run workflow") → publishes to TestPyPI.
5. Make `main` the default branch (Settings → General → Default branch).

## Each release

1. Update `src/tokenalarm/__init__.py` (`__version__`) and add a section to `CHANGELOG.md`.
2. Merge to `main`, wait for the `test` workflow (includes the Windows installer smoke test).
3. Tag and push:
   ```bash
   git tag v0.2.0
   ```
   ```bash
   git push origin v0.2.0
   ```
4. After the first successful PyPI upload, switch the installer default to PyPI:
   in `install.ps1` set `$defaultSource` to `'pypi'` (the GitHub archive stays available via
   `$env:TOKENALARM_SOURCE`).
