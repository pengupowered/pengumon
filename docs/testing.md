# 🧪 Running Tests

```bash
pytest tests/ -v
```

Or in a container with nothing installed locally (Podman, `Dockerfile.test`): `scripts/test.sh` (extra arguments go to
pytest).

On GitHub, `.github/workflows/ci.yml` runs both `scripts/test.sh` and `scripts/test-ui.sh` on every push to `main` and
every pull request, and `release.yml` runs it again before a release image is published (`docs/releasing.md`).

## The container image

`scripts/test-container.sh` tests the image the way it is run: read-only root filesystem, uid 10001, a data volume, a
config mounted in, on a private network. It builds the image from source (what `scripts/deploy.sh` does), or with
`--prebuilt` from the wheel in `dist/` (what CI and releases do: `podman build --target wheel-built -o type=local,dest=dist .`
makes one, and the `WHEEL_STAGE=wheel-prebuilt` build argument puts it in the image), or tests an existing image
(`--image NAME`). It checks:

- the process runs as uid 10001; a write to `/app` or `/usr/local` fails, one to `/app/data` and `/tmp` works, and
  `podman diff` shows nothing written anywhere else;
- `/api/health` reports the version in `src/pengumon/__init__.py`; `pengumon version` and `pengumon paths` show it and
  `/app/config.yaml`, `/app/data`, `/app/data/pengumon.db` and `0.0.0.0`;
- an ICMP probe to 127.0.0.1 works from inside (the app's backend, on the raw socket), `pengumon doctor` exits 0, no
  `ping` or `traceroute` program is installed, and the app's own prober reports healthy;
- a versioned static asset (`/static/v/<hash>/...`) is served;
- unless `--no-e2e`: the browser end to end check, `tests/ui/container-e2e.mjs`, run in `localhost/ui-test-runner:1` on the
  same network: the dashboard loads without page or console errors, the live indicator and the path chain go live, and a
  file under its versioned URL loads. It is small on purpose; the full suite below runs against a seeded app, not the image.
  Chromium aborts requests with `net::ERR_NETWORK_CHANGED` when its network namespace changes, so the script waits until
  the runner's network has been quiet for 1.5 s and the app answers before it starts Chromium, and retries once (with a
  notice) on exactly that error. Waits in the script are on conditions with timeouts, not fixed sleeps. With
  `GITHUB_ACTIONS=true` every failed check also prints a `::error title=<check>::` annotation (the job log is not readable
  without a login, annotations are).

It removes the container, network, volume and (if it built one) the image. It needs unprivileged ping for your user, as
the deployment does (`net.ipv4.ping_group_range`).

## CI jobs (`.github/workflows/ci.yml`)

`unit` and `ui` run the two scripts above. `runner-image` builds the browser runner image once per change of its
files and saves it in the Actions cache; `ui` and `container` restore it (only that job saves, so two jobs never race
for one cache key). `package` builds the wheel and sdist once, in the Dockerfile's build stage (the hash-locked build
backend, `pip wheel --no-deps --no-build-isolation`), lists their contents in the job summary, checks the wheel has the
dashboard files and no tests or config, and uploads them as the artifact `dist`. `container` (amd64 on
`ubuntu-24.04`, arm64 on `ubuntu-24.04-arm`, no emulation) downloads `dist` and runs
`scripts/test-container.sh --prebuilt` on both; the browser end-to-end check runs on amd64 only.

