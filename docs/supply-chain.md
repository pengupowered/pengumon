# Supply-chain policy and update procedure (pengumon)

The dependency checks here use depguard, an optional external tool (a separate project; any
equivalent hash-locking and advisory-audit tooling works for the same policy). Reference for all of its commands, exit
codes and file formats: the depguard README. Commands below run from the project directory; `$DG` stands for your
checkout of `depguard.sh`. All Python tooling runs in throwaway containers; nothing is installed on the host.

## Policy

| Rule | Effect |
|---|---|
| Any known advisory (PyPI advisory DB or OSV) for a locked `name==version` | blocks, any severity |
| Advisory id starting with `MAL-` (malicious package) | always blocks, can never be ignored |
| Release younger than 7 days (cooldown) | blocks; lowered only with `--cooldown-days N --reason "..."`, recorded as `override-reason` in the lock header |
| Release yanked on PyPI | blocks (never waived) |
| Tool, network or parse error | exit 2, a failure, never treated as clean |
| Fixable Debian package in the built image (`os-scan`) | blocks (exit 1) |
| Debian package with no fix yet | reported only |

Python packages are installed from hash-locked files (`requirements.txt` for the runtime image,
`requirements-build.txt` for the build backend, `requirements-dev.txt` for the test image) with
`pip install --require-hashes --no-deps --only-binary :all:`; the runtime image carries no test
tooling. The base image is pinned by digest (a multi-architecture index, so the same pin builds the amd64 and arm64 images).

These lock files are the dependency record: every package, its exact version and its hashes. GitHub reads them
(with `tests/ui/runner/package-lock.json`) for the repository's dependency graph and Dependabot alerts, and exports an
SBOM from it (Insights → Dependency graph → Export SBOM). `$DG sbom .` writes a CycloneDX SBOM locally when one is needed.

Runtime privileges: the container runs as uid 10001 with only `cap_net_raw` as a file capability on
the Python interpreter (needed for ICMP; the build fails if `setcap` fails). The first deploy of the
non-root image chowns the `pengumon_data` volume to 10001:0.

## Browser test tooling (npm)

The opt-in dashboard tests (`tests/test_ui_smoke.py`, `tests/test_ui_lint.py`, run by `scripts/test-ui.sh`) use three
npm packages: `puppeteer-core`, `eslint` and `globals`. They are pinned at exact versions, with all transitive packages
and integrity hashes, in `tests/ui/runner/package.json` / `package-lock.json`, and installed into the test base image
`localhost/ui-test-runner:1` that `tests/ui/runner/build.sh` builds. Rules:

- Cooldown: every pinned version was published at least 7 days before it was chosen (`npm view <pkg> time --json`),
  the same rule as for Python packages. The lock is generated in a throwaway `node:22-slim` container, never with npm
  on the host.
- The runner image installs with `npm ci --ignore-scripts`: the lockfile is followed exactly, hashes are verified and no
  dependency lifecycle script runs. The lock currently has no package with an install script; re-check after an update.
- `Dockerfile.ui` here is only `FROM localhost/ui-test-runner:1` (override with the `BASE` build arg) plus the hash-locked
  Python test requirements and the repo (image `localhost/pengumon-ui-test`). The production `Dockerfile` and
  `Dockerfile.test` contain no Node, npm packages or browser.
