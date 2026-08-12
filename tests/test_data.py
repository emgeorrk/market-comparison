from __future__ import annotations

import csv
import json
import math
import re
import unittest
from collections import Counter
from pathlib import Path

from scripts.build_data import (
    BIS_HOUSING_SERIES,
    parse_bis_housing,
    parse_cbr,
    parse_dax_price_archive,
    parse_ecb_hkd_eur,
    parse_housing,
    parse_moex,
    parse_yahoo,
)


ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "monthly_prices.csv"
METADATA_PATH = ROOT / "data" / "metadata.json"
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"
NUMERIC_COLUMNS = [
    "moscow_secondary_rub_m2",
    "spb_secondary_rub_m2",
    "moscow_primary_rub_m2",
    "spb_primary_rub_m2",
    "new_york_housing_index",
    "london_housing_gbp",
    "paris_secondary_eur_m2",
    "vienna_housing_index",
    "hong_kong_housing_index",
    "usd_rub",
    "eur_rub",
    "gbp_rub",
    "jpy_rub",
    "hkd_rub",
    "sp500_close",
    "imoex_close",
    "nasdaq100_close",
    "russell2000_close",
    "dowjones_close",
    "rtsi_close",
    "dax_price_close",
    "nikkei225_close",
]
SELECTABLE_SERIES = {
    "moscow_secondary_rub_m2": ("RUB", False),
    "spb_secondary_rub_m2": ("RUB", False),
    "moscow_primary_rub_m2": ("RUB", False),
    "spb_primary_rub_m2": ("RUB", False),
    "new_york_housing_index": ("USD", False),
    "london_housing_gbp": ("GBP", False),
    "paris_secondary_eur_m2": ("EUR", False),
    "vienna_housing_index": ("EUR", False),
    "hong_kong_housing_index": ("HKD", False),
    "usd_rub": ("USD", True),
    "eur_rub": ("EUR", True),
    "sp500_close": ("USD", False),
    "imoex_close": ("RUB", False),
    "nasdaq100_close": ("USD", False),
    "russell2000_close": ("USD", False),
    "dowjones_close": ("USD", False),
    "rtsi_close": ("USD", False),
    "dax_price_close": ("EUR", False),
    "nikkei225_close": ("JPY", False),
}
RUB_RATE_FIELDS = {
    "RUB": None,
    "USD": "usd_rub",
    "EUR": "eur_rub",
    "GBP": "gbp_rub",
    "JPY": "jpy_rub",
    "HKD": "hkd_rub",
}


def expected_months() -> list[str]:
    return [f"{year:04d}-{month:02d}" for year in range(2000, 2026) for month in range(1, 13)]


