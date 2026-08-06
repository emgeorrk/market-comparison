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

    def test_all_eight_series_are_positive_numbers(self) -> None:
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
        self.assertAlmostEqual(float(self.by_month["2025-12"]["moscow_secondary_rub_m2"]), 371970.43)
        self.assertAlmostEqual(float(self.by_month["2025-12"]["spb_secondary_rub_m2"]), 276184.86)
        self.assertEqual(self.metadata["housing"]["raw_observations"], 208)

    def test_representative_housing_values_match_raw_emiss(self) -> None:
        raw = (ROOT / "data" / "raw" / "fedstat_housing_31452.xml").read_bytes()
        quarterly, count = parse_housing(raw)
        self.assertEqual(count, 208)
        self.assertAlmostEqual(quarterly["moscow"]["2000-03"], 15034.81)
        self.assertAlmostEqual(quarterly["spb"]["2000-03"], 9659.76)
        self.assertAlmostEqual(quarterly["moscow"]["2025-12"], 371970.43)
        self.assertAlmostEqual(quarterly["spb"]["2025-12"], 276184.86)

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
        self.assertIn("d3@7.9.0", html)

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
        self.assertNotIn('id="chart-context"', html)
        self.assertNotIn("Январь ${state.startYear} = 100 · результат", html)
        self.assertNotIn("Стартовый год: ${state.startYear}", html)
        self.assertNotIn('state.visible.has(definition.id) ? "показан" : "скрыт"', html)
        self.assertIn('.attr("text-anchor", "middle").text("Индекс")', html)
        self.assertNotIn('unit: "пунктов"', html)
        self.assertIn('<a href="https://finance.yahoo.com/">Yahoo Finance</a>', html)

    def test_period_has_end_year_and_pointer_navigator(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('<select id="end-year"></select>', html)
        self.assertIn("endYear: 2025", html)
        self.assertIn("row.month >= baseMonth && row.month <= endMonth", html)
        self.assertIn('data-period-selection', html)
        self.assertIn('data-period-handle', html)
        self.assertIn('beginPeriodGesture(event, "pan")', html)
        self.assertIn('beginPeriodGesture(event, "recenter")', html)
        self.assertIn('svg.on("pointermove.period", movePeriodGesture)', html)

    def test_regular_housing_tooltips_omit_observation_labels(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn('reported: "отчётное значение"', html)
        self.assertNotIn('interpolated: "линейная интерполяция"', html)
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
