# Vendored front-end libraries

## Chart.js

- Name: Chart.js
- Version: 4.4.2
- File: `chart.umd.min.js`
- Source: https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js
- SHA-256: `08dfa4730571b23810c34fc39c5101461ecafca56c3f92caf4850509cb158f30` (205,488 bytes)
- License: MIT (header preserved in the file; https://www.chartjs.org)
- Vendored: 2026-10-10

### Update procedure

1. Download the new version from the jsDelivr URL (change the version) into an empty temp directory.
2. Verify it against the npm registry: `npm view chart.js@<version> dist.integrity` (or fetch the tarball
   from the registry, check its integrity, and compare `dist/chart.umd.js` content/behaviour). Do not trust
   the CDN copy alone.
3. Compute `sha256sum` of the file, copy it here, and update the hash, size, version and date above.
4. Update `EXPECTED_SHA256` in `tests/test_vendor_assets.py` and re-run the tests.
5. Check in the browser that all charts render.
