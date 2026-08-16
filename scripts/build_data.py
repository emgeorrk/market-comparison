#!/usr/bin/env python3
"""Download, validate, and align asset data from 2000-01 to the last complete month.

The script intentionally uses only the Python standard library. All network
responses are held in memory until every source has been parsed and validated;
only then are the raw snapshots, processed CSV, metadata, and dashboard written
atomically. Every series must be gapless from 2000-01, but each one may end at
its own last published month (ragged ends). With --offline, sources whose raw
snapshots exist in data/raw are read from disk and only the missing ones are
downloaded. --allow-shrink permits a deliberate rebuild whose coverage is
shorter than the committed CSV.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import math
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


START_YEAR = 2000
# Every series must reach at least this month; the committed history satisfies it.
MIN_END_MONTH = "2025-12"


def last_complete_month(today: date | None = None) -> str:
    current = today or datetime.now(timezone.utc).date()
    if current.month == 1:
        return f"{current.year - 1:04d}-12"
    return f"{current.year:04d}-{current.month - 1:02d}"


LAST_COMPLETE_MONTH = last_complete_month()
END_YEAR = int(LAST_COMPLETE_MONTH[:4])
ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
CSV_PATH = ROOT / "data" / "monthly_prices.csv"
METADATA_PATH = ROOT / "data" / "metadata.json"
DASHBOARD_TEMPLATE_PATH = ROOT / "dashboard" / "template.html"
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"

USER_AGENT = "invest-research-dashboard/1.0 (+local research project)"
GENERIC_NS = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/generic"
G = f"{{{GENERIC_NS}}}"

CBR_CODES = {
    "usd": "R01235",
    "eur": "R01239",
    "gbp": "R01035",
    "jpy": "R01820",
}
YAHOO_SYMBOLS = {
    "sp500": "%5EGSPC",
    "nasdaq100": "%5ENDX",
    "russell2000": "%5ERUT",
    "dowjones": "%5EDJI",
    "dax_price": "%5EGDAXIP",
    "nikkei225": "%5EN225",
}
DAX_PRICE_ARCHIVE_URL = (
    "https://static-content.springer.com/esm/"
    "chp%3A10.1007%2F978-3-030-28444-2_1/"
    "MediaObjects/370574_2_En_1_MOESM1_ESM.zip"
)
DAX_PRICE_ARCHIVE_MEMBER = "myData/BBK01.WU3140.xlsx"
LBMA_GOLD_URL = "https://prices.lbma.org.uk/json/gold_pm.json"
MOEX_SYMBOLS = {
    "imoex": "IMOEX",
    "rtsi": "RTSI",
}
BIS_HOUSING_SERIES = {
    "new_york": {"id": "Q:US:3:2:1:3:6:0", "frequency": "quarterly"},
    "london": {"id": "M:GB:2:1:0:1:0:0", "frequency": "monthly"},
    "paris": {"id": "Q:FR:2:8:1:2:1:1", "frequency": "quarterly"},
    "vienna": {"id": "Q:AT:2:1:0:0:1:0", "frequency": "quarterly"},
    "hong_kong": {"id": "M:HK:0:1:0:1:1:0", "frequency": "monthly"},
}

CSV_COLUMNS = [
    "month",
    "housing_observation_kind",
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
NUMERIC_COLUMNS = CSV_COLUMNS[2:]


@dataclass(frozen=True)
class Download:
    name: str
    url: str
    raw_filename: str
    body: bytes
    cached: bool = False


def month_range(start_month: str, end_month: str) -> list[str]:
    year, month = (int(part) for part in start_month.split("-"))
    end_year, end_index = (int(part) for part in end_month.split("-"))
    months: list[str] = []
    while (year, month) <= (end_year, end_index):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return months


def quarter_anchor_months() -> list[str]:
    return [
        month
        for month in month_range(f"{START_YEAR:04d}-03", LAST_COMPLETE_MONTH)
        if int(month[5:7]) % 3 == 0
    ]


def month_end(month_key: str) -> date:
    year, month = (int(part) for part in month_key.split("-"))
    return date(year, month, monthrange(year, month)[1])


def fetch_bytes(url: str, *, data: bytes | None = None, attempts: int = 6, timeout: int = 90) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/xml, text/xml, */*",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    context = ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                body = response.read()
                if not body:
                    raise RuntimeError(f"Empty response from {url}")
                return body
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, RuntimeError) as error:
            last_error = error
            if attempt == attempts:
                break
            time.sleep(min(2 ** (attempt - 1), 12))
    raise RuntimeError(f"Failed to download {url} after {attempts} attempts: {last_error}")


def source_body(url: str, raw_filename: str, *, offline: bool) -> bytes:
    if offline:
        cached_path = RAW_DIR / raw_filename
        if cached_path.exists():
            return cached_path.read_bytes()
    return fetch_bytes(url)


def body_with_cache_fallback(
    url: str,
    raw_filename: str,
    validate,
    *,
    offline: bool,
    data: bytes | None = None,
) -> tuple[bytes, bool]:
    """Fetch and validate a source, falling back to the committed raw snapshot.

    Reserved for sources that may be unreachable from foreign CI runners; under
    ragged ends a frozen series is acceptable and self-healing, while the
    coverage-regression guard proves the fallback never shrinks the data.
    """
    cached_path = RAW_DIR / raw_filename
    if offline and cached_path.exists():
        body = cached_path.read_bytes()
        validate(body)
        return body, True
    try:
        body = fetch_bytes(url, data=data)
        validate(body)
        return body, False
    except (RuntimeError, ValueError, KeyError, ET.ParseError) as error:
        if not cached_path.exists():
            raise
        body = cached_path.read_bytes()
        validate(body)
        print(f"WARNING: {raw_filename}: using cached snapshot after error: {error}", file=sys.stderr)
        return body, True


def fedstat_request_body() -> bytes:
    pairs: list[tuple[str, str]] = [
        # The Russian title is a required literal of the EMISS request protocol.
        ("title", "Средняя цена 1 кв. м общей площади квартир на рынке жилья"),
        ("id", "31452"),
        ("lineObjectIds", "57831"),
        ("lineObjectIds", "58849"),
        ("lineObjectIds", "63148"),
        ("columnObjectIds", "3"),
        ("columnObjectIds", "33560"),
        ("filterObjectIds", "30611"),
        ("selectedFilterIds", "0_31452"),
        ("selectedFilterIds", "57831_1688506"),  # Moscow
        ("selectedFilterIds", "57831_1688519"),  # Saint Petersburg
        ("selectedFilterIds", "58849_1752264"),  # All apartment types
        ("selectedFilterIds", "63148_1855615"),  # Secondary market
        ("selectedFilterIds", "63148_1855614"),  # Primary market
        ("selectedFilterIds", "30611_950351"),   # RUB
        ("selectedFilterIds", "33560_1540222"),  # Q1
        ("selectedFilterIds", "33560_1540224"),  # Q2
        ("selectedFilterIds", "33560_1540226"),  # Q3
        ("selectedFilterIds", "33560_1540227"),  # Q4
    ]
    pairs.extend(("selectedFilterIds", f"3_{year}") for year in range(START_YEAR, END_YEAR + 1))
    return urllib.parse.urlencode(pairs).encode("utf-8")


def download_sources(offline: bool = False) -> dict[str, Download]:
    coverage_end = month_end(LAST_COMPLETE_MONTH)
    period1 = int(datetime(START_YEAR, 1, 1, tzinfo=timezone.utc).timestamp())
    next_year, next_month = (
        (coverage_end.year + 1, 1) if coverage_end.month == 12 else (coverage_end.year, coverage_end.month + 1)
    )
    # First instant of the month after the coverage end: excludes the in-progress bar.
    period2 = int(datetime(next_year, next_month, 1, tzinfo=timezone.utc).timestamp())

    downloads: dict[str, Download] = {}

    fedstat_url = "https://www.fedstat.ru/indicator/data.do?format=sdmx"
    housing_filename = "fedstat_housing_31452.xml"
    housing_body, housing_cached = body_with_cache_fallback(
        fedstat_url,
        housing_filename,
        parse_housing,
        offline=offline,
        data=fedstat_request_body(),
    )
    downloads["housing"] = Download(
        name="EMISS housing indicator 31452",
        url="https://www.fedstat.ru/indicator/31452",
        raw_filename=housing_filename,
        body=housing_body,
        cached=housing_cached,
    )

    bis_housing_url = "https://data.bis.org/static/bulk/WS_DPP_csv_col.zip"
    downloads["bis_housing"] = Download(
        name="BIS detailed residential property prices",
        url="https://data.bis.org/topics/RPP",
        raw_filename="bis_detailed_property_prices.zip",
        body=source_body(bis_housing_url, "bis_detailed_property_prices.zip", offline=offline),
    )

    for key, code in CBR_CODES.items():
        query = urllib.parse.urlencode(
            {
                "date_req1": f"01/01/{START_YEAR}",
                "date_req2": coverage_end.strftime("%d/%m/%Y"),
                "VAL_NM_RQ": code,
            }
        )
        url = f"https://www.cbr.ru/scripts/XML_dynamic.asp?{query}"
        body, cached = body_with_cache_fallback(
            url,
            f"cbr_{key}_rub.xml",
            lambda body, name=key: require_contiguous_series(f"CBR {name.upper()}/RUB", parse_cbr(body)),
            offline=offline,
        )
        downloads[key] = Download(
            name=f"CBR {key.upper()}/RUB",
            url=url,
            raw_filename=f"cbr_{key}_rub.xml",
            body=body,
            cached=cached,
        )

    ecb_hkd_url = (
        "https://data-api.ecb.europa.eu/service/data/EXR/D.HKD.EUR.SP00.A"
        f"?startPeriod={START_YEAR}-01-01&endPeriod={coverage_end.isoformat()}"
        "&format=csvdata&detail=dataonly"
    )
    downloads["hkd_eur"] = Download(
        name="ECB HKD/EUR daily reference rate",
        url=ecb_hkd_url,
        raw_filename="ecb_hkd_eur.csv",
        body=source_body(ecb_hkd_url, "ecb_hkd_eur.csv", offline=offline),
    )

    dax_archive_path = RAW_DIR / "bundesbank_dax_price_wu3140.xlsx"
    if offline and dax_archive_path.exists():
        dax_archive_body = dax_archive_path.read_bytes()
    else:
        dax_archive_bundle = fetch_bytes(DAX_PRICE_ARCHIVE_URL)
        with zipfile.ZipFile(io.BytesIO(dax_archive_bundle)) as archive:
            try:
                dax_archive_body = archive.read(DAX_PRICE_ARCHIVE_MEMBER)
            except KeyError as error:
                raise ValueError(
                    f"DAX archive does not contain {DAX_PRICE_ARCHIVE_MEMBER}"
                ) from error
    downloads["dax_price_archive"] = Download(
        name="Archived Deutsche Bundesbank DAX price index BBK01.WU3140",
        url=f"{DAX_PRICE_ARCHIVE_URL}#{DAX_PRICE_ARCHIVE_MEMBER}",
        raw_filename="bundesbank_dax_price_wu3140.xlsx",
        body=dax_archive_body,
    )

    for key, symbol in MOEX_SYMBOLS.items():
        moex_url = (
            "https://iss.moex.com/iss/engines/stock/markets/index/securities/"
            f"{symbol}/candles.json?from={START_YEAR}-01-01&till={coverage_end.isoformat()}"
            "&interval=31&iss.meta=off"
        )
        body, cached = body_with_cache_fallback(
            moex_url,
            f"moex_{key}.json",
            lambda body, name=symbol: require_contiguous_series(f"MOEX {name}", parse_moex(body)),
            offline=offline,
        )
        downloads[key] = Download(
            name=f"MOEX {symbol} monthly candles",
            url=moex_url,
            raw_filename=f"moex_{key}.json",
            body=body,
            cached=cached,
        )

    for key, symbol in YAHOO_SYMBOLS.items():
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?period1={period1}&period2={period2}&interval=1mo&events=history"
        )
        downloads[key] = Download(
            name=f"Yahoo Finance {key} monthly history",
            url=url,
            raw_filename=f"yahoo_{key}.json",
            body=source_body(url, f"yahoo_{key}.json", offline=offline),
        )

    downloads["gold"] = Download(
        name="LBMA Gold Price PM fix",
        url="https://www.lbma.org.uk/prices-and-data/precious-metal-prices",
        raw_filename="lbma_gold_pm.json",
        body=source_body(LBMA_GOLD_URL, "lbma_gold_pm.json", offline=offline),
    )

    return downloads


def parse_decimal(value: str) -> float:
    return float(value.replace(" ", "").replace("\xa0", "").replace(",", "."))


def parse_housing(raw: bytes) -> tuple[dict[str, dict[str, float]], int]:
    root = ET.fromstring(raw)
    city_by_okato = {
        "45000000000": "moscow",
        "40000000000": "spb",
    }
    market_by_code = {
        "1": "primary",
        "3": "secondary",
    }
    # Quarter labels exactly as the EMISS SDMX payload emits them (Russian).
    quarter_by_name = {
        "I квартал": 1,
        "II квартал": 2,
        "III квартал": 3,
        "IV квартал": 4,
    }
    quarterly: dict[str, dict[str, float]] = {
        f"{city}_{market}": {}
        for city in city_by_okato.values()
        for market in market_by_code.values()
    }
    observation_count = 0

    for series in root.findall(f".//{G}Series"):
        series_key = {
            element.attrib["concept"]: element.attrib["value"]
            for element in series.findall(f"./{G}SeriesKey/{G}Value")
        }
        attributes = {
            element.attrib["concept"]: element.attrib["value"]
            for element in series.findall(f"./{G}Attributes/{G}Value")
        }
        okato = series_key.get("s_OKATO")
        city = city_by_okato.get(okato or "")
        market = market_by_code.get(series_key.get("s_rynzhel", ""))
        quarter = quarter_by_name.get(attributes.get("PERIOD", ""))
        if city is None or market is None or quarter is None:
            continue
        if series_key.get("S_TIPKVARTIR") != "1":
            continue

        for observation in series.findall(f"./{G}Obs"):
            year_element = observation.find(f"./{G}Time")
            value_element = observation.find(f"./{G}ObsValue")
            if year_element is None or value_element is None:
                continue
            year = int(year_element.text or 0)
            if not START_YEAR <= year <= END_YEAR:
                continue
            anchor_month = quarter * 3
            key = f"{year:04d}-{anchor_month:02d}"
            if key > LAST_COMPLETE_MONTH:
                continue
            quarterly[f"{city}_{market}"][key] = parse_decimal(value_element.attrib["value"])
            observation_count += 1

    expected_anchor_prefix = quarter_anchor_months()
    minimum_anchors = 4 * (int(MIN_END_MONTH[:4]) - START_YEAR + 1)
    total_anchor_count = 0
    for name, values in quarterly.items():
        anchors = sorted(values)
        if anchors != expected_anchor_prefix[: len(anchors)]:
            raise ValueError(
                f"Housing anchors for {name} are not a contiguous quarter-end prefix from {START_YEAR}-03"
            )
        if len(anchors) < minimum_anchors:
            raise ValueError(
                f"Housing source for {name} has {len(anchors)} unique anchors; expected at least {minimum_anchors}"
            )
        total_anchor_count += len(anchors)
    if observation_count != total_anchor_count:
        raise ValueError(
            f"Housing source has {observation_count} observations for {total_anchor_count} unique anchors"
        )
    return quarterly, observation_count


def parse_bis_housing(raw: bytes) -> tuple[dict[str, dict[str, float]], int]:
    expected_ids = {item["id"]: name for name, item in BIS_HOUSING_SERIES.items()}
    selected_rows: dict[str, dict[str, str]] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"BIS housing archive has {len(csv_names)} CSV files; expected 1")
        with archive.open(csv_names[0]) as source:
            reader = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig"))
            for row in reader:
                series_id = row.get("Series", "")
                if series_id in expected_ids:
                    if series_id in selected_rows:
                        raise ValueError(f"Duplicate BIS housing series {series_id}")
                    selected_rows[series_id] = row

    missing_ids = sorted(set(expected_ids).difference(selected_rows))
    if missing_ids:
        raise ValueError(f"BIS housing archive is missing series: {missing_ids}")

    values_by_name: dict[str, dict[str, float]] = {}
    observation_count = 0
    for name, specification in BIS_HOUSING_SERIES.items():
        row = selected_rows[specification["id"]]
        values: dict[str, float] = {}
        if specification["frequency"] == "monthly":
            periods = month_range(f"{START_YEAR:04d}-01", LAST_COMPLETE_MONTH)
            key_for_period = lambda period: period
        else:
            periods = [f"{month[:4]}-Q{int(month[5:7]) // 3}" for month in quarter_anchor_months()]
            key_for_period = lambda period: f"{period[:4]}-{int(period[-1]) * 3:02d}"

        missing_from: str | None = None
        for period in periods:
            raw_value = row.get(period, "")
            if not raw_value or raw_value == "NaN":
                if missing_from is None:
                    missing_from = period
                continue
            if missing_from is not None:
                raise ValueError(
                    f"BIS housing series {name} has an internal gap at {missing_from} before {period}"
                )
            values[key_for_period(period)] = float(raw_value)
            observation_count += 1
        if not values or max(values) < MIN_END_MONTH:
            raise ValueError(f"BIS housing series {name} ends before {MIN_END_MONTH}")
        values_by_name[name] = values
    return values_by_name, observation_count


def housing_kind(month: str) -> str:
    if month < f"{START_YEAR:04d}-03":
        return "backfilled"
    if int(month[5:7]) % 3 == 0:
        return "reported"
    return "interpolated"


def interpolate_housing(quarterly: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    interpolated: dict[str, dict[str, float]] = {}
    for name, anchors in quarterly.items():
        ordered_anchor_months = sorted(anchors)
        # Each series runs to its own last anchor; no extrapolation past it.
        months = month_range(f"{START_YEAR:04d}-01", ordered_anchor_months[-1])
        month_index = {month: index for index, month in enumerate(months)}
        ordered_anchor_indices = [month_index[item] for item in ordered_anchor_months]
        series: dict[str, float] = {}
        for month in months:
            current_index = month_index[month]
            if month in anchors:
                value = anchors[month]
            elif current_index < ordered_anchor_indices[0]:
                value = anchors[ordered_anchor_months[0]]
            else:
                upper_position = bisect.bisect_right(ordered_anchor_indices, current_index)
                lower_index = ordered_anchor_indices[upper_position - 1]
                upper_index = ordered_anchor_indices[upper_position]
                lower_month = ordered_anchor_months[upper_position - 1]
                upper_month = ordered_anchor_months[upper_position]
                weight = (current_index - lower_index) / (upper_index - lower_index)
                value = anchors[lower_month] + weight * (anchors[upper_month] - anchors[lower_month])
            series[month] = value
        interpolated[name] = series
    return interpolated


def parse_cbr(raw: bytes) -> dict[str, float]:
    root = ET.fromstring(raw)
    latest: dict[str, tuple[date, float]] = {}
    for record in root.findall("Record"):
        record_date = datetime.strptime(record.attrib["Date"], "%d.%m.%Y").date()
        if not date(START_YEAR, 1, 1) <= record_date <= month_end(LAST_COMPLETE_MONTH):
            continue
        nominal = parse_decimal(record.findtext("Nominal", "1"))
        value = parse_decimal(record.findtext("Value", "0")) / nominal
        key = record_date.strftime("%Y-%m")
        if key not in latest or record_date > latest[key][0]:
            latest[key] = (record_date, value)
    return {key: item[1] for key, item in latest.items()}


def parse_lbma_gold(raw: bytes) -> dict[str, float]:
    latest: dict[str, tuple[date, float]] = {}
    for record in json.loads(raw):
        record_date = datetime.strptime(record["d"], "%Y-%m-%d").date()
        if not date(START_YEAR, 1, 1) <= record_date <= month_end(LAST_COMPLETE_MONTH):
            continue
        raw_value = record["v"][0]
        if raw_value is None:
            continue
        value = float(raw_value)
        if not math.isfinite(value) or value <= 0:
            continue
        key = record_date.strftime("%Y-%m")
        if key not in latest or record_date > latest[key][0]:
            latest[key] = (record_date, value)
    return {key: item[1] for key, item in latest.items()}


def parse_ecb_hkd_eur(raw: bytes) -> dict[str, float]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    latest: dict[str, tuple[date, float]] = {}
    for row in reader:
        if row.get("KEY") != "EXR.D.HKD.EUR.SP00.A":
            continue
        raw_value = row.get("OBS_VALUE", "")
        if not raw_value:
            continue
        observation_date = datetime.strptime(row["TIME_PERIOD"], "%Y-%m-%d").date()
        if not date(START_YEAR, 1, 1) <= observation_date <= month_end(LAST_COMPLETE_MONTH):
            continue
        key = observation_date.strftime("%Y-%m")
        value = float(raw_value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid ECB HKD/EUR value for {observation_date}: {raw_value}")
        if key not in latest or observation_date > latest[key][0]:
            latest[key] = (observation_date, value)
    return {key: item[1] for key, item in latest.items()}


def parse_moex(raw: bytes) -> dict[str, float]:
    payload = json.loads(raw)
    columns = payload["candles"]["columns"]
    rows = payload["candles"]["data"]
    close_index = columns.index("close")
    begin_index = columns.index("begin")
    values: dict[str, float] = {}
    for row in rows:
        key = str(row[begin_index])[:7]
        if f"{START_YEAR}-01" <= key <= LAST_COMPLETE_MONTH:
            values[key] = float(row[close_index])
    return values


def parse_yahoo(raw: bytes) -> dict[str, float]:
    payload = json.loads(raw)
    result = payload.get("chart", {}).get("result")
    if not result:
        raise ValueError(f"Yahoo response has no result: {payload.get('chart', {}).get('error')}")
    chart = result[0]
    timestamps = chart["timestamp"]
    closes = chart["indicators"]["quote"][0]["close"]
    timezone_name = chart.get("meta", {}).get("exchangeTimezoneName", "UTC")
    try:
        exchange_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown Yahoo exchange timezone: {timezone_name}") from error
    values: dict[str, float] = {}
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        key = datetime.fromtimestamp(timestamp, tz=exchange_timezone).strftime("%Y-%m")
        if f"{START_YEAR}-01" <= key <= LAST_COMPLETE_MONTH:
            values[key] = float(close)
    return values


def parse_dax_price_archive(raw: bytes) -> dict[str, float]:
    """Parse the archived Bundesbank BBK01.WU3140 workbook without openpyxl."""
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    namespace = {"s": spreadsheet_ns}
    with zipfile.ZipFile(io.BytesIO(raw)) as workbook:
        shared_root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
        shared_strings = [
            "".join(node.text or "" for node in item.findall(".//s:t", namespace))
            for item in shared_root.findall("s:si", namespace)
        ]
        sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))

    values: dict[str, float] = {}
    for row in sheet_root.findall(".//s:row", namespace):
        cells = {cell.attrib["r"][0]: cell for cell in row.findall("s:c", namespace)}
        if "A" not in cells or "B" not in cells:
            continue
        date_value = cells["A"].find("s:v", namespace)
        index_value = cells["B"].find("s:v", namespace)
        if date_value is None or index_value is None:
            continue
        date_text = date_value.text or ""
        if cells["A"].attrib.get("t") == "s":
            date_text = shared_strings[int(date_text)]
        if len(date_text) < 7 or not date_text[:4].isdigit():
            continue
        key = date_text[:7]
        if f"{START_YEAR}-01" <= key <= LAST_COMPLETE_MONTH:
            value = float(index_value.text or "nan")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid archived DAX price value for {key}: {value}")
            values[key] = value
    return values


def merge_dax_price_history(archive_values: dict[str, float], yahoo_values: dict[str, float]) -> dict[str, float]:
    """Backfill Yahoo's pre-2013 gap with the archived official price index."""
    overlap = sorted(set(archive_values).intersection(yahoo_values))
    if not overlap:
        raise ValueError("Archived Bundesbank and Yahoo DAX price histories do not overlap")
    for month in overlap:
        if not math.isclose(archive_values[month], yahoo_values[month], rel_tol=0.0, abs_tol=0.1):
            raise ValueError(
                f"DAX price sources disagree for {month}: "
                f"Bundesbank={archive_values[month]}, Yahoo={yahoo_values[month]}"
            )
    return {**archive_values, **yahoo_values}


