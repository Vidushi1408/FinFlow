# Vendored front-end libraries

Served from our own origin so the Content-Security-Policy needs no third-party script or style host and the
app works offline. Unmodified copies of the official release files from jsDelivr:

| File | Library | Version | License |
|---|---|---|---|
| bootstrap.min.css | Bootstrap | 5.3.3 | MIT |
| bootstrap.bundle.min.js | Bootstrap (with Popper) | 5.3.3 | MIT |
| chart.umd.js | Chart.js | 4.4.7 | MIT |
| fonts/inter-latin.woff2, fonts/inter-latin-ext.woff2 | Inter (variable, 400-700) | as served by Google Fonts (v20) | SIL OFL 1.1 |

To upgrade, download the new release file from `https://cdn.jsdelivr.net/npm/<package>@<version>/dist/...`, replace
it here, update this table, and run the browser tests (`tests/e2e`), which load every page.