`native` runs on `ubuntu-24.04`, `ubuntu-24.04-arm`, a macOS runner and `windows-latest`, with no container: the unit
suite from the hash-locked `requirements-dev.txt` (`PENGUMON_REQUIRE_ICMP=1`, so the tests that probe 127.0.0.1 for
real fail instead of skipping when the OS's ICMP backend can't open), then the CI wheel in a fresh venv:
`pengumon doctor`, `pengumon run` away from the checkout, `/api/health`, `/api/info`'s probe backend and the dashboard
page. Linux needs the `ping_group_range` sysctl first; macOS and Windows need nothing. Tests that cannot apply to an
OS skip there with the reason (`tests/platform_helpers.py`: permission bits, `time.tzset`, FIFOs, SIGTERM, the
maintainer's bash scripts). `netns-trace` runs `scripts/netns-trace.sh` with `sudo` (see [probing.md](probing.md)).
`internet-trace` traces 1.1.1.1 from Linux, macOS and Windows runners and reports what answered as a notice; it never
fails (hosted runners may drop ICMP; in the runs so far the Linux and Windows runners answered nothing past the host).

Every pytest run writes JUnit XML, and `scripts/junit-annotations.py` turns the failures into error annotations (10
per step, GitHub's limit; three steps in `native`), which the REST API serves without signing in:
`/repos/<owner>/<repo>/check-runs/<job id>/annotations`. A run is meant to end with no warning annotations.
Pushing a branch named `ci/<topic>` runs the whole workflow before a merge.

## Dashboard browser tests

Three opt-in tests drive the dashboard. Run both with `scripts/test-ui.sh` (extra arguments go to pytest). It builds
`Dockerfile.ui`, which adds pengumon's Python test setup to the shared UI test runner image
`localhost/ui-test-runner:1` (Debian's Chromium, Node, and `puppeteer-core`, `eslint` and `globals` from a pinned
lockfile). Build that once with `tests/ui/runner/build.sh`, or point `UI_RUNNER_BASE` at an equivalent image; the
script says so if it is missing. The tests
run in a throwaway container with no network, no capabilities and a read-only root filesystem, started with
hardening flags set inline in `scripts/test-ui.sh` (or through a shared runner script, when `UI_RUNNER_DIR` has one). Nothing is installed on the host. Only the image builds touch the network. The packages are test
tooling and never enter the production image (see `docs/supply-chain.md`).

- `tests/test_ui_smoke.py` starts the app on a free port with a seeded database (the fake server is `tests/ui/serve.py`,
  one function per capability's fake data) and drives it in headless Chromium, once per variant (`full`, `bare`,
  `tworouters`, `unknownline`, `longpath`, and `token`: the app behind an access token, signed in through the sign-in page,
  where only `steps/sign-out.mjs` runs: sign out, the old cookie refused, sign out everywhere with a second browser
  context; and `viewonly`: the token with `PENGUMON_VIEW_WITHOUT_TOKEN=1`, not signed in, where `steps/sign-out.mjs`
  checks the view-only page, the server's refusals, and "Sign in to edit" from the header); any failed step, page error, console error, CSP violation or failed request fails it.
- `tests/test_ui_steps.py` runs the guards on the step files (below); it needs node only.
- `tests/test_ui_lint.py` runs ESLint (`tests/ui/eslint.config.mjs`: `no-undef`, module source type, `Chart` global)
  over the modules and over the browser test itself (`tests/ui/*.mjs`). Module scripts are strict, so an undeclared variable is a runtime error.

Both are skipped unless `PENGUMON_UI_SMOKE=1` is set and the packages exist. Alternative without the container: run `npm ci --ignore-scripts` in a copy of `tests/ui/runner` (its `package.json` and
`package-lock.json`), then set `PENGUMON_UI_SMOKE=1`, `PGBROWSER` (that directory), `PUPPETEER_EXECUTABLE_PATH` (an
installed Chrome) and optionally `PENGUMON_BROWSER_ARGS`, and run `pytest tests/test_ui_smoke.py tests/test_ui_lint.py`.

### How the browser test is laid out (`tests/ui/`)

- `smoke.mjs`: the runner. Launches the browser, finds the step files, checks them, runs the steps of the variant and
  reports every failure (not only the first), then page errors, console errors, CSP violations and failed requests.
- `lib.mjs`: the shared context every step receives (`page`, `base`, `outDir`, `variant`, `bare`, `LINE`, `ROUTER`, and the
  helpers `until`, `text`, `count`, `click`, `waitText`, `waitCount`, `eq`, `ok`, `setTheme`, `shots`, `snap`, `phone`, ...). Defined once.
- `steps/<id>.mjs`: the steps. `core.mjs` holds the page-level steps (load, bus, header, desktop and phone layout, tab bar,
  theme, loading/stale/error/outage states). `<panel-id>.mjs` and `<dialog-id>.mjs` test one panel or dialog
  (`static/panels/<id>`, `static/dialogs/<id>`). A file exports `subject` (`"core"`, `"panel:<id>"`, `"dialog:<id>"`),
  `variants` (default `["full"]`) and `steps`, a list of `[name, async (ctx) => {...}, { variants, fatal }]`; the options are
  per step. `runsLast = true` puts a file after all others.
- The step files are in `tests/ui/steps`, not next to the panels, because everything under `static/` is shipped in the
  image and hashed into the asset version; a test file there would change the cache key and be served. A test
  (`test_frontend.py`) fails if a test-like file appears under `static/` and checks that such paths answer 404.
- Order: `core`, then the panels in page order (`[data-panel]` of the page, which is `panel.json` order), then the dialogs
  alphabetically, then `runsLast` files (`add-target`: its first step adds a real target, a sixth hop row; `path-editor`:
  its first step applies an edited path). `stubFetch({"METHOD /path": {status, body}})` reloads the page with those calls
  answered by the page itself from its first script on (an API another branch adds; the first-run state); `null` clears.
- Each file after the first starts from a reset page (desktop size, system theme, nothing stored, fresh load), so it relies
  only on what its own earlier steps did. Where a step needs something done by a user (the `clients` feature needs the
  router's password stored) it does it itself (`signIn`). A failed step ends its file, not the run; `fatal` ends the run.
- Guards (`guards.mjs`, run before any step and by `test_ui_steps.py`): two step files may not declare the same top-level
  helper, a step file may not redeclare a name `lib.mjs` owns (put shared helpers in `lib.mjs`), the file name must match
  its subject, and a file for a panel or dialog that does not exist fails.
- Adding a panel's browser test: create `steps/<panel-id>.mjs` with `subject`, `variants` and `steps`; nothing else changes.
  Fake data for a new capability goes in its own function in `serve.py`.
- `PENGUMON_SMOKE_SHOTS=1` still writes the phone and states screenshots (`ps-*.png`).
