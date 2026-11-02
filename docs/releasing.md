# Releasing

A release is a signed tag `vX.Y.Z` pushed to GitHub. The tag starts `.github/workflows/release.yml`, which runs the
tests (`ci.yml`, which also builds the wheel and sdist once and tests the image built from that wheel), builds the image
from that same wheel on a native runner for `linux/amd64` and `linux/arm64` (no QEMU), joins the two into one
multi-architecture image at `ghcr.io/pengupowered/pengumon` as `X.Y.Z` and `latest`, and creates the GitHub release with
that version's section of `CHANGELOG.md` as its notes and the wheel, the sdist and `SHA256SUMS` attached. The
per-architecture tags (`X.Y.Z-amd64`, `X.Y.Z-arm64`) stay in the registry; use `X.Y.Z`.

## Cut a release

1. On a feature branch or on `main`, with the tests passing (`scripts/test.sh`, `scripts/test-ui.sh`).
2. Bump the version in one place, `__version__` in `src/pengumon/__init__.py` (pyproject.toml reads it). Also change the
   image tag in `compose.release.yaml` (`tests/test_distribution.py` checks it).
3. In `CHANGELOG.md`, move what is under `## [Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section, leave
   `[Unreleased]` empty, and update the two compare links at the bottom. Describe changes for people who run
   Pengumon, in plain words. `scripts/changelog-section.sh X.Y.Z` prints what the release notes will be.
4. Commit (`chore: release X.Y.Z`), then tag it, signed: `git tag -s vX.Y.Z -m "Pengumon X.Y.Z"`.
5. Push the commit and the tag to GitHub: `git push github main vX.Y.Z`.
6. Watch the Release workflow in the Actions tab. When it is green, check that the release page shows the notes and that
   `docker pull ghcr.io/pengupowered/pengumon:X.Y.Z` works.

The workflow stops before publishing anything if the tag does not match `__version__` or the changelog has no notes for
it. Before tagging you can run what CI runs: `podman build --target wheel-built -o type=local,dest=dist .`, then
`scripts/test-container.sh --prebuilt`.

## First release only

The first push to GitHub Container Registry creates the package as private. In the package settings on GitHub (Package
settings, Danger Zone, Change visibility) make it public once, or `docker compose up` on other machines cannot pull it.

## Notes

- Only tags like `v1.2.3` publish; a tag with a suffix (`v1.2.3-rc1`) does not trigger the workflow.
- `latest` moves with every release. The compose file pins an exact version so upgrades are deliberate.
- A failed release workflow can be re-run from the Actions tab; pushing the same image tags again overwrites them.
  The GitHub release step fails if the release already exists; delete that release and re-run the job.
