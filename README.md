# UK fuel prices

Live page: https://andyhewitt35-cmyk.github.io/fuel-prices-uk/

- `fetch_prices.py` fetches the government Fuel Finder CSV (official URL, then the hourly public mirror), plus the old CMA retailer feeds still online, de-duplicates, filters bad prices and writes `site/data.json` (compact, short keys).
- `site/index.html` is the page; it loads `data.json`. Leaflet + markercluster from unpkg; OpenStreetMap tiles.
- `.github/workflows/publish.yml` re-fetches hourly 05:20-21:20 UTC and deploys to GitHub Pages (no data commits; a keep-alive commit every ~30 days).
- Local test: `python3 fetch_prices.py && (cd site && python3 -m http.server 18932)` then `node test.js`.
