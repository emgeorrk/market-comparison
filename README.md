# Asset comparison, 2000 – today

A local, reproducible dashboard for comparing housing markets of six countries, currencies, stock indices and gold. The dataset starts in January 2000 and extends to the last complete calendar month; each series ends at its own last published period (quarterly housing statistics lag behind daily market data), and the guaranteed common window covers at least 2000–2025. 20 selectable indicators and three supporting FX series.

[Open the published dashboard](https://emgeorrk.github.io/market-comparison/)

## Quick start

1. Open `dashboard/index.html` in a browser. Access to the CDN is required to load D3 7.9.0.
2. Pick the start and end years and the result currency (USD by default). January of the start year is automatically taken as 100.
3. To zoom into a period, press and hold the mouse button or your finger on the chart, select a range by dragging right or left, and release. The "Previous period" button walks back through the zoom history.
4. Click indicators in the four groups to add or remove lines. On load only S&P 500, Nasdaq-100, Gold and DAX Price are selected; at least one series always remains on the chart.

Refresh all data:

```sh
python3 scripts/build_data.py
```

Rebuild from the saved snapshots in `data/raw` (only missing sources are downloaded):

```sh
python3 scripts/build_data.py --offline
```

Verify the outputs:

```sh
python3 -m unittest discover -s tests -v
```

The script uses only the Python standard library, stores the original responses in `data/raw`, validates every series before writing (each must be gapless from January 2000 through its own last month), and atomically replaces each finished file. If downloading or validating any source fails, the existing valid outputs remain untouched. Sources hosted in Russia (EMISS, Bank of Russia, MOEX) fall back to the validated cached snapshot when unreachable; a build whose coverage would shrink relative to the committed CSV fails unless `--allow-shrink` is passed.

## Automatic updates

The GitHub Actions workflow [`update-data.yml`](.github/workflows/update-data.yml) rebuilds the dataset on the 5th of every month (and on manual dispatch), runs the test suite, and pushes the regenerated outputs to `main`, which redeploys the GitHub Pages dashboard automatically.

## Data contents

- `data/monthly_prices.csv` — months, the housing observation kind and 23 numeric series in native units: 20 user-facing indicators and three supporting FX rates.
- `data/metadata.json` — sources, retrieval date, units, aggregation, interpolation and currency transformations.
- `data/raw/` — original SDMX XML, Bank of Russia XML and JSON from the market data sources.
- `dashboard/index.html` — a self-contained HTML page with embedded data; D3 is loaded from a pinned CDN version.

Russian housing is the average price of 1 m² of primary and secondary housing across all apartment types from EMISS indicator 31452. BIS Detailed Residential Property Prices provides the exact series for New York, London, Paris, Vienna and Hong Kong; property types, units and methodologies differ between markets and are described in `data/metadata.json`. Quarterly values are anchored to March, June, September and December, the months in between are linearly interpolated, and January–February 2000 are backfilled with the Q1 value. Monthly BIS values are used directly. No series is extrapolated past its last published period, so housing lines end a few months earlier than market data.

USD/RUB, EUR/RUB, GBP/RUB and JPY/RUB are the last official Bank of Russia quote of each month. HKD/RUB is derived from the last daily ECB HKD/EUR rate and EUR/RUB. The equity series include S&P 500, Nasdaq-100, Russell 2000, Dow Jones, MOEX Index, RTS, DAX Price and Nikkei 225; the monthly close of the price index in the exchange's time zone is used. Yahoo history for DAX Price (`^GDAXIP`) starts in March 2013, so the earlier stretch is extended with the archival official Deutsche Bundesbank series `BBK01.WU3140`; the overlap between the sources is verified during the build. Gold is the LBMA Gold Price PM fix in US dollars per troy ounce; the last quote of each month is used. Before normalization, each asset is converted from its native currency into the selected RUB or USD. USD/RUB and EUR/RUB remain standalone direct quotes. Dividends and inflation are not accounted for.
