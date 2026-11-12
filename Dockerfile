# The image is built from the wheel (the same file that `pip install pengumon` or uv installs), in two stages. The
# runtime image has no source tree, no tests and no build tools.
#
#   scripts/deploy.sh / podman-compose build   builds the wheel from the source tree (stage "wheel-built").
#   CI and releases                            put an already-built wheel in dist/ and build with
#                                              --build-arg WHEEL_STAGE=wheel-prebuilt, so the image holds
#                                              exactly the file that was tested and attached to the release.
#
# python:3.12-slim as of 2026-11-05 (update: a base-image digest check, e.g. depguard base-image)
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG WHEEL_STAGE=wheel-built

# ---- build: the wheel, with the hash-locked build backend; no build isolation, no download at build time ----
FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY requirements-build.txt ./
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r requirements-build.txt
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
# The wheel is what the image installs; the sdist (same backend, PEP 517 hook called directly: no extra tool) is for
# the release.
RUN pip wheel --no-deps --no-build-isolation -w /dist . \
    && python -c "from setuptools import build_meta; build_meta.build_sdist('/dist')"

FROM scratch AS wheel-built
COPY --from=build /dist/ /

# A wheel built elsewhere (CI): any pengumon-*.whl in dist/ of the build context.
FROM scratch AS wheel-prebuilt
COPY dist/pengumon-*.whl /

FROM ${WHEEL_STAGE} AS wheel

# ---- runtime ----
FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app

# Security updates of the base image, then curl (scripts/test-container.sh runs its health checks inside the container
# with it). setcap's package, libcap2-bin, is installed and purged again in the one RUN that uses it (below). No ping or traceroute: the app
# sends ICMP itself (pengumon.engine.icmp_linux), traceroute included. APT_REFRESH (scripts/deploy.sh passes the date)
# makes a local build run the upgrade again at least once a day instead of reusing an old build's cached layer, which
# kept fixed Debian packages out of local images; a CI build has no cache.
ARG APT_REFRESH=
RUN apt-get update && apt-get -y --no-install-recommends upgrade \
    && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Every package is installed from a hash-locked file (see README, "Dependencies"), then the wheel itself.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r requirements.txt
# COPY, not a bind mount: a COPY layer's cache key is the wheel's content, while a RUN --mount=type=bind layer is
# cached on the command text alone, so Podman could reuse a layer holding another tree's wheel.
COPY --from=wheel / /tmp/wheel/
RUN pip install --no-cache-dir --no-deps /tmp/wheel/pengumon-*.whl \
    && rm -rf /tmp/wheel \
    && pip uninstall -y setuptools \
    && python -m compileall -q "$(python -c 'import pengumon, os; print(os.path.dirname(pengumon.__file__))')"

# The image carries only the example config; compose mounts your own config.yaml over it (nothing private is baked in).
COPY config.example.yaml ./config.yaml

# The raw ICMP socket needs CAP_NET_RAW; granted as a file capability on the interpreter so the app can run
# unprivileged. Without it (a runtime that drops NET_RAW) the backend uses a ping socket, which needs the container's
# net.ipv4.ping_group_range to cover gid 0 (Podman's default). No fallback: a failing setcap must break the build.
# libcap2-bin (setcap, getcap) is only needed here: installed, used, verified and purged in this one RUN, so the image
# carries the capability and not the tools.
RUN apt-get update && apt-get install -y --no-install-recommends libcap2-bin \
    && setcap cap_net_raw+ep /usr/local/bin/python3.12 \
    && getcap /usr/local/bin/python3.12 | grep -q cap_net_raw \
    && apt-get purge -y --auto-remove libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# SQLite database and router secrets live here (mount a named volume).
# Runs as an unprivileged user; an existing root-owned volume needs a one-time `chown -R 10001:0`.
RUN useradd --system --uid 10001 --gid 0 --no-create-home app \
    && mkdir -p /app/data && chown -R 10001:0 /app/data && chmod -R g+rwX /app/data
VOLUME ["/app/data"]
USER 10001

EXPOSE 8000

# PYTHONDONTWRITEBYTECODE: the root filesystem is read-only at run time (the bytecode is compiled above).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PENGUMON_DATA=/app/data PENGUMON_CONFIG=/app/config.yaml PENGUMON_HOST=0.0.0.0

CMD ["pengumon", "run"]
