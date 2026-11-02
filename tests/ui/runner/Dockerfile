# localhost/ui-test-runner:1 - the base of the browser tests (Dockerfile.ui builds FROM it): Debian's Chromium, fonts, the
# node binary and puppeteer-core + eslint + globals pinned in package-lock.json (in /opt/pgbrowser). No project code.
# Build it once with tests/ui/runner/build.sh; scripts/test-ui.sh runs the tests on it. Test-only tooling.

# node:22-slim (index digest) as of 2026-10-31. Used for two things: `npm ci` (stage 1) and the node binary (stage 2).
# Debian trixie's own nodejs is 20.19, but puppeteer-core 25 needs node >=22.12, so Node comes from the official image.
FROM node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS npm
WORKDIR /opt/pgbrowser
COPY package.json package-lock.json ./
# Exactly the locked versions (integrity hashes are verified); lifecycle scripts of dependencies never run.
RUN npm ci --ignore-scripts --no-audit --no-fund

# python:3.12-slim as of 2026-10-11. The same digest as the app's Dockerfile, so the two share the layers.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# Security updates of OS packages, then the browser. fonts-liberation gives pages real fonts to lay out with.
RUN export DEBIAN_FRONTEND=noninteractive && apt-get update && apt-get -y --no-install-recommends upgrade \
    && apt-get -y --no-install-recommends install chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Only the node binary (no npm needed at run time); it is a glibc build and runs on this Debian.
COPY --from=npm /usr/local/bin/node /usr/local/bin/node
COPY --from=npm /opt/pgbrowser /opt/pgbrowser

# Runs read-only with a tmpfs /tmp (scripts/test-ui.sh), so everything that writes (Chromium profile, crash files) goes under /tmp.
# UI_BROWSER_ARGS: extra Chromium arguments for the project's scripts to use. --no-sandbox because the container (no
# capabilities, no new privileges) is the sandbox, and Chromium's own cannot start inside it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin tester
USER 10001
WORKDIR /tmp
ENV PGBROWSER=/opt/pgbrowser PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium UI_BROWSER_ARGS="--no-sandbox" \
    HOME=/tmp XDG_CONFIG_HOME=/tmp/.config XDG_CACHE_HOME=/tmp/.cache
CMD ["node", "--version"]
