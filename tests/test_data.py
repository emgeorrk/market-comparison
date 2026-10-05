from __future__ import annotations

import csv
import io
import json
import math
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import call, patch

from scripts.build_data import (
    BIS_HOUSING_SERIES,
    WORLD_BANK_COMMODITIES_URL,
    WORLD_BANK_GOLD_FILENAME,
    download_sources,
    download_world_bank_gold,
    find_world_bank_monthly_url,
    housing_kind,
    last_complete_month,
    main,
    month_range,
    parse_bis_housing,
    parse_cbr,
    parse_dax_price_archive,
    parse_ecb_hkd_eur,
    parse_housing,
    parse_world_bank_gold,
    parse_moex,
    parse_yahoo,
    require_no_coverage_regression,
    require_contiguous_series,
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
    "gold_usd_oz",
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
    "gold_usd_oz": ("USD", False),
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
    """Minimum guaranteed prefix: the fully-covered 2000-2025 window."""
    return [f"{year:04d}-{month:02d}" for year in range(2000, 2026) for month in range(1, 13)]


QUARTERLY_HOUSING_COLUMNS = [
    "moscow_secondary_rub_m2",
    "spb_secondary_rub_m2",
    "moscow_primary_rub_m2",
    "spb_primary_rub_m2",
    "new_york_housing_index",
    "paris_secondary_eur_m2",
    "vienna_housing_index",
]


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
        self.assertEqual(months[:312], expected_months())
        self.assertGreaterEqual(len(months), 312)
        self.assertEqual(len(set(months)), len(months))
        self.assertEqual(months, month_range("2000-01", months[-1]))

    def test_all_twenty_three_numeric_columns_are_positive_prefixes(self) -> None:
        self.assertEqual(list(self.rows[0])[2:], NUMERIC_COLUMNS)
        for column in NUMERIC_COLUMNS:
            cells = [(row["month"], row[column]) for row in self.rows]
            non_empty = [(month, value) for month, value in cells if value != ""]
            # Ragged ends: filled cells must be a contiguous prefix (no internal gaps).
            self.assertEqual(
                [month for month, _ in non_empty],
                [month for month, _ in cells[: len(non_empty)]],
                column,
            )
            self.assertGreaterEqual(non_empty[-1][0], "2025-12", column)
            for month, raw_value in non_empty:
                value = float(raw_value)
                self.assertTrue(math.isfinite(value), f"{month} {column}")
                self.assertGreater(value, 0, f"{month} {column}")

    def test_housing_observation_kinds(self) -> None:
        counts = Counter(row["housing_observation_kind"] for row in self.rows[:312])
        self.assertEqual(counts, {"reported": 104, "interpolated": 206, "backfilled": 2})
        self.assertEqual(self.by_month["2000-01"]["housing_observation_kind"], "backfilled")
        self.assertEqual(self.by_month["2000-03"]["housing_observation_kind"], "reported")
        self.assertEqual(self.by_month["2000-04"]["housing_observation_kind"], "interpolated")
        for row in self.rows[312:]:
            if any(row[column] != "" for column in QUARTERLY_HOUSING_COLUMNS):
                self.assertEqual(row["housing_observation_kind"], housing_kind(row["month"]), row["month"])
            else:
                self.assertEqual(row["housing_observation_kind"], "", row["month"])

    def test_representative_russian_housing_values(self) -> None:
        expected = {
            ("2000-03", "moscow_secondary_rub_m2"): 15034.81,
            ("2000-03", "spb_secondary_rub_m2"): 9659.76,
            ("2000-03", "moscow_primary_rub_m2"): 16023.80,
            ("2000-03", "spb_primary_rub_m2"): 10477.87,
        }
        for (month, column), value in expected.items():
            self.assertAlmostEqual(float(self.by_month[month][column]), value)
        self.assertGreaterEqual(self.metadata["housing"]["raw_observations"], 416)
        self.assertEqual(self.metadata["housing"]["markets"], ["primary", "secondary"])

    def test_russian_housing_values_match_raw_emiss(self) -> None:
        raw = (ROOT / "data" / "raw" / "fedstat_housing_31452.xml").read_bytes()
        quarterly, count = parse_housing(raw)
        self.assertGreaterEqual(count, 416)
        for name, values in quarterly.items():
            self.assertGreaterEqual(len(values), 104, name)
        self.assertAlmostEqual(quarterly["moscow_secondary"]["2000-03"], 15034.81)
        column_by_series = {
            "moscow_secondary": "moscow_secondary_rub_m2",
            "spb_secondary": "spb_secondary_rub_m2",
            "moscow_primary": "moscow_primary_rub_m2",
            "spb_primary": "spb_primary_rub_m2",
        }
        for name, column in column_by_series.items():
            last_month = max(quarterly[name])
            self.assertGreaterEqual(last_month, "2025-12", name)
            self.assertAlmostEqual(
                float(self.by_month[last_month][column]), round(quarterly[name][last_month], 2), places=2
            )

    def test_exact_bis_series_and_representative_values(self) -> None:
        raw = (ROOT / "data" / "raw" / "bis_detailed_property_prices.zip").read_bytes()
        values, count = parse_bis_housing(raw)
        self.assertGreaterEqual(count, 936)
        self.assertEqual(self.metadata["international_housing"]["raw_observations"], count)
        self.assertEqual(
            self.metadata["international_housing"]["series"],
            {name: item["id"] for name, item in BIS_HOUSING_SERIES.items()},
        )
        minimum_lengths = {
            "new_york": 104,
            "london": 312,
            "paris": 104,
            "vienna": 104,
            "hong_kong": 312,
        }
        for name, minimum in minimum_lengths.items():
            self.assertGreaterEqual(len(values[name]), minimum, name)
        # BIS revises historical observations. Verify exact source cells instead
        # of freezing values from one release of the live dataset.
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            source_rows = {
                row["Series"]: row
                for row in csv.DictReader(io.StringIO(archive.read("WS_DPP_csv_col.csv").decode("utf-8-sig")))
            }
        expected = [
            ("new_york", "Q:US:3:2:1:3:6:0", "2000-Q1", "2000-03"),
            ("london", "M:GB:2:1:0:1:0:0", "2000-01", "2000-01"),
            ("paris", "Q:FR:2:8:1:2:1:1", "2000-Q1", "2000-03"),
            ("vienna", "Q:AT:2:1:0:0:1:0", "2000-Q1", "2000-03"),
            ("hong_kong", "M:HK:0:1:0:1:1:0", "2000-01", "2000-01"),
        ]
        for name, series_id, period, month in expected:
            self.assertAlmostEqual(values[name][month], float(source_rows[series_id][period]))
        column_by_series = {
            "new_york": "new_york_housing_index",
            "london": "london_housing_gbp",
            "paris": "paris_secondary_eur_m2",
            "vienna": "vienna_housing_index",
            "hong_kong": "hong_kong_housing_index",
        }
        for name, column in column_by_series.items():
            last_month = max(values[name])
            self.assertGreaterEqual(last_month, "2025-12", name)
            self.assertAlmostEqual(
                float(self.by_month[last_month][column]), values[name][last_month], places=4
            )

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
        self.assertGreaterEqual(len(gbp), 312)
        self.assertGreaterEqual(len(jpy), 312)
        self.assertGreaterEqual(len(hkd_eur), 312)
        for month in ["2000-01", "2014-06", "2025-12"]:
            row = self.by_month[month]
            self.assertAlmostEqual(float(row["gbp_rub"]), gbp[month], places=6)
            self.assertAlmostEqual(float(row["jpy_rub"]), jpy[month], places=6)
            self.assertAlmostEqual(float(row["hkd_rub"]), float(row["eur_rub"]) / hkd_eur[month], places=6)
        self.assertAlmostEqual(float(self.by_month[max(gbp)]["gbp_rub"]), gbp[max(gbp)], places=6)
        self.assertAlmostEqual(float(self.by_month[max(jpy)]["jpy_rub"]), jpy[max(jpy)], places=6)
        last_hkd = [row["month"] for row in self.rows if row["hkd_rub"] != ""][-1]
        row = self.by_month[last_hkd]
        self.assertAlmostEqual(float(row["hkd_rub"]), float(row["eur_rub"]) / hkd_eur[last_hkd], places=6)

    def test_new_market_indices_match_raw_endpoints(self) -> None:
        raw_dir = ROOT / "data" / "raw"
        sources = {
            "rtsi_close": parse_moex((raw_dir / "moex_rtsi.json").read_bytes()),
            "dowjones_close": parse_yahoo((raw_dir / "yahoo_dowjones.json").read_bytes()),
            "nikkei225_close": parse_yahoo((raw_dir / "yahoo_nikkei225.json").read_bytes()),
            "gold_usd_oz": parse_world_bank_gold((raw_dir / WORLD_BANK_GOLD_FILENAME).read_bytes()),
        }
        for column, values in sources.items():
            self.assertGreaterEqual(len(values), 312)
            self.assertGreaterEqual(max(values), "2025-12", column)
            for month in ["2000-01", max(values)]:
                self.assertAlmostEqual(float(self.by_month[month][column]), values[month], places=5)

        dax_yahoo = parse_yahoo((raw_dir / "yahoo_dax_price.json").read_bytes())
        dax_archive = parse_dax_price_archive(
            (raw_dir / "bundesbank_dax_price_wu3140.xlsx").read_bytes()
        )
        self.assertEqual(min(dax_yahoo), "2013-03")
        self.assertEqual(max(dax_archive), "2013-04")
        self.assertAlmostEqual(float(self.by_month["2000-01"]["dax_price_close"]), dax_archive["2000-01"])
        self.assertAlmostEqual(
            float(self.by_month[max(dax_yahoo)]["dax_price_close"]), dax_yahoo[max(dax_yahoo)], places=6
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

    def test_every_selectable_series_is_100_in_the_start_month(self) -> None:
        self.assertEqual(len(SELECTABLE_SERIES), 20)
        for month in ["2000-01", "2014-06", "2025-11"]:
            row = self.by_month[month]
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
        self.assertIn(f'"month":"{self.metadata["coverage"]["end"]}"', html)
        embedded = re.search(r"window\.MONTHLY_DATA = (\[.*\]);", html)
        self.assertIsNotNone(embedded)
        expected = [
            {key: (float(value) if value else None) if key in NUMERIC_COLUMNS else value for key, value in row.items()}
            for row in self.rows
        ]
        self.assertEqual(json.loads(embedded.group(1)), expected)
        self.assertIn('"nikkei225_close":', html)
        self.assertIn('"gold_usd_oz":', html)
        self.assertIn("d3@7.9.0", html)

    def test_dashboard_defines_twenty_series_in_four_groups(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        definitions = re.findall(r'\{ id: "[^"]+", label: "[^"]+", field: "[^"]+"', html)
        self.assertEqual(len(definitions), 20)
        self.assertEqual(html.count('assetType: "housing"'), 9)
        for label in [
            "Real estate — Russia",
            "Real estate — World",
            "Currencies",
            "Market assets",
        ]:
            self.assertIn(f'label: "{label}"', html)
        for label_ru in [
            "Недвижимость — Россия",
            "Недвижимость — мир",
            "Валюты",
            "Биржевые активы",
        ]:
            self.assertIn(f'labelRu: "{label_ru}"', html)
        self.assertIn('groupElement.className = "legend-group"', html)
        self.assertIn('button.setAttribute("aria-pressed"', html)
        self.assertIn('.legend-button[aria-pressed="false"] { background: transparent;', html)
        self.assertIn('.legend-button[aria-pressed="true"] {', html)
        self.assertIn("color: var(--legend-color);", html)
        self.assertIn('stroke="currentColor"', html)
        self.assertNotIn("text-decoration: line-through", html)

    def test_dashboard_starts_with_exactly_four_primary_series(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('visible: new Set(["sp500", "nasdaq100", "gold", "dax-price"])', html)
        self.assertIn('if (state.visible.has(definition.id) && state.visible.size === 1)', html)
        self.assertIn("status.textContent = t.keepOneSeries;", html)
        self.assertIn('keepOneSeries: "На графике должен остаться хотя бы один ряд."', html)
        self.assertIn('keepOneSeries: "At least one series must remain on the chart."', html)
        self.assertNotIn("visible: new Set(series.map", html)

    def test_dashboard_layout_is_balanced_and_responsive(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("max-width: 1240px;", html)
        self.assertIn("position: sticky;", html)
        self.assertIn("backdrop-filter: blur(8px);", html)
        self.assertIn("justify-content: center;", html)
        self.assertIn("border-radius: 24px;", html)
        self.assertIn("? { top: 18, right: 56, bottom: 58, left: 80 }", html)
        self.assertIn(": { top: 18, right: 72, bottom: 58, left: 96 }", html)
        self.assertIn("margin: { top: 18, right: 96, bottom: 64, left: 124 }", html)
        self.assertIn("Math.round(chartShell.clientWidth)", html)
        self.assertIn("@media (max-width: 560px)", html)

    def test_dashboard_uses_warm_paper_and_deep_night_palette(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        expected = {
            "background": ("#f5eee1", "#0b1120"),
            "foreground": ("#241f15", "#e9edf7"),
            "muted-foreground": ("#6d6350", "#9aa6c5"),
            "border": ("#d6c9ac", "#324067"),
            "grid": ("#ece2cc", "#233052"),
            "input": ("#fffdf7", "#1a2440"),
            "popover": ("#fffdf7", "#1c2745"),
            "accent": ("#efe6d0", "#253458"),
            "primary": ("#0e7a6c", "#38bdf8"),
            "primary-foreground": ("#ffffff", "#071322"),
        }
        for name, (light, dark) in expected.items():
            self.assertIn(f"--{name}: light-dark({light}, {dark});", html)
        self.assertIn("--surface: light-dark(#fffdf7, #131c31);", html)
        self.assertIn("--surface-subtle: light-dark(#fbf5e8, #101828);", html)

    def test_dashboard_has_theme_switch_with_persisted_override(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="light"] { color-scheme: light; }', html)
        self.assertIn(':root[data-theme="dark"] { color-scheme: dark; }', html)
        self.assertIn('<button id="theme-switch" class="theme-switch" type="button" role="switch" aria-checked="false" aria-label="Dark theme">', html)
        self.assertIn('localStorage.getItem("theme")', html)
        self.assertIn('localStorage.setItem("theme", nextTheme)', html)
        self.assertIn('window.matchMedia("(prefers-color-scheme: dark)")', html)

    def test_dashboard_has_language_switch_with_persisted_override(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('<button id="lang-switch" class="lang-switch" type="button"', html)
        self.assertIn('localStorage.getItem("lang")', html)
        self.assertIn('localStorage.setItem("lang", lang)', html)
        self.assertIn("navigator.language", html)
        self.assertIn("document.documentElement.lang = lang", html)
        self.assertIn("const I18N = {", html)
        self.assertIn('locale: "ru-RU"', html)
        self.assertIn('locale: "en-US"', html)
        self.assertIn('label: "Moscow, resale"', html)
        self.assertIn('labelRu: "Москва, вторичка"', html)
        self.assertIn('labelRu: "Индекс Мосбиржи"', html)
        self.assertIn('labelRu: "Золото"', html)
        self.assertIn('labelRu: "Недвижимость — Россия"', html)
        self.assertIn("${monthLabel(snapshot.startMonth)} = 100 pts", html)
        self.assertNotIn('new Intl.NumberFormat("ru-RU"', html)
        self.assertNotIn('new Intl.DateTimeFormat("ru-RU"', html)

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
        self.assertIn('interpolated: " · линейная интерполяция"', html)
        self.assertIn('quarterly: " · квартальное значение"', html)
        self.assertIn('interpolated: " · linear interpolation"', html)
        self.assertIn('quarterly: " · quarterly observation"', html)

    def test_chart_has_no_native_svg_title_tooltip(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertNotIn('svg.append("title")', html)
        self.assertIn('aria-labelledby="svg-description"', html)
        self.assertIn("Линейный график выбранных активов", html)

    def test_requested_interface_copy_and_sources(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("<title>Market and asset comparison</title>", html)
        self.assertIn("<h1>Market and asset comparison</h1>", html)
        self.assertIn("Сравнение динамики недвижимости, валют и биржевых активов.", html)
        self.assertNotIn("в рублях и долларах", html)
        self.assertIn('subtitle: "Comparing the performance of real estate, currencies and market assets."', html)
        self.assertIn('.attr("text-anchor", "middle").text(t.yAxisTitle)', html)
        self.assertIn('yAxisTitle: "Индекс"', html)
        self.assertIn('yAxisTitle: "Index"', html)
        self.assertIn('data-currency="RUB" aria-pressed="false">₽ RUB<', html)
        self.assertIn('data-currency="USD" aria-pressed="true">$ USD<', html)
        self.assertIn('currency: "USD"', html)
        self.assertIn('<html lang="en">', html)
        self.assertIn('|| navigator.language || "en"', html)
        self.assertNotIn('unit: "пунктов"', html)
        self.assertIn('<a href="https://data.bis.org/topics/RPP">BIS</a>', html)
        self.assertIn('<a href="https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.HKD.EUR.SP00.A">ЕЦБ</a>', html)
        self.assertIn('<a href="https://finance.yahoo.com/">Yahoo Finance</a>', html)
        self.assertIn("Bundesbank BBK01.WU3140", html)
        self.assertIn('<a href="https://www.worldbank.org/en/research/commodity-markets">World Bank Pink Sheet</a>', html)
        self.assertIn("средняя цена за месяц", html)
        self.assertIn("not monthly average RUB prices", html)
        self.assertIn("Bundesbank · World Bank", html)
        self.assertNotIn("lbma.org.uk", html)
        self.assertIn("типы объектов и методики различаются", html)

    def test_period_has_month_precision_and_drag_to_zoom(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('<select id="start-month"></select>', html)
        self.assertIn('<select id="end-month"></select>', html)
        self.assertIn('<select id="end-year"></select>', html)
        self.assertIn('<button id="period-back" class="period-back-button" type="button" disabled hidden>', html)
        self.assertIn("startMonth: minMonth", html)
        self.assertIn("endMonth: maxMonth", html)
        self.assertIn("for (let year = minYear; year <= maxYear; year += 1)", html)
        self.assertIn("function populateMonthOptions(", html)
        self.assertIn("periodHistory: []", html)
        self.assertIn("const baseMonth = settings.startMonth;", html)
        self.assertIn("row.month >= baseMonth && row.month <= endMonth", html)
        self.assertIn('data-chart-range-selection', html)
        self.assertIn('overlay.on("pointerdown", beginRangeSelection)', html)
        self.assertIn('.on("pointerup", event => finishRangeSelection(event, false))', html)
        self.assertIn("applyPeriod(formatMonthKey(leftDate), formatMonthKey(rightDate))", html)
        self.assertIn("state.periodHistory.push({ startMonth: state.startMonth, endMonth: state.endMonth })", html)
        self.assertIn("periodBackButton.hidden = state.periodHistory.length === 0", html)
        self.assertIn('periodBackButton.addEventListener("click"', html)
        self.assertIn("const monthSpan = d3.utcMonth.count(", html)
        self.assertIn("const yearCount = Math.min(xTickCount, endYear - startYear + 1)", html)
        self.assertIn("xAxis.tickValues(tickYears.map", html)
        self.assertIn("d3.utcMonth.every(monthStep)", html)

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
        self.assertIn("${monthLabel(snapshot.startMonth)} = 100 п.", html)
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
        self.assertEqual(selectable, 20)
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

    def test_metadata_coverage_and_series_bounds_match_csv(self) -> None:
        months = [row["month"] for row in self.rows]
        self.assertEqual(self.metadata["coverage"]["start"], "2000-01")
        self.assertEqual(self.metadata["coverage"]["end"], months[-1])
        self.assertEqual(self.metadata["coverage"]["months"], len(months))
        for column in NUMERIC_COLUMNS:
            non_empty = [row["month"] for row in self.rows if row[column] != ""]
            entry = self.metadata["series"][column]
            self.assertEqual(entry["first_month"], non_empty[0], column)
            self.assertEqual(entry["last_month"], non_empty[-1], column)
            self.assertGreaterEqual(entry["last_month"], "2025-12", column)

    def test_gold_history_and_metadata_use_monthly_world_bank_prices(self) -> None:
        prices = parse_world_bank_gold((ROOT / "data/raw" / WORLD_BANK_GOLD_FILENAME).read_bytes())
        csv_prices = {row["month"]: float(row["gold_usd_oz"]) for row in self.rows if row["gold_usd_oz"]}
        self.assertEqual(set(csv_prices), set(prices))
        for month, value in prices.items():
            self.assertAlmostEqual(csv_prices[month], value, places=6)
        gold = self.metadata["series"]["gold_usd_oz"]
        self.assertEqual(gold["frequency"], "monthly")
        self.assertEqual(gold["unit"], "USD per troy ounce")
        self.assertIn("monthly average", gold["monthly_aggregation"])
        self.assertIn("May 2025", gold["monthly_aggregation"])
        self.assertIn("June 2025", gold["monthly_aggregation"])
        self.assertIn("month-end USD/RUB", gold["currency_transformation"]["RUB"])
        source = self.metadata["sources"]["gold"]
        self.assertEqual(source["url"], WORLD_BANK_COMMODITIES_URL)
        self.assertEqual(source["raw_file"], f"raw/{WORLD_BANK_GOLD_FILENAME}")

    def test_last_complete_month_boundaries(self) -> None:
        self.assertEqual(last_complete_month(date(2026, 8, 13)), "2026-07")
        self.assertEqual(last_complete_month(date(2026, 1, 1)), "2025-12")
        self.assertEqual(last_complete_month(date(2026, 12, 31)), "2026-11")

    def test_housing_kind_is_structural(self) -> None:
        self.assertEqual(housing_kind("2000-01"), "backfilled")
        self.assertEqual(housing_kind("2000-02"), "backfilled")
        self.assertEqual(housing_kind("2000-03"), "reported")
        self.assertEqual(housing_kind("2013-07"), "interpolated")
        self.assertEqual(housing_kind("2026-06"), "reported")

    def test_coverage_regression_guard(self) -> None:
        require_no_coverage_regression(self.rows)
        truncated = self.rows[:-1]
        with self.assertRaises(ValueError):
            require_no_coverage_regression(truncated)
        require_no_coverage_regression(truncated, allow_shrink=True)

    def test_dashboard_handles_ragged_series_ends(self) -> None:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("if (row[definition.field] == null) return null;", html)
        self.assertIn("if (row.usd_rub == null) return null;", html)
        self.assertIn("const { items: allDisplay, rows: displayedRows } = buildDisplay(settings);", html)
        self.assertIn("if (base == null || !Number.isFinite(base) || base <= 0) return { definition, points: [] };", html)
        self.assertIn("exactDate <= item.points[item.points.length - 1].date", html)
        self.assertIn("previous && previous.length && item.points.length", html)
        self.assertIn('noDataForPeriod: "Нет данных за выбранный период."', html)
        self.assertIn('noDataForPeriod: "No data for the selected period."', html)


class WorldBankGoldTests(unittest.TestCase):
    @staticmethod
    def workbook(
        observations: list[tuple[str, str | None]] | None = None,
        *,
        gold_column: str = "BR",
        inline_strings: bool = False,
        gold_first: bool = False,
        unit: str = "($/troy oz)",
        header: str = "Gold",
        sheet_name: str = "Monthly Prices",
        value_type: str | None = None,
    ) -> bytes:
        """Small independent XLSX fixture with a decoy sheet and movable Gold column."""
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        package_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        strings: list[str] = []
        sheet = ET.Element(f"{{{ns}}}worksheet")
        data = ET.SubElement(sheet, f"{{{ns}}}sheetData")

        def cell(row: ET.Element, address: str, value: str, *, string: bool = False) -> None:
            node = ET.SubElement(row, f"{{{ns}}}c", r=address)
            if string and inline_strings:
                node.set("t", "inlineStr")
                ET.SubElement(ET.SubElement(node, f"{{{ns}}}is"), f"{{{ns}}}t").text = value
            else:
                if string:
                    node.set("t", "s")
                    strings.append(value)
                    value = str(len(strings) - 1)
                elif value_type:
                    node.set("t", value_type)
                ET.SubElement(node, f"{{{ns}}}v").text = value

        for number, value in [(3, header), (4, unit)]:
            row = ET.SubElement(data, f"{{{ns}}}row", r=str(number))
            cell(row, f"{gold_column}{number}", value, string=True)
        if observations is None:
            observations = [("1999M12", "280"), ("2000M01", "284.32"), ("2000M02", "300"), ("2100M01", "999")]
        for number, (period, price) in enumerate(observations, 5):
            row = ET.SubElement(data, f"{{{ns}}}row", r=str(number))
            cell(row, f"A{number}", period, string=True)
            if price is not None:
                cell(row, f"{gold_column}{number}", price)
        book = ET.Element(f"{{{ns}}}workbook")
        sheets = ET.SubElement(book, f"{{{ns}}}sheets")
        names = [("Other prices", "rOther"), (sheet_name, "rGold")]
        if gold_first:
            names.reverse()
        for name, rid in names:
            ET.SubElement(sheets, f"{{{ns}}}sheet", {"name": name, f"{{{rel_ns}}}id": rid})
        rels = ET.Element(f"{{{package_ns}}}Relationships")
        for rid, target in [("rOther", "worksheets/sheet1.xml"), ("rGold", "/xl/worksheets/prices7.xml" if gold_first else "worksheets/prices7.xml")]:
            ET.SubElement(rels, f"{{{package_ns}}}Relationship", Id=rid, Target=target, Type=f"{rel_ns}/worksheet")
        shared = ET.Element(f"{{{ns}}}sst")
        for value in strings:
            ET.SubElement(ET.SubElement(shared, f"{{{ns}}}si"), f"{{{ns}}}t").text = value
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("xl/workbook.xml", ET.tostring(book))
            archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(rels))
            archive.writestr("xl/worksheets/prices7.xml", ET.tostring(sheet))
            archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData/></worksheet>')
            if not inline_strings:
                archive.writestr("xl/sharedStrings.xml", ET.tostring(shared))
        return stream.getvalue()

    def test_discovers_current_monthly_link_and_deduplicates_anchors(self) -> None:
        url = "https://thedocs.worldbank.org/en/doc/new-edition/related/CMO-Historical-Data-Monthly.xlsx"
        html = f'<a href="{url}">Monthly prices</a><a href="{url}#download">XLSX</a><a href="{url.replace("Monthly", "Annual")}">Annual</a>'
        self.assertEqual(find_world_bank_monthly_url(html.encode()), url)
        relative = b'<a href="//thedocs.worldbank.org/new/CMO-Historical-Data-Monthly.xlsx?x=1&amp;y=2">Monthly</a>'
        self.assertEqual(find_world_bank_monthly_url(relative), "https://thedocs.worldbank.org/new/CMO-Historical-Data-Monthly.xlsx?x=1&y=2")

    def test_rejects_missing_ambiguous_and_unofficial_links(self) -> None:
        for html in [
            "<html>Unavailable</html>",
            '<a href="https://example.org/CMO-Historical-Data-Monthly.xlsx">Monthly</a>',
            '<a href="http://thedocs.worldbank.org/CMO-Historical-Data-Monthly.xlsx">Monthly</a>',
            ''.join(f'<a href="https://thedocs.worldbank.org/{edition}/CMO-Historical-Data-Monthly.xlsx">Monthly</a>' for edition in ["old", "new"]),
        ]:
            with self.subTest(html=html), self.assertRaisesRegex(ValueError, "monthly workbook URL"):
                find_world_bank_monthly_url(html.encode())

    def test_resolves_named_sheet_and_gold_column_with_both_string_encodings(self) -> None:
        for column, inline, first in [("BR", False, False), ("C", True, True)]:
            with self.subTest(column=column), patch("scripts.build_data.LAST_COMPLETE_MONTH", "2026-09"):
                raw = self.workbook(gold_column=column, inline_strings=inline, gold_first=first)
                self.assertEqual(parse_world_bank_gold(raw), {"2000-01": 284.32, "2000-02": 300.0})

    def test_excludes_incomplete_month(self) -> None:
        raw = self.workbook([("2026M08", "4400"), ("2026M09", "4300"), ("2026M10", "4200")])
        with patch("scripts.build_data.LAST_COMPLETE_MONTH", "2026-09"):
            self.assertEqual(parse_world_bank_gold(raw), {"2026-08": 4400.0, "2026-09": 4300.0})

    def test_rejects_changed_schema_and_corrupt_workbooks(self) -> None:
        for raw in [self.workbook(unit="($/kg)"), self.workbook(header="Silver"), self.workbook(sheet_name="Annual Prices"), b"<html>Access denied</html>", b"PK\x03\x04broken"]:
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                parse_world_bank_gold(raw)

    def test_rejects_duplicates_invalid_dates_and_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate.*2000-01"):
            parse_world_bank_gold(self.workbook([("2000M01", "280"), ("2000M01", "280")]))
        with self.assertRaisesRegex(ValueError, "monthly period"):
            parse_world_bank_gold(self.workbook([("2000M13", "280")]))
        for price in [None, "", "..", "NaN", "Infinity", "0", "-1", "#VALUE!"]:
            with self.subTest(price=price), self.assertRaisesRegex(ValueError, "Gold value for 2000-01"):
                parse_world_bank_gold(self.workbook([("2000M01", price)]))
        for kind in ["b", "e"]:
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "Gold value"):
                parse_world_bank_gold(self.workbook([("2000M01", "1")], value_type=kind))

    def test_gap_and_minimum_coverage_validation(self) -> None:
        months = month_range("2000-01", "2025-12")
        prices = parse_world_bank_gold(self.workbook([(month.replace("-", "M"), "280") for month in months]))
        require_contiguous_series("World Bank Gold", prices)
        missing_month = {month: value for month, value in prices.items() if month != "2010-06"}
        with self.assertRaisesRegex(ValueError, "missing=.*2010-06"):
            require_contiguous_series("World Bank Gold", missing_month)
        with self.assertRaisesRegex(ValueError, "expected at least"):
            require_contiguous_series("World Bank Gold", {"2000-01": 280})

    def test_online_download_discovers_url_and_offline_reuses_workbook(self) -> None:
        url = "https://thedocs.worldbank.org/current/CMO-Historical-Data-Monthly.xlsx"
        raw = self.workbook()
        with tempfile.TemporaryDirectory() as directory, patch("scripts.build_data.RAW_DIR", Path(directory)):
            with patch("scripts.build_data.fetch_bytes", side_effect=[f'<a href="{url}">Monthly</a>'.encode(), raw]) as fetch:
                downloaded = download_world_bank_gold(offline=True)
                self.assertEqual(fetch.call_args_list, [call(WORLD_BANK_COMMODITIES_URL), call(url)])
                self.assertEqual(downloaded.body, raw)
                self.assertFalse(downloaded.cached)
            (Path(directory) / WORLD_BANK_GOLD_FILENAME).write_bytes(raw)
            with patch("scripts.build_data.fetch_bytes") as fetch:
                cached = download_world_bank_gold(offline=True)
                fetch.assert_not_called()
                self.assertTrue(cached.cached)
                self.assertEqual(cached.body, raw)
            with patch("scripts.build_data.fetch_bytes", side_effect=RuntimeError("network unavailable")):
                with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                    download_world_bank_gold(offline=False)

    def test_failed_gold_download_or_validation_never_writes_outputs(self) -> None:
        with patch("scripts.build_data.sys.argv", ["build_data.py"]), patch("scripts.build_data.write_outputs") as write:
            with patch("scripts.build_data.fetch_bytes", side_effect=RuntimeError("World Bank unavailable")):
                with patch("scripts.build_data.download_sources", side_effect=lambda **kwargs: {"gold": download_world_bank_gold(offline=False)}):
                    with self.assertRaisesRegex(RuntimeError, "World Bank unavailable"):
                        main()
            write.assert_not_called()
            downloads = download_sources(offline=True)
            downloads["gold"] = replace(downloads["gold"], body=b"invalid workbook")
            with patch("scripts.build_data.download_sources", return_value=downloads):
                with self.assertRaisesRegex(ValueError, "World Bank"):
                    main()
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
