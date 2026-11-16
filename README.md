# Partwise price robot

Updates the store prices shown on [partwisepc.com](https://partwisepc.com) once a day.

- `prices.py` reads the stores' affiliate product feeds (Awin), finds the products listed on the site and writes `prices.json`.
- `config.json` lists the stores and maps their categories to the site's categories.
- `products.json` is the site's product list (id, category, search words, typical EU price used as a sanity check).
- `report.json` shows how many products each store feed matched, and any rejected matches.
- The workflow `.github/workflows/prices.yml` runs every day at 04:17 UTC. The Awin feed key is stored as the repository secret `AWIN_KEY` and never appears in any file.

The site reads `prices.json` directly from this repository, so price updates need no website deploy.