def require_contiguous_series(name: str, values: dict[str, float]) -> None:
    months = sorted(values)
    if not months:
        raise ValueError(f"{name} has no observations")
    if months[-1] < MIN_END_MONTH:
        raise ValueError(f"{name} ends at {months[-1]}; expected at least {MIN_END_MONTH}")
    expected = month_range(f"{START_YEAR:04d}-01", months[-1])
    if months != expected:
        missing = [month for month in expected if month not in values]
        extras = sorted(set(months).difference(expected))
        raise ValueError(f"Invalid {name} series: missing={missing[:5]}, extras={extras[:5]}")
    invalid = [month for month in months if not math.isfinite(values[month]) or values[month] <= 0]
    if invalid:
        raise ValueError(f"Invalid {name} values: {invalid[:5]}")


def series_end_months(rows: list[dict[str, object]]) -> dict[str, str | None]:
    ends: dict[str, str | None] = {column: None for column in NUMERIC_COLUMNS}
    for row in rows:
        for column in NUMERIC_COLUMNS:
            if row[column] not in (None, ""):
                ends[column] = str(row["month"])
    return ends


def require_no_coverage_regression(rows: list[dict[str, object]], *, allow_shrink: bool = False) -> None:
    """New data must never cover less than the committed CSV (frozen sources may stall, not shrink)."""
    if allow_shrink or not CSV_PATH.exists():
        return
    with CSV_PATH.open(encoding="utf-8", newline="") as source:
        previous_rows = list(csv.DictReader(source))
    if not previous_rows:
        return
    previous_ends = series_end_months(previous_rows)
    new_ends = series_end_months(rows)
    regressions = [
        f"{column}: {previous_ends[column]} -> {new_ends[column]}"
        for column in NUMERIC_COLUMNS
        if previous_ends[column] is not None
        and (new_ends[column] is None or new_ends[column] < previous_ends[column])
    ]
    if str(previous_rows[-1]["month"]) > str(rows[-1]["month"]):
        regressions.append(f"master window: {previous_rows[-1]['month']} -> {rows[-1]['month']}")
    if regressions:
        raise ValueError(
            "Coverage regression against committed CSV (pass --allow-shrink to override): "
            + "; ".join(regressions)
        )


