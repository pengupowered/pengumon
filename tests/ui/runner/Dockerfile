# localhost/ui-test-runner:1 - the base of the browser tests (Dockerfile.ui builds FROM it): Debian's Chromium, fonts, the
# node binary, Firefox ESR (one test pass) and puppeteer-core + eslint + globals pinned in package-lock.json (in /opt/pgbrowser). No project code.
# Build it once with tests/ui/runner/build.sh; scripts/test-ui.sh runs the tests on it. Test-only tooling.

# node:22-slim (index digest) as of 2026-10-31. Used for two things: `npm ci` (stage 1) and the node binary (stage 2).
# Debian trixie's own nodejs is 20.19, but puppeteer-core 25 needs node >=22.12, so Node comes from the official image.
FROM node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS npm
WORKDIR /opt/pgbrowser
COPY package.json package-lock.json ./
# Exactly the locked versions (integrity hashes are verified); lifecycle scripts of dependencies never run.
RUN npm ci --ignore-scripts --no-audit --no-fund

# Firefox ESR from Mozilla's release archive, checked against a pinned SHA-256 (the release's own SHA256SUMS file,
# https://archive.mozilla.org/pub/firefox/releases/<version>/SHA256SUMS, entry linux-x86_64/en-US/firefox-<version>.tar.xz).
# Used by one Firefox pass of the browser tests (puppeteer-core over WebDriver BiDi); never part of an app image.
# To update: change both values together, build, and run scripts/test-ui.sh.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS firefox
ARG FIREFOX_VERSION=153.6.0esr
ARG FIREFOX_SHA256=31321cd9e2cef73df678417e810cdd1b5760aa5cc8063c8465669e5869cb8498
COPY fetch-firefox.py /fetch-firefox.py
RUN python /fetch-firefox.py

# python:3.12-slim as of 2026-10-11. The same digest as the app's Dockerfile, so the two share the layers.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# Security updates of OS packages, then the browser. fonts-liberation gives pages real fonts to lay out with.
RUN export DEBIAN_FRONTEND=noninteractive && apt-get update && apt-get -y --no-install-recommends upgrade \
    && apt-get -y --no-install-recommends install chromium fonts-liberation \
       libgtk-3-0t64 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxtst6 libxt6t64 \
    && rm -rf /var/lib/apt/lists/*

# Only the node binary (no npm needed at run time); it is a glibc build and runs on this Debian.
COPY --from=npm /usr/local/bin/node /usr/local/bin/node
COPY --from=npm /opt/pgbrowser /opt/pgbrowser

COPY --from=firefox /opt/firefox /opt/firefox

# Runs read-only with a tmpfs /tmp (scripts/test-ui.sh), so everything that writes (Chromium profile, crash files) goes under /tmp.
# UI_BROWSER_ARGS: extra Chromium arguments for the project's scripts to use. --no-sandbox because the container (no
# capabilities, no new privileges) is the sandbox, and Chromium's own cannot start inside it. MOZ_DISABLE_*_SANDBOX: the
# same for Firefox (its content process cannot chroot here and would never load a page).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin tester
USER 10001
WORKDIR /tmp
ENV MOZ_DISABLE_CONTENT_SANDBOX=1 MOZ_DISABLE_GMP_SANDBOX=1 MOZ_DISABLE_RDD_SANDBOX=1 MOZ_DISABLE_SOCKET_PROCESS_SANDBOX=1
ENV PGBROWSER=/opt/pgbrowser PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium FIREFOX_EXECUTABLE_PATH=/opt/firefox/firefox UI_BROWSER_ARGS="--no-sandbox" \
    HOME=/tmp XDG_CONFIG_HOME=/tmp/.config XDG_CACHE_HOME=/tmp/.cache
CMD ["node", "--version"]