def converted_value(
    row: dict[str, str], column: str, native_currency: str, quote: bool, currency: str
) -> float:
    native = float(row[column])
    if quote:
        return native
    rate_field = RUB_RATE_FIELDS[native_currency]
    rub_value = native * (float(row[rate_field]) if rate_field else 1)
    return rub_value if currency == "RUB" else rub_value / float(row["usd_rub"])


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

    def test_all_twenty_two_numeric_columns_are_positive(self) -> None:
        self.assertEqual(list(self.rows[0])[2:], NUMERIC_COLUMNS)
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

    def test_representative_russian_housing_values(self) -> None:
        expected = {
            ("2000-03", "moscow_secondary_rub_m2"): 15034.81,
            ("2000-03", "spb_secondary_rub_m2"): 9659.76,
            ("2000-03", "moscow_primary_rub_m2"): 16023.80,
            ("2000-03", "spb_primary_rub_m2"): 10477.87,
            ("2025-12", "moscow_secondary_rub_m2"): 371970.43,
            ("2025-12", "spb_secondary_rub_m2"): 276184.86,
            ("2025-12", "moscow_primary_rub_m2"): 433182.38,
            ("2025-12", "spb_primary_rub_m2"): 293609.01,
        }
        for (month, column), value in expected.items():
            self.assertAlmostEqual(float(self.by_month[month][column]), value)
        self.assertEqual(self.metadata["housing"]["raw_observations"], 416)
        self.assertEqual(self.metadata["housing"]["markets"], ["primary", "secondary"])

    def test_russian_housing_values_match_raw_emiss(self) -> None:
        raw = (ROOT / "data" / "raw" / "fedstat_housing_31452.xml").read_bytes()
        quarterly, count = parse_housing(raw)
        self.assertEqual(count, 416)
        self.assertEqual({name: len(values) for name, values in quarterly.items()}, {
            "moscow_primary": 104,
            "moscow_secondary": 104,
            "spb_primary": 104,
            "spb_secondary": 104,
        })
        self.assertAlmostEqual(quarterly["moscow_secondary"]["2000-03"], 15034.81)
        self.assertAlmostEqual(quarterly["spb_primary"]["2025-12"], 293609.01)

    def test_exact_bis_series_and_representative_values(self) -> None:
        raw = (ROOT / "data" / "raw" / "bis_detailed_property_prices.zip").read_bytes()
        values, count = parse_bis_housing(raw)
        self.assertEqual(count, 936)
        self.assertEqual(self.metadata["international_housing"]["raw_observations"], 936)
        self.assertEqual(
            self.metadata["international_housing"]["series"],
            {name: item["id"] for name, item in BIS_HOUSING_SERIES.items()},
        )
        self.assertEqual({name: len(series) for name, series in values.items()}, {
            "new_york": 104,
            "london": 312,
            "paris": 104,
            "vienna": 104,
            "hong_kong": 312,
        })
        expected = {
            ("new_york", "2000-03"): 129.33,
            ("new_york", "2025-12"): 451.86,
            ("london", "2000-01"): 140000,
            ("london", "2025-12"): 549000,
            ("paris", "2000-03"): 2740,
            ("paris", "2025-12"): 9600,
            ("vienna", "2000-03"): 100.3,
            ("vienna", "2025-12"): 305.6,
            ("hong_kong", "2000-01"): 97.5,
            ("hong_kong", "2025-12"): 299.6,
        }
        for (name, month), expected_value in expected.items():
            self.assertAlmostEqual(values[name][month], expected_value)

    def test_quarterly_bis_series_are_backfilled_and_linearly_interpolated(self) -> None:
        raw = (ROOT / "data" / "raw" / "bis_detailed_property_prices.zip").read_bytes()
        values, _ = parse_bis_housing(raw)
        columns = {
            "new_york": "new_york_housing_index",
            "paris": "paris_secondary_eur_m2",
            "vienna": "vienna_housing_index",
        }
        for name, column in columns.items():
            q1 = values[name]["2000-03"]
            q2 = values[name]["2000-06"]
            self.assertAlmostEqual(float(self.by_month["2000-01"][column]), q1)
            self.assertAlmostEqual(float(self.by_month["2000-02"][column]), q1)
            self.assertAlmostEqual(
                float(self.by_month["2000-04"][column]), q1 + (q2 - q1) / 3, delta=0.01
            )

    def test_monthly_currency_series_and_hkd_cross_rate(self) -> None:
        raw_dir = ROOT / "data" / "raw"
        gbp = parse_cbr((raw_dir / "cbr_gbp_rub.xml").read_bytes())
        jpy = parse_cbr((raw_dir / "cbr_jpy_rub.xml").read_bytes())
        hkd_eur = parse_ecb_hkd_eur((raw_dir / "ecb_hkd_eur.csv").read_bytes())
        self.assertEqual(len(gbp), 312)
        self.assertEqual(len(jpy), 312)
        self.assertEqual(len(hkd_eur), 312)
        for month in ["2000-01", "2014-06", "2025-12"]:
            row = self.by_month[month]
            self.assertAlmostEqual(float(row["gbp_rub"]), gbp[month], places=6)
            self.assertAlmostEqual(float(row["jpy_rub"]), jpy[month], places=6)
            self.assertAlmostEqual(float(row["hkd_rub"]), float(row["eur_rub"]) / hkd_eur[month], places=6)

    def test_new_market_indices_match_raw_endpoints(self) -> None:
        raw_dir = ROOT / "data" / "raw"
        sources = {
            "rtsi_close": parse_moex((raw_dir / "moex_rtsi.json").read_bytes()),
            "dowjones_close": parse_yahoo((raw_dir / "yahoo_dowjones.json").read_bytes()),
            "nikkei225_close": parse_yahoo((raw_dir / "yahoo_nikkei225.json").read_bytes()),
        }
        for column, values in sources.items():
            self.assertEqual(len(values), 312)
            for month in ["2000-01", "2025-12"]:
                self.assertAlmostEqual(float(self.by_month[month][column]), values[month], places=5)

        dax_yahoo = parse_yahoo((raw_dir / "yahoo_dax_price.json").read_bytes())
        dax_archive = parse_dax_price_archive(
            (raw_dir / "bundesbank_dax_price_wu3140.xlsx").read_bytes()
        )
        self.assertEqual(min(dax_yahoo), "2013-03")
        self.assertEqual(max(dax_archive), "2013-04")
        self.assertAlmostEqual(float(self.by_month["2000-01"]["dax_price_close"]), dax_archive["2000-01"])
        self.assertAlmostEqual(
            float(self.by_month["2025-12"]["dax_price_close"]), dax_yahoo["2025-12"], places=6
        )
        for month in sorted(set(dax_yahoo).intersection(dax_archive)):
            self.assertAlmostEqual(dax_yahoo[month], dax_archive[month], delta=0.1)

    def test_currency_conversion_supports_all_native_currencies(self) -> None:
        row = self.by_month["2014-06"]
        cases = {
            "moscow_secondary_rub_m2": "RUB",
            "sp500_close": "USD",
            "paris_secondary_eur_m2": "EUR",
            "london_housing_gbp": "GBP",
            "nikkei225_close": "JPY",
            "hong_kong_housing_index": "HKD",
        }
        for column, native_currency in cases.items():
            raw = float(row[column])
            rate_field = RUB_RATE_FIELDS[native_currency]
            expected_rub = raw * (float(row[rate_field]) if rate_field else 1)
            self.assertAlmostEqual(converted_value(row, column, native_currency, False, "RUB"), expected_rub)
            self.assertAlmostEqual(
                converted_value(row, column, native_currency, False, "USD"),
                expected_rub / float(row["usd_rub"]),
            )

    def test_every_selectable_series_is_100_in_starting_january(self) -> None:
        self.assertEqual(len(SELECTABLE_SERIES), 19)
        for year in [2000, 2014, 2025]:
            row = self.by_month[f"{year}-01"]
            for currency in ["RUB", "USD"]:
                for column, (native_currency, quote) in SELECTABLE_SERIES.items():
                    converted = converted_value(row, column, native_currency, quote, currency)
                    self.assertAlmostEqual(100 * converted / converted, 100, places=12)

    def test_dashboard_contains_embedded_complete_data(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn("__MONTHLY_DATA__", html)
        self.assertNotIn("__DATA_METADATA__", html)
        self.assertIn('window.MONTHLY_DATA = [{"month":"2000-01"', html)
        self.assertIn('"month":"2025-12"', html)
        self.assertIn('"new_york_housing_index":129.33', html)
        self.assertIn('"nikkei225_close":', html)
        self.assertIn("d3@7.9.0", html)

    def test_dashboard_defines_nineteen_series_in_four_groups(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        definitions = re.findall(r'\{ id: "[^"]+", label: "[^"]+", field: "[^"]+"', html)
        self.assertEqual(len(definitions), 19)
        self.assertEqual(html.count('assetType: "housing"'), 9)
        for label in [
            "Недвижимость — Россия",
            "Недвижимость — мир",
            "Валюты",
            "Фондовые индексы",
        ]:
            self.assertIn(f'label: "{label}"', html)
        self.assertIn('groupElement.className = "legend-group"', html)
        self.assertIn('button.setAttribute("aria-pressed"', html)
        self.assertIn('.legend-button[aria-pressed="false"] { background: transparent;', html)
        self.assertIn('.legend-button[aria-pressed="true"] {', html)
        self.assertIn("color: var(--legend-color);", html)
        self.assertIn('stroke="currentColor"', html)
        self.assertNotIn("text-decoration: line-through", html)

    def test_dashboard_starts_with_exactly_three_primary_series(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('visible: new Set(["moscow", "sp500", "imoex"])', html)
        self.assertIn('if (state.visible.has(definition.id) && state.visible.size === 1)', html)
        self.assertIn('status.textContent = "На графике должен остаться хотя бы один ряд."', html)
        self.assertNotIn("visible: new Set(series.map", html)

    def test_dashboard_layout_is_balanced_and_responsive(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("max-width: 1240px;", html)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", html)
        self.assertIn("align-items: stretch;", html)
        self.assertIn("border-radius: 14px;", html)
        self.assertIn("? { top: 18, right: 56, bottom: 58, left: 56 }", html)
        self.assertIn(": { top: 18, right: 72, bottom: 58, left: 72 }", html)
        self.assertIn("margin: { top: 18, right: 96, bottom: 64, left: 96 }", html)
        self.assertIn("Math.round(chartShell.clientWidth)", html)
        self.assertIn("@media (max-width: 560px)", html)

    def test_dashboard_uses_original_neutral_palette(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        expected = {
            "background": ("#f8f7f3", "#101216"),
            "foreground": ("#202227", "#f0f1f3"),
            "muted-foreground": ("#686c74", "#aeb2ba"),
            "border": ("#c7c9cf", "#4b4f58"),
            "grid": ("#dedfe3", "#343840"),
            "input": ("#ffffff", "#24272d"),
            "popover": ("#ffffff", "#25282f"),
            "accent": ("#eceef2", "#30343c"),
            "primary": ("#202227", "#f0f1f3"),
            "primary-foreground": ("#ffffff", "#17191d"),
        }
        for name, (light, dark) in expected.items():
            self.assertIn(f"--{name}: light-dark({light}, {dark});", html)
        self.assertIn("--surface: light-dark(#ffffff, #181b20);", html)
        self.assertIn("--surface-subtle: light-dark(#f2f1ed, #14171b);", html)

    def test_dashboard_converts_before_normalization_and_rescales_visible_y_domain(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        for currency, field in [("USD", "usd_rub"), ("EUR", "eur_rub"), ("GBP", "gbp_rub"), ("JPY", "jpy_rub"), ("HKD", "hkd_rub")]:
            self.assertIn(f'{currency}: "{field}"', html)
        self.assertIn('if (definition.conversion === "quote") return nativeValue;', html)
        self.assertIn("const base = selectedValue(baseRow, definition, settings.currency);", html)
        self.assertIn("value: 100 * converted / base", html)
        self.assertIn("const visibleValues = visibleDisplay.flatMap", html)

    def test_hover_tooltip_targets_only_the_nearest_series(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("const nearest = d3.least(candidates", html)
        self.assertIn('markerById.forEach(marker => marker.attr("opacity", 0));', html)
        self.assertNotIn("const tooltipRows = visibleDisplay.map", html)
        self.assertIn('return " · линейная интерполяция";', html)
        self.assertIn('return " · квартальное значение";', html)

    def test_chart_has_no_native_svg_title_tooltip(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn('svg.append("title")', html)
        self.assertIn('aria-labelledby="svg-description"', html)
        self.assertIn("Линейный график выбранных активов", html)

    def test_requested_interface_copy_and_sources(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("<title>Сравнение рынков и активов</title>", html)
        self.assertIn("<h1>Сравнение рынков и активов</h1>", html)
        self.assertIn("Сравнение динамики недвижимости, валют и фондовых индексов в рублях и долларах.", html)
        self.assertIn('.attr("text-anchor", "middle").text("Индекс")', html)
        self.assertNotIn('unit: "пунктов"', html)
        self.assertIn('<a href="https://data.bis.org/topics/RPP">BIS</a>', html)
        self.assertIn('<a href="https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.HKD.EUR.SP00.A">ЕЦБ</a>', html)
        self.assertIn('<a href="https://finance.yahoo.com/">Yahoo Finance</a>', html)
        self.assertIn("Bundesbank BBK01.WU3140", html)
        self.assertIn("типы объектов и методики различаются", html)

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

    def test_dashboard_can_export_only_visible_series_as_png(self) -> None:
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

    def test_metadata_describes_all_columns_and_sources(self) -> None:
        self.assertEqual(set(self.metadata["series"]), set(NUMERIC_COLUMNS))
        selectable = 0
        support = 0
        for column in NUMERIC_COLUMNS:
            item = self.metadata["series"][column]
            for key in [
                "source",
                "unit",
                "frequency",
                "output_frequency",
                "monthly_aggregation",
                "group",
                "native_currency",
                "selectable",
            ]:
                self.assertIn(key, item, f"{column} lacks {key}")
            self.assertEqual(item["output_frequency"], "monthly")
            self.assertEqual(set(item["currency_transformation"]), {"RUB", "USD"})
            selectable += bool(item["selectable"])
            support += not bool(item["selectable"])
        self.assertEqual(selectable, 19)
        self.assertEqual(support, 3)
        self.assertEqual(self.metadata["series"]["dax_price_close"]["native_currency"], "EUR")
        self.assertEqual(
            self.metadata["series"]["dax_price_close"]["source_supplement"],
            "dax_price_archive",
        )
        self.assertEqual(self.metadata["series"]["nikkei225_close"]["native_currency"], "JPY")
        self.assertEqual(self.metadata["series"]["hkd_rub"]["group"], "support")
        self.assertEqual(
            self.metadata["series"]["paris_secondary_eur_m2"]["source_series"],
            BIS_HOUSING_SERIES["paris"]["id"],
        )


if __name__ == "__main__":
    unittest.main()