- Chromium comes from Debian (`apt`), Node from the official `node:22-slim` image pinned by digest (Debian's
  nodejs 20 is older than puppeteer-core's `engines.node`). Bump the digest together with the other base images, in the
  runner's `Dockerfile`.
- The tests run with `--network none`, so the browser cannot reach anything but the app on loopback. `scripts/test-ui.sh`
  starts them with its hardening flags inline (or through a shared `run.sh` when `UI_RUNNER_DIR` names one). If the
  base image is missing it stops and says to run `tests/ui/runner/build.sh`.

To update a pin: change the version in `tests/ui/runner/package.json` (respecting the cooldown), regenerate the lock in a
throwaway container (`podman run --rm -v "$PWD/tests/ui/runner:/w" -w /w node:22-slim npm install --package-lock-only
--ignore-scripts`), review the lock diff, rebuild the image and run the UI tests.

## Updating dependencies

1. Edit `requirements.in`, `requirements-dev.in` or `requirements-build.in`.
2. Lock:
   `$DG lock .`
3. Read the printed diff: `+ added`, `- removed`, `~ changed`, flags `NEW (unapproved)`, `age Nd`,
   `COOLDOWN`. A new package that you expect is accepted with `--approve-new` (it is then added to
   `.depguard-approved.txt`). The same diff is visible with `git diff` on `requirements*.txt`.
4. Run the tests:
   `scripts/test.sh`
5. Deploy: `scripts/deploy.sh`.
   It first runs `depguard check` (locks match the `.in` files) and `depguard audit`, and refuses to
   build if either fails. `--gate-only` runs just that gate and exits. `--dry-run` runs the gate, then only prints the steps.
   depguard is optional: where it is not installed (`$DEPGUARD_HOME`, default `~/src/depguard`) the script prints a
   warning that the gate was skipped and carries on, because the hash-locked requirements still apply. Set
   `PENGUMON_REQUIRE_DEPGUARD=1` to make a missing depguard a failure, as on the maintainer's host.

A security fix younger than 7 days: lock with
`$DG lock <dir> --cooldown-days 0 --reason "GHSA-xxxx fixed in 1.2.3, released yesterday"`.
The reason stays in the lock header and in git history.

Base image: `$DG base-image .` compares the
`FROM ...@sha256:` pins of both Dockerfiles with Docker Hub. To bump, replace the digest in
`Dockerfile` and `Dockerfile.test` (and the date comment), rebuild, run the tests, run `base-image`
again; details in the depguard README, "Base image digest check". Image OS packages:
`$DG os-scan localhost/pengumon:latest`; if it blocks, rebuild so
the `apt-get upgrade` layer picks up the fix.

## Ignoring an advisory (with expiry)

Create `.depguard-ignore.yaml` in the project root:

```yaml
ignores:
  - id: GHSA-h5c8-rqwp-cp95        # advisory id or any alias (CVE-..., PYSEC-...)
    package: jinja2
    reason: "template sandbox is never used; fix blocked by cooldown"
    expires: 2026-11-30            # YYYY-MM-DD
```

All four keys are required. The id may be any alias of the advisory; the package must match; an
expired entry is a problem. `MAL-` ids are never ignorable. Any problem in the file (missing reason,
expired, syntax, `MAL-`) makes the audit exit 1, so a broken file cannot silently pass. Pick a short
expiry and remove the entry once the dependency is updated.

## Weekly audit

`$DG weekly` runs, for every project listed in depguard's `weekly.json` (for example this one and your other apps): `check`,
`audit`, `base-image`, and `os-scan` on the image the live service runs. It alerts on any blocking finding,
a stale lock, a tool error, an ignore expiring within 14 days, a lock older than 30 days, and
fixable OS packages (a base-image update alone is shown, not alerted).

Results, git-ignored, in depguard's `status/` directory: `latest.txt` (summary), `latest.json`,
`latest-alert.txt`, `history.log` (one line per alerting run). An alert posts to ntfy when
`DEPGUARD_NTFY_URL` is set in depguard's environment file.

A systemd user timer (depguard ships an installer for one; for example Sundays 09:00) can run it; with linger enabled it
runs without a login.

## Manually tracked: Chart.js

Chart.js 4.4.2 is vendored in `src/pengumon/web/static/vendor/chart.umd.min.js` (sha256
`08dfa4730571b23810c34fc39c5101461ecafca56c3f92caf4850509cb158f30`). It is not covered by the Python
lock or audit, nor by GitHub's dependency graph. Check its releases and advisories by hand; the update procedure is in
`src/pengumon/web/static/vendor/README.md` (verify against the npm registry, update the hash in
`tests/test_vendor_assets.py`).

## Accepted risks

- The CSP keeps `'unsafe-inline'` in `script-src`: `index.html` has 48 inline event handlers (43
  `onclick`, 2 `onchange`, 1 `oninput`, 2 `onsubmit`) and `static/app.js` builds one more
  (`onclick="event.stopPropagation(); inspectIncident(...)"`). Removing it means replacing them with
  `addEventListener` code; that is a separate refactor.
- `connect-src 'self' ws: wss:` allows websockets to any host; tighten later.

## Not covered

- Trust on first lock: hashes prove the same files are installed later, not that the first upload was
  benign. The 7-day cooldown and `MAL-` advisories reduce, not remove, that risk.
- No signature or attestation verification of wheels.
- Debian packages come from apt (signed repositories) and are not hash-pinned; the `apt-get upgrade`
  layer makes image builds not bit-reproducible.
- The Trivy vulnerability database is downloaded unpinned (rebuilt daily); only the Trivy image is pinned.
- `pip` inside the image is whatever the pinned base image ships.
- The lock files and the dependency graph leave out the OS layer (`os-scan` covers it) and Chart.js (tracked by hand).
- depguard's own tools are hash-locked, but the depguard repo itself is trusted.
- The weekly audit runs only while the Linux host is up (a missed run starts at the next boot: `Persistent=true`).
- An advisory published after a lock is caught only by the weekly audit or the next deploy.

## Published images

Tagged releases are built by `.github/workflows/release.yml` and pushed to `ghcr.io/pengupowered/pengumon` for amd64
and arm64, after the tests pass. The workflows use `GITHUB_TOKEN` only (no other secrets), start from
`permissions: contents: read` and grant `packages: write` and `contents: write` to the one job each that needs them.
Third-party actions are pinned to full commit SHAs, with the version in a comment; update them by looking the new tag
up (`git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>`) and replacing the SHA. The wheel and sdist
are built once per CI run (`package` job); the image of each architecture is built from that wheel on a native runner
(no QEMU) and joined into one multi-architecture image, and the same wheel and sdist are attached to the GitHub release.
The image is built from the same hash-locked requirements as a local build; the arm64 wheels of the compiled packages
are in the lock.
The image holds only `config.example.yaml`; no private configuration is copied into it.
