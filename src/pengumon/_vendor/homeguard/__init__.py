"""homeguard: request guards shared by the home apps that are reachable on the local network.

This package is vendored: scripts/vendor.py copies it verbatim into each app's src/ directory. Never edit
the copy inside an app; change it here, commit, and re-vendor.

The version constant is deliberately not called __version__: `depguard sbom` takes an app's version from
the first src/*/__init__.py that defines __version__, and "homeguard" sorts before the app packages.
"""

HOMEGUARD_VERSION = "0.1.0"
