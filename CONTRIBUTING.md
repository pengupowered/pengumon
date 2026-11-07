# Contributing to Pengumon

Thanks for helping. Bug reports, ideas and code are all welcome. Everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md). A security problem goes through the private process in [SECURITY.md](SECURITY.md),
not an issue.

## Reporting a bug or asking for a feature

Open an [issue](https://github.com/pengupowered/pengumon/issues/new/choose) and pick the form that fits. For a bug, the
most useful details are the version, how Pengumon is installed, what you expected and what happened, and the log lines
around it.

**Keep your own network private.** Pengumon's data names your devices and addresses. Before pasting anything, replace
addresses, names and passwords with made-up ones. For help with an incident, the **Ask AI** pack at the **Anonymized**
level ([docs/ai-pack.md](docs/ai-pack.md)) is a safe summary to attach.

## Contributing code

For anything beyond a small fix, open an issue first, so we can agree on the approach before you spend time on it.

### How a pull request becomes part of Pengumon

The public history is one signed commit per release (`docs/releasing.md`), so a pull request is not merged as it is.
It is reviewed and tested here, then applied into the next release commit, with you credited as a co-author
(`Co-authored-by:`) and in the changelog. The pull request is then closed with a link to that release.

By contributing you agree that your contribution is licensed under the project's [MIT license](LICENSE).

### Setting up

You need Python 3.11 or newer, or only Podman (or Docker) to run everything in containers:

```bash
pip install -e ".[dev]"
PENGUMON_CONFIG=config.yaml PENGUMON_DATA=data python -m pengumon.main   # the dashboard on 127.0.0.1:8000
scripts/test.sh                    # the unit tests, in a container
tests/ui/runner/build.sh           # once: the browser test image
scripts/test-ui.sh                 # browser tests in headless Chromium and Firefox, no network
```

[docs/testing.md](docs/testing.md) explains the test suites, and [docs/architecture.md](docs/architecture.md) where things
live. To support a new modem or router, follow [docs/adding-a-device.md](docs/adding-a-device.md), and open a
"Support for a new device" [issue](https://github.com/pengupowered/pengumon/issues/new/choose) first. Recordings from your
device must be redacted before they go into a test or an issue; the guide says how.

### What a good pull request looks like

- **Tests:** a fix comes with a test that fails without it. Keep tests lean: one meaningful test beats several that check
  the same thing. `scripts/test.sh` passes, and so does `scripts/test-ui.sh` when you changed anything under
  `src/pengumon/web/` or `tests/ui/`. CI runs both on your pull request, on Linux, macOS and Windows.
- **No new dependencies** without agreeing on it in an issue first. Dependencies are hash-locked (`requirements*.txt`, from
  the `.in` files) and checked before every release ([docs/supply-chain.md](docs/supply-chain.md)).
- **Efficient:** Pengumon runs all day on small machines. Nothing new should run every probe round or every
  second unless it has to.
- **Plain words** on screen and in the docs: what a home user understands, no config keys or internal ids. Dark and light
  themes and a phone width of 390 px must both work.
- **Made-up values in tests and examples:** addresses from 192.0.2.0/24, 198.51.100.0/24 or 192.168.88.0/24, names
  like "Example-Mesh" or example-isp.net, never data from a real network.
- **Docs:** update the docs your change affects. Leave the version number and `CHANGELOG.md` to the maintainers.
