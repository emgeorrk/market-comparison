from __future__ import annotations

import csv
import json
import math
import unittest
from collections import Counter
from pathlib import Path

from scripts.build_data import parse_housing


ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "monthly_prices.csv"
METADATA_PATH = ROOT / "data" / "metadata.json"
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"
NUMERIC_COLUMNS = [
    "moscow_secondary_rub_m2",
    "spb_secondary_rub_m2",
    "moscow_primary_rub_m2",
    "spb_primary_rub_m2",
    "usd_rub",
    "eur_rub",
    "sp500_close",
    "imoex_close",
    "nasdaq100_close",
    "russell2000_close",
]
SERIES = {
    "moscow_secondary_rub_m2": "russian",
    "spb_secondary_rub_m2": "russian",
    "moscow_primary_rub_m2": "russian",
    "spb_primary_rub_m2": "russian",
    "usd_rub": "fx",
    "eur_rub": "fx",
    "sp500_close": "us",
    "imoex_close": "russian",
    "nasdaq100_close": "us",
    "russell2000_close": "us",
}


def expected_months() -> list[str]:
    return [f"{year:04d}-{month:02d}" for year in range(2000, 2026) for month in range(1, 13)]


class MonthlyDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with CSV_PATH.open(encoding="utf-8", newline="") as source:
            cls.rows = list(csv.DictReader(source))
        cls.by_month = {row["month"]: row for row in cls.rows}
        cls.metadata = json.loads(METADATA_PATH.read_text(encoding="utf-8"))

    def test_months_are_complete_unique_and_ordered(self) -> None:
        months = [row["month"] for row in self.rows]
        self.assertEqual(months, expected_months())
        self.assertEqual(len(months), 312)
        self.assertEqual(len(set(months)), 312)

    def test_all_ten_series_are_positive_numbers(self) -> None:
        for row in self.rows:
            for column in NUMERIC_COLUMNS:
                value = float(row[column])
                self.assertTrue(math.isfinite(value), f"{row['month']} {column}")
                self.assertGreater(value, 0, f"{row['month']} {column}")

    def test_housing_observation_kinds(self) -> None:
        counts = Counter(row["housing_observation_kind"] for row in self.rows)
        self.assertEqual(counts, {"reported": 104, "interpolated": 206, "backfilled": 2})
        self.assertEqual(self.by_month["2000-01"]["housing_observation_kind"], "backfilled")
        self.assertEqual(self.by_month["2000-03"]["housing_observation_kind"], "reported")
        self.assertEqual(self.by_month["2000-04"]["housing_observation_kind"], "interpolated")

    def test_representative_housing_values(self) -> None:
        self.assertAlmostEqual(float(self.by_month["2000-03"]["moscow_secondary_rub_m2"]), 15034.81)
        self.assertAlmostEqual(float(self.by_month["2000-03"]["spb_secondary_rub_m2"]), 9659.76)
        self.assertAlmostEqual(float(self.by_month["2000-03"]["moscow_primary_rub_m2"]), 16023.80)
        self.assertAlmostEqual(float(self.by_month["2000-03"]["spb_primary_rub_m2"]), 10477.87)
        self.assertAlmostEqual(float(self.by_month["2025-12"]["moscow_secondary_rub_m2"]), 371970.43)
        self.assertAlmostEqual(float(self.by_month["2025-12"]["spb_secondary_rub_m2"]), 276184.86)
        self.assertAlmostEqual(float(self.by_month["2025-12"]["moscow_primary_rub_m2"]), 433182.38)
        self.assertAlmostEqual(float(self.by_month["2025-12"]["spb_primary_rub_m2"]), 293609.01)
        self.assertEqual(self.metadata["housing"]["raw_observations"], 416)
        self.assertEqual(self.metadata["housing"]["markets"], ["primary", "secondary"])
        self.assertNotIn("market", self.metadata["housing"])

    def test_representative_housing_values_match_raw_emiss(self) -> None:
        raw = (ROOT / "data" / "raw" / "fedstat_housing_31452.xml").read_bytes()
        quarterly, count = parse_housing(raw)
        self.assertEqual(count, 416)
        self.assertEqual(
            {name: len(values) for name, values in quarterly.items()},
            {
                "moscow_primary": 104,
                "moscow_secondary": 104,
                "spb_primary": 104,
                "spb_secondary": 104,
            },
        )
        self.assertAlmostEqual(quarterly["moscow_secondary"]["2000-03"], 15034.81)
        self.assertAlmostEqual(quarterly["spb_secondary"]["2000-03"], 9659.76)
        self.assertAlmostEqual(quarterly["moscow_primary"]["2000-03"], 16023.80)
        self.assertAlmostEqual(quarterly["spb_primary"]["2000-03"], 10477.87)
        self.assertAlmostEqual(quarterly["moscow_secondary"]["2025-12"], 371970.43)
        self.assertAlmostEqual(quarterly["spb_secondary"]["2025-12"], 276184.86)
        self.assertAlmostEqual(quarterly["moscow_primary"]["2025-12"], 433182.38)
        self.assertAlmostEqual(quarterly["spb_primary"]["2025-12"], 293609.01)

    def test_selected_january_is_100_after_conversion(self) -> None:
        for year, currency in [(2000, "RUB"), (2014, "USD"), (2025, "RUB")]:
            row = self.by_month[f"{year}-01"]
            usd_rub = float(row["usd_rub"])
            for column, family in SERIES.items():
                native = float(row[column])
                if family == "russian" and currency == "USD":
                    converted = native / usd_rub
                elif family == "us" and currency == "RUB":
                    converted = native * usd_rub
                else:
                    converted = native
                normalized = 100 * converted / converted
                self.assertAlmostEqual(normalized, 100, places=12, msg=f"{year} {currency} {column}")

    def test_dashboard_contains_embedded_complete_data(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn("__MONTHLY_DATA__", html)
        self.assertNotIn("__DATA_METADATA__", html)
        self.assertIn('window.MONTHLY_DATA = [{"month":"2000-01"', html)
        self.assertIn('"month":"2025-12"', html)
        self.assertIn('"moscow_primary_rub_m2":16023.8', html)
        self.assertIn('"spb_primary_rub_m2":293609.01', html)
        self.assertIn("d3@7.9.0", html)

    def test_dashboard_includes_visible_primary_housing_series(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('label: "Москва, новостройки", field: "moscow_primary_rub_m2"', html)
        self.assertIn('label: "Санкт-Петербург, новостройки", field: "spb_primary_rub_m2"', html)
        self.assertEqual(html.count('assetType: "housing"'), 4)
        self.assertEqual(html.count('dash: "10 3"'), 2)
        self.assertIn("visible: new Set(series.map(item => item.id))", html)
        self.assertIn("Линейный график десяти активов", html)

    def test_market_indices_have_distinct_colors_from_housing(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('--series-7: light-dark(#c45100, #ff9d5c);', html)
        self.assertIn('--series-8: light-dark(#4f5d75, #b8c5d6);', html)
        self.assertIn('id: "sp500", label: "S&P 500", field: "sp500_close", family: "us", color: "var(--series-5)"', html)
        self.assertIn('id: "nasdaq100", label: "Nasdaq-100", field: "nasdaq100_close", family: "us", color: "var(--series-7)"', html)
        self.assertIn('id: "russell2000", label: "Russell 2000", field: "russell2000_close", family: "us", color: "var(--series-8)"', html)

    def test_hover_tooltip_targets_only_the_nearest_series(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("const nearest = d3.least(candidates", html)
        self.assertIn('markerById.forEach(marker => marker.attr("opacity", 0));', html)
        self.assertNotIn("const tooltipRows = visibleDisplay.map", html)

    def test_chart_has_no_native_svg_title_tooltip(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn('svg.append("title")', html)
        self.assertIn('aria-labelledby="svg-description"', html)

    def test_requested_interface_copy(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("<title>Сравнение рынков и активов</title>", html)
        self.assertIn("<h1>Сравнение рынков и активов</h1>", html)
        self.assertIn("Сравнение динамики недвижимости, валют и фондовых индексов в рублях и долларах.", html)
        self.assertNotIn("max-width: 860px", html)
        self.assertNotIn('id="chart-context"', html)
        self.assertNotIn("Январь ${state.startYear} = 100 · результат", html)
        self.assertNotIn("Стартовый год: ${state.startYear}", html)
        self.assertNotIn('state.visible.has(definition.id) ? "показан" : "скрыт"', html)
        self.assertIn('.attr("text-anchor", "middle").text("Индекс")', html)
        self.assertNotIn('unit: "пунктов"', html)
        self.assertIn('<a href="https://finance.yahoo.com/">Yahoo Finance</a>', html)

    def test_period_has_end_year_and_drag_to_zoom(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('<select id="end-year"></select>', html)
        self.assertIn('<button id="period-back" class="period-back-button" type="button" disabled hidden>', html)
        self.assertIn("endYear: 2025", html)
        self.assertIn("periodHistory: []", html)
        self.assertIn("row.month >= baseMonth && row.month <= endMonth", html)
        self.assertIn('data-chart-range-selection', html)
        self.assertIn('overlay.on("pointerdown", beginRangeSelection)', html)
        self.assertIn('.on("pointerup", event => finishRangeSelection(event, false))', html)
        self.assertIn("state.periodHistory.push({ startYear: state.startYear, endYear: state.endYear })", html)
        self.assertIn("periodBackButton.hidden = state.periodHistory.length === 0", html)
        self.assertIn('periodBackButton.addEventListener("click"', html)
        self.assertIn("const yearCount = Math.min(xTickCount, settings.endYear - settings.startYear + 1)", html)
        self.assertIn("xAxis.tickValues(tickYears.map", html)
        self.assertNotIn('class", "period-navigator"', html)
        self.assertNotIn('data-period-handle', html)

    def test_dashboard_can_export_and_share_visible_series_as_png(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('id="share-button"', html)
        self.assertIn('id="share-menu" class="share-menu" role="menu"', html)
        self.assertIn("Скопировать изображение", html)
        self.assertIn("Отправить…", html)
        self.assertIn("Сохранить PNG", html)
        self.assertIn("function renderStaticChart(targetSvg, options)", html)
        self.assertIn("function buildShareSvg(snapshot)", html)
        self.assertIn("const width = 1600", html)
        self.assertIn("const height = 1000", html)
        self.assertIn("январь ${snapshot.startYear} = 100 п.", html)
        self.assertIn(
            "const visibleDefinitions = series.filter(definition => snapshot.visible.has(definition.id))",
            html,
        )
        self.assertIn("visibleDisplay = allDisplay.filter(item => visibleIds.has(item.definition.id))", html)
        self.assertIn('new ClipboardItem({ "image/png": preparedShare.blob })', html)
        self.assertIn('navigator.canShare({ files: [file] })', html)
        self.assertIn("link.download = preparedShare.filename", html)
        self.assertIn('shareMenuStatus.textContent = "Готовим изображение…"', html)

    def test_regular_housing_tooltips_omit_observation_labels(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn('reported: "отчётное значение"', html)
        self.assertNotIn('interpolated: "линейная интерполяция"', html)
        self.assertIn('if (definition.assetType !== "housing") return "";', html)
        self.assertNotIn('definition.id !== "moscow"', html)
        self.assertIn('return " · заполнение значением I квартала"', html)

    def test_metadata_describes_every_series(self) -> None:
        self.assertEqual(set(self.metadata["series"]), set(NUMERIC_COLUMNS))
        for column in NUMERIC_COLUMNS:
            item = self.metadata["series"][column]
            self.assertTrue(item["source"])
            self.assertTrue(item["unit"])
            self.assertTrue(item["monthly_aggregation"])
            self.assertEqual(set(item["currency_transformation"]), {"RUB", "USD"})


if __name__ == "__main__":
    unittest.main()
