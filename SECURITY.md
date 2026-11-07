# Security policy

Pengumon runs inside home networks and can hold the passwords of a modem and a router, so security reports are
welcome and taken seriously.

## Reporting a vulnerability

Please report it privately, not in a public issue:

1. Open the [Security tab](https://github.com/pengupowered/pengumon/security) of this repository.
2. Choose **Report a vulnerability** and describe what you found: the version (Settings, About this install, or
   `pengumon version`), how it is installed (container, `install.sh`, `install.ps1` or `uv`), the steps to reproduce,
   and what an attacker gains.

Only the maintainers see the report. Leave out your own network's details (addresses, names, passwords): made-up values
show the problem just as well.

You can expect a first answer within a week. Pengumon is maintained in spare time, so a fix may take longer; you will be
told the plan, and credited in the advisory and the changelog unless you prefer not to be.

## Supported versions

Only the latest release gets security fixes. Upgrading is described in [docs/install.md](docs/install.md).

## Scope

In scope: the Pengumon app (its web pages and API, the stored credentials and settings, the probes it sends, the device
drivers), the install scripts, and the published container image and Python package.

Out of scope: a dashboard reachable from outside the home without a password (see [docs/security.md](docs/security.md)
for the token and sign-in), and vulnerabilities in your modem's or router's own firmware, which belong with their
vendor.

How Pengumon protects itself (the sign-in password, the request guard, where secrets are kept, the dependency policy) is in
[docs/security.md](docs/security.md) and [docs/supply-chain.md](docs/supply-chain.md).