def build_rows(downloads: dict[str, Download]) -> tuple[list[dict[str, object]], int, int]:
    quarterly, housing_count = parse_housing(downloads["housing"].body)
    housing = interpolate_housing(quarterly)

    bis_housing, bis_housing_count = parse_bis_housing(downloads["bis_housing"].body)
    bis_quarterly = {
        name: bis_housing[name]
        for name, item in BIS_HOUSING_SERIES.items()
        if item["frequency"] == "quarterly"
    }
    bis_interpolated = interpolate_housing(bis_quarterly)
    bis_monthly = {
        name: bis_housing[name]
        for name, item in BIS_HOUSING_SERIES.items()
        if item["frequency"] == "monthly"
    }

    usd_rub = parse_cbr(downloads["usd"].body)
    eur_rub = parse_cbr(downloads["eur"].body)
    gbp_rub = parse_cbr(downloads["gbp"].body)
    jpy_rub = parse_cbr(downloads["jpy"].body)
    hkd_eur = parse_ecb_hkd_eur(downloads["hkd_eur"].body)
    hkd_rub = {
        month: eur_rub[month] / hkd_eur[month]
        for month in sorted(set(eur_rub).intersection(hkd_eur))
    }

    imoex = parse_moex(downloads["imoex"].body)
    rtsi = parse_moex(downloads["rtsi"].body)
    sp500 = parse_yahoo(downloads["sp500"].body)
    nasdaq100 = parse_yahoo(downloads["nasdaq100"].body)
    russell2000 = parse_yahoo(downloads["russell2000"].body)
    dowjones = parse_yahoo(downloads["dowjones"].body)
    dax_price_yahoo = parse_yahoo(downloads["dax_price"].body)
    dax_price_archive = parse_dax_price_archive(downloads["dax_price_archive"].body)
    dax_price = merge_dax_price_history(dax_price_archive, dax_price_yahoo)
    nikkei225 = parse_yahoo(downloads["nikkei225"].body)
    gold = parse_lbma_gold(downloads["gold"].body)

    named_series = {
        "Moscow secondary housing": housing["moscow_secondary"],
        "Saint Petersburg secondary housing": housing["spb_secondary"],
        "Moscow primary housing": housing["moscow_primary"],
        "Saint Petersburg primary housing": housing["spb_primary"],
        "New York housing": bis_interpolated["new_york"],
        "London housing": bis_monthly["london"],
        "Paris housing": bis_interpolated["paris"],
        "Vienna housing": bis_interpolated["vienna"],
        "Hong Kong housing": bis_monthly["hong_kong"],
        "USD/RUB": usd_rub,
        "EUR/RUB": eur_rub,
        "GBP/RUB": gbp_rub,
        "JPY/RUB": jpy_rub,
        "HKD/RUB": hkd_rub,
        "S&P 500": sp500,
        "IMOEX": imoex,
        "Nasdaq-100": nasdaq100,
        "Russell 2000": russell2000,
        "Dow Jones": dowjones,
        "RTS": rtsi,
        "DAX Price": dax_price,
        "Nikkei 225": nikkei225,
        "Gold (LBMA PM)": gold,
    }
    for name, values in named_series.items():
        require_contiguous_series(name, values)

    # The observation-kind column only describes months covered by at least one
    # quarterly-anchored housing series; past their common end it stays empty.
    quarterly_housing_end = max(
        max(series) for series in [*housing.values(), *bis_interpolated.values()]
    )
    master_end = max(max(values) for values in named_series.values())

    def cell(values: dict[str, float], month: str, digits: int) -> float | None:
        value = values.get(month)
        return None if value is None else round(value, digits)

    rows: list[dict[str, object]] = []
    for month in month_range(f"{START_YEAR:04d}-01", master_end):
        rows.append(
            {
                "month": month,
                "housing_observation_kind": housing_kind(month) if month <= quarterly_housing_end else "",
                "moscow_secondary_rub_m2": cell(housing["moscow_secondary"], month, 2),
                "spb_secondary_rub_m2": cell(housing["spb_secondary"], month, 2),
                "moscow_primary_rub_m2": cell(housing["moscow_primary"], month, 2),
                "spb_primary_rub_m2": cell(housing["spb_primary"], month, 2),
                "new_york_housing_index": cell(bis_interpolated["new_york"], month, 6),
                "london_housing_gbp": cell(bis_monthly["london"], month, 2),
                "paris_secondary_eur_m2": cell(bis_interpolated["paris"], month, 2),
                "vienna_housing_index": cell(bis_interpolated["vienna"], month, 6),
                "hong_kong_housing_index": cell(bis_monthly["hong_kong"], month, 6),
                "usd_rub": cell(usd_rub, month, 6),
                "eur_rub": cell(eur_rub, month, 6),
                "gbp_rub": cell(gbp_rub, month, 6),
                "jpy_rub": cell(jpy_rub, month, 6),
                "hkd_rub": cell(hkd_rub, month, 6),
                "sp500_close": cell(sp500, month, 6),
                "imoex_close": cell(imoex, month, 6),
                "nasdaq100_close": cell(nasdaq100, month, 6),
                "russell2000_close": cell(russell2000, month, 6),
                "dowjones_close": cell(dowjones, month, 6),
                "rtsi_close": cell(rtsi, month, 6),
                "dax_price_close": cell(dax_price, month, 6),
                "nikkei225_close": cell(nikkei225, month, 6),
                "gold_usd_oz": cell(gold, month, 6),
            }
        )

    if [row["month"] for row in rows] != month_range(f"{START_YEAR:04d}-01", master_end):
        raise ValueError("Monthly rows are not unique and chronologically complete")
    return rows, housing_count, bis_housing_count


