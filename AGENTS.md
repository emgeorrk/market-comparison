# Repository Guidelines

## Project Structure & Module Organization

- `scripts/build_data.py` downloads, parses, validates, and aligns all source series using only the Python standard library.
- `tests/test_data.py` contains the `unittest` regression suite for processed data and dashboard behavior.
- `data/raw/` stores downloaded XML and JSON snapshots. `data/monthly_prices.csv` and `data/metadata.json` are generated outputs.
- `dashboard/template.html` is the editable D3 dashboard source. `dashboard/index.html` is generated with data embedded; do not edit it directly.
- `README.md` documents the dataset assumptions and user workflow.

## Build, Test, and Development Commands

```sh
python3 scripts/build_data.py
python3 -m unittest discover -s tests -v
python3 -m http.server 8000 --directory dashboard
```

The build command fetches every upstream source, validates that every series is gapless from 2000-01 through its own last published month (ragged ends; at least 2025-12 for each), and atomically replaces raw data, processed files, metadata, and the dashboard. It requires network access. A coverage-regression guard refuses to write outputs shorter than the committed CSV unless `--allow-shrink` is passed. Run the test command after any Python, data, or dashboard change. The optional HTTP server exposes the dashboard at `http://localhost:8000`; D3 still loads from its pinned CDN. The `update-data.yml` GitHub Actions workflow performs the same build + test + commit cycle monthly.

## Coding Style & Naming Conventions

Use Python 3 annotations, four-space indentation, `snake_case` for functions and variables, and `UPPER_SNAKE_CASE` for module constants. Keep parsers small and source-specific, and raise informative errors when validation fails. Follow the existing HTML/CSS/JavaScript style: two-space indentation, semantic HTML, kebab-case CSS classes, and camelCase JavaScript identifiers. No formatter or linter is configured, so keep diffs focused and match nearby code.

## Testing Guidelines

Tests use the standard-library `unittest` framework. Name methods `test_<expected_behavior>` and add representative boundary values when changing parsers or date logic. Preserve assertions that months are ordered, unique and contiguous with the 2000–2025 window as the guaranteed prefix, and that every numeric column is a positive contiguous prefix ending no earlier than 2025-12. Dashboard changes should test stable structural or accessibility behavior rather than incidental formatting. There is no formal coverage threshold.

## Commit & Pull Request Guidelines

Recent commits use short, imperative subjects such as `Add interactive chart date range` and `Refine dashboard labels and tooltip copy`; follow that pattern and keep each commit cohesive. Commit regenerated outputs with the source change that produced them. Pull requests should explain the behavior or data change, list validation commands, link relevant issues or upstream sources, and include before/after screenshots for visible dashboard changes. Never commit credentials; this project needs none.