def csv_bytes(rows: list[dict[str, object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def build_metadata(
    downloads: dict[str, Download],
    rows: list[dict[str, object]],
    housing_count: int,
    bis_housing_count: int,
) -> dict[str, object]:
    quarterly_method = (
        "quarter-end anchors with linear monthly interpolation; "
        "Jan-Feb 2000 backfilled from Q1; each series ends at its last "
        "published quarter-end anchor (no extrapolation)"
    )

    def currency_transformation(native_currency: str, *, quote: bool = False) -> dict[str, str]:
        if quote:
            return {
                "RUB": f"standard {native_currency}/RUB quote",
                "USD": f"standard {native_currency}/RUB quote",
            }
        if native_currency == "RUB":
            return {"RUB": "native", "USD": "divide by USD/RUB"}
        if native_currency == "USD":
            return {"RUB": "multiply by USD/RUB", "USD": "native"}
        return {
            "RUB": f"multiply by {native_currency}/RUB",
            "USD": f"multiply by {native_currency}/RUB and divide by USD/RUB",
        }

    def item(
        source: str,
        unit: str,
        aggregation: str,
        group: str,
        native_currency: str,
        *,
        frequency: str = "monthly",
        selectable: bool = True,
        quote: bool = False,
        source_series: str | None = None,
        source_supplement: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "source": source,
            "unit": unit,
            "frequency": frequency,
            "output_frequency": "monthly",
            "monthly_aggregation": aggregation,
            "group": group,
            "native_currency": native_currency,
            "selectable": selectable,
            "currency_transformation": currency_transformation(native_currency, quote=quote),
        }
        if source_series is not None:
            result["source_series"] = source_series
        if source_supplement is not None:
            result["source_supplement"] = source_supplement
        return result

    series_metadata = {
        "moscow_secondary_rub_m2": item(
            "housing", "RUB per square meter", quarterly_method, "housing-russia", "RUB",
            frequency="quarterly",
        ),
        "spb_secondary_rub_m2": item(
            "housing", "RUB per square meter", quarterly_method, "housing-russia", "RUB",
            frequency="quarterly",
        ),
        "moscow_primary_rub_m2": item(
            "housing", "RUB per square meter", quarterly_method, "housing-russia", "RUB",
            frequency="quarterly",
        ),
        "spb_primary_rub_m2": item(
            "housing", "RUB per square meter", quarterly_method, "housing-russia", "RUB",
            frequency="quarterly",
        ),
        "new_york_housing_index": item(
            "bis_housing", "index points", quarterly_method, "housing-world", "USD",
            frequency="quarterly",
            source_series=BIS_HOUSING_SERIES["new_york"]["id"],
        ),
        "london_housing_gbp": item(
            "bis_housing", "GBP per dwelling", "monthly observation", "housing-world", "GBP",
            source_series=BIS_HOUSING_SERIES["london"]["id"],
        ),
        "paris_secondary_eur_m2": item(
            "bis_housing", "EUR per square meter", quarterly_method, "housing-world", "EUR",
            frequency="quarterly",
            source_series=BIS_HOUSING_SERIES["paris"]["id"],
        ),
        "vienna_housing_index": item(
            "bis_housing", "index points", quarterly_method, "housing-world", "EUR",
            frequency="quarterly",
            source_series=BIS_HOUSING_SERIES["vienna"]["id"],
        ),
        "hong_kong_housing_index": item(
            "bis_housing", "index points", "monthly observation", "housing-world", "HKD",
            source_series=BIS_HOUSING_SERIES["hong_kong"]["id"],
        ),
        "usd_rub": item(
            "usd", "RUB per USD", "last official rate in month", "fx", "USD",
            frequency="daily", quote=True,
        ),
        "eur_rub": item(
            "eur", "RUB per EUR", "last official rate in month", "fx", "EUR",
            frequency="daily", quote=True,
        ),
        "gbp_rub": item(
            "gbp", "RUB per GBP", "last official rate in month", "support", "GBP",
            frequency="daily", selectable=False, quote=True,
        ),
        "jpy_rub": item(
            "jpy", "RUB per JPY", "last official rate in month", "support", "JPY",
            frequency="daily", selectable=False, quote=True,
        ),
        "hkd_rub": item(
            "hkd_eur", "RUB per HKD", "last ECB HKD/EUR rate in month, crossed through EUR/RUB",
            "support", "HKD", frequency="daily", selectable=False, quote=True,
        ),
        "sp500_close": item("sp500", "price-index points", "monthly close", "indices", "USD"),
        "imoex_close": item("imoex", "price-index points", "monthly close", "indices", "RUB"),
        "nasdaq100_close": item("nasdaq100", "price-index points", "monthly close", "indices", "USD"),
        "russell2000_close": item("russell2000", "price-index points", "monthly close", "indices", "USD"),
        "dowjones_close": item("dowjones", "price-index points", "monthly close", "indices", "USD"),
        "rtsi_close": item("rtsi", "price-index points", "monthly close", "indices", "USD"),
        "dax_price_close": item(
            "dax_price",
            "price-index points",
            "Yahoo monthly close; 2000-01 through 2013-02 backfilled from archived "
            "Deutsche Bundesbank BBK01.WU3140 month-end values",
            "indices",
            "EUR",
            source_series="^GDAXIP",
            source_supplement="dax_price_archive",
        ),
        "nikkei225_close": item("nikkei225", "price-index points", "monthly close", "indices", "JPY"),
        "gold_usd_oz": item(
            "gold", "USD per troy ounce", "last LBMA PM fix in month", "indices", "USD",
            frequency="daily",
        ),
    }
    for column, entry in series_metadata.items():
        months_present = [str(row["month"]) for row in rows if row[column] not in (None, "")]
        entry["first_month"] = months_present[0]
        entry["last_month"] = months_present[-1]
    return {
        "coverage": {
            "start": f"{START_YEAR}-01",
            "end": str(rows[-1]["month"]),
            "months": len(rows),
        },
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "housing": {
            "indicator": 31452,
            "markets": ["primary", "secondary"],
            "apartment_types": "all",
            "raw_frequency": "quarterly; anchored to quarter-end months",
            "monthly_method": (
                "linear interpolation between quarter-end anchors; Jan-Feb 2000 backfilled from Q1; "
                "each series ends at its last published quarter (no extrapolation)"
            ),
            "raw_observations": housing_count,
            "unit": "RUB per square meter",
        },
        "international_housing": {
            "source": "BIS detailed residential property prices",
            "series": {name: item["id"] for name, item in BIS_HOUSING_SERIES.items()},
            "raw_observations": bis_housing_count,
            "monthly_method": (
                "monthly series used directly; quarterly series linearly interpolated between "
                "quarter-end anchors; Jan-Feb 2000 backfilled from Q1; each series ends at its "
                "last published period (no extrapolation)"
            ),
        },
        "monthly_aggregation": {
            "cbr_fx": "last official rate published in each calendar month",
            "ecb_hkd_eur": "last reference rate published in each calendar month",
            "market_indices": "monthly closing value",
        },
        "normalization": {
            "formula": "100 * selected_currency_value / selected_currency_value_in_selected_start_month",
            "local_assets_rub": "native value * local_currency_RUB",
            "local_assets_usd": "native value * local_currency_RUB / USD_RUB",
            "fx_pairs": "USD/RUB and EUR/RUB remain in standard quotes in both display currencies",
            "dividends": "excluded",
            "inflation_adjustment": "excluded",
        },
        "sources": {
            key: {
                "name": item.name,
                "url": item.url,
                "raw_file": f"raw/{item.raw_filename}",
                "retrieval": "validated cached snapshot" if item.cached else "downloaded",
            }
            for key, item in downloads.items()
        },
        "series": series_metadata,
        "columns": {
            "moscow_secondary_rub_m2": "Moscow secondary housing, all apartment types, RUB/m2",
            "spb_secondary_rub_m2": "Saint Petersburg secondary housing, all apartment types, RUB/m2",
            "moscow_primary_rub_m2": "Moscow primary housing, all apartment types, RUB/m2",
            "spb_primary_rub_m2": "Saint Petersburg primary housing, all apartment types, RUB/m2",
            "new_york_housing_index": "New York and New Jersey existing single-family houses, index",
            "london_housing_gbp": "London all dwellings, GBP per dwelling",
            "paris_secondary_eur_m2": "Paris existing flats, EUR/m2",
            "vienna_housing_index": "Vienna all dwellings, index",
            "hong_kong_housing_index": "Hong Kong all dwellings, index",
            "usd_rub": "RUB per USD",
            "eur_rub": "RUB per EUR",
            "gbp_rub": "RUB per GBP",
            "jpy_rub": "RUB per JPY",
            "hkd_rub": "RUB per HKD, derived from ECB HKD/EUR and CBR EUR/RUB",
            "sp500_close": "S&P 500 price index monthly close, points",
            "imoex_close": "MOEX Russia Index monthly close, points",
            "nasdaq100_close": "Nasdaq-100 price index monthly close, points",
            "russell2000_close": "Russell 2000 price index monthly close, points",
            "dowjones_close": "Dow Jones Industrial Average monthly close, points",
            "rtsi_close": "RTS Index monthly close, points",
            "dax_price_close": "DAX price index monthly close, points",
            "nikkei225_close": "Nikkei 225 price index monthly close, points",
            "gold_usd_oz": "LBMA Gold Price PM fix, USD per troy ounce",
        },
    }


def render_dashboard(rows: list[dict[str, object]], metadata: dict[str, object]) -> bytes:
    template = DASHBOARD_TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count("__MONTHLY_DATA__") != 1 or template.count("__DATA_METADATA__") != 1:
        raise ValueError("Dashboard template placeholders are missing or duplicated")
    rendered = template.replace(
        "__MONTHLY_DATA__", json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    ).replace(
        "__DATA_METADATA__", json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    )
    return rendered.encode("utf-8")


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False) as temporary:
        temporary.write(body)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def write_outputs(downloads: dict[str, Download], rows: list[dict[str, object]], metadata: dict[str, object]) -> None:
    outputs: list[tuple[Path, bytes]] = [
        (RAW_DIR / item.raw_filename, item.body) for item in downloads.values()
    ]
    outputs.extend(
        [
            (CSV_PATH, csv_bytes(rows)),
            (METADATA_PATH, json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"),
            (DASHBOARD_PATH, render_dashboard(rows, metadata)),
        ]
    )
    for path, body in outputs:
        atomic_write(path, body)


def main() -> None:
    arguments = sys.argv[1:]
    offline = "--offline" in arguments
    allow_shrink = "--allow-shrink" in arguments
    downloads = download_sources(offline=offline)
    rows, housing_count, bis_housing_count = build_rows(downloads)
    require_no_coverage_regression(rows, allow_shrink=allow_shrink)
    metadata = build_metadata(downloads, rows, housing_count, bis_housing_count)
    write_outputs(downloads, rows, metadata)
    print(
        f"Built {CSV_PATH.relative_to(ROOT)} and {DASHBOARD_PATH.relative_to(ROOT)}: "
        f"{len(rows)} months through {rows[-1]['month']}, 20 selectable series"
    )


if __name__ == "__main__":
    main()
