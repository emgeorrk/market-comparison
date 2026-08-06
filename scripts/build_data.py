#!/usr/bin/env python3
"""Download, validate, and align asset data for 2000-2025.

The script intentionally uses only the Python standard library. All network
responses are held in memory until every source has been parsed and validated;
only then are the raw snapshots, processed CSV, metadata, and dashboard written
atomically.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
import math
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


START_YEAR = 2000
END_YEAR = 2025
ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
CSV_PATH = ROOT / "data" / "monthly_prices.csv"
METADATA_PATH = ROOT / "data" / "metadata.json"
DASHBOARD_TEMPLATE_PATH = ROOT / "dashboard" / "template.html"
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"

USER_AGENT = "invest-research-dashboard/1.0 (+local research project)"
GENERIC_NS = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/generic"
G = f"{{{GENERIC_NS}}}"

CBR_CODES = {"usd": "R01235", "eur": "R01239"}
YAHOO_SYMBOLS = {
    "sp500": "%5EGSPC",
    "nasdaq100": "%5ENDX",
    "russell2000": "%5ERUT",
}

CSV_COLUMNS = [
    "month",
    "housing_observation_kind",
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
NUMERIC_COLUMNS = CSV_COLUMNS[2:]


@dataclass(frozen=True)
class Download:
    name: str
    url: str
    raw_filename: str
    body: bytes


def month_keys(start_year: int = START_YEAR, end_year: int = END_YEAR) -> list[str]:
    return [f"{year:04d}-{month:02d}" for year in range(start_year, end_year + 1) for month in range(1, 13)]


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


def fedstat_request_body() -> bytes:
    pairs: list[tuple[str, str]] = [
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


def download_sources() -> dict[str, Download]:
    period1 = int(datetime(START_YEAR, 1, 1, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(END_YEAR + 1, 1, 1, tzinfo=timezone.utc).timestamp())

    downloads: dict[str, Download] = {}

    fedstat_url = "https://www.fedstat.ru/indicator/data.do?format=sdmx"
    downloads["housing"] = Download(
        name="EMISS housing indicator 31452",
        url="https://www.fedstat.ru/indicator/31452",
        raw_filename="fedstat_housing_31452.xml",
        body=fetch_bytes(fedstat_url, data=fedstat_request_body()),
    )

    for key, code in CBR_CODES.items():
        query = urllib.parse.urlencode(
            {
                "date_req1": f"01/01/{START_YEAR}",
                "date_req2": f"31/12/{END_YEAR}",
                "VAL_NM_RQ": code,
            }
        )
        url = f"https://www.cbr.ru/scripts/XML_dynamic.asp?{query}"
        downloads[key] = Download(
            name=f"CBR {key.upper()}/RUB",
            url=url,
            raw_filename=f"cbr_{key}_rub.xml",
            body=fetch_bytes(url),
        )

    moex_url = (
        "https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX/candles.json"
        f"?from={START_YEAR}-01-01&till={END_YEAR}-12-31&interval=31&iss.meta=off"
    )
    downloads["imoex"] = Download(
        name="MOEX IMOEX monthly candles",
        url=moex_url,
        raw_filename="moex_imoex.json",
        body=fetch_bytes(moex_url),
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
            body=fetch_bytes(url),
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
            quarterly[f"{city}_{market}"][key] = parse_decimal(value_element.attrib["value"])
            observation_count += 1

    expected_per_series = 4 * (END_YEAR - START_YEAR + 1)
    expected = len(quarterly) * expected_per_series
    if observation_count != expected:
        raise ValueError(f"Housing source has {observation_count} observations; expected {expected}")
    for name, values in quarterly.items():
        if len(values) != expected_per_series:
            raise ValueError(
                f"Housing source for {name} has {len(values)} unique anchors; expected {expected_per_series}"
            )
    return quarterly, observation_count


def interpolate_housing(quarterly: dict[str, dict[str, float]]) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    months = month_keys()
    month_index = {month: index for index, month in enumerate(months)}
    interpolated: dict[str, dict[str, float]] = {name: {} for name in quarterly}
    kind: dict[str, str] = {}

    reference_name, reference_anchors = next(iter(quarterly.items()))
    reference_anchor_months = set(reference_anchors)
    for name, anchors in quarterly.items():
        if set(anchors) != reference_anchor_months:
            raise ValueError(f"Housing anchor months for {name} do not match {reference_name}")

    first_anchor = f"{START_YEAR:04d}-03"
    for month in months:
        if month in reference_anchors:
            kind[month] = "reported"
        elif month < first_anchor:
            kind[month] = "backfilled"
        else:
            kind[month] = "interpolated"

    for name, anchors in quarterly.items():
        ordered_anchor_months = sorted(anchors, key=month_index.__getitem__)
        ordered_anchor_indices = [month_index[item] for item in ordered_anchor_months]
        for month in months:
            current_index = month_index[month]
            if month in anchors:
                value = anchors[month]
            elif current_index < ordered_anchor_indices[0]:
                value = anchors[ordered_anchor_months[0]]
            else:
                upper_position = bisect.bisect_right(ordered_anchor_indices, current_index)
                if upper_position >= len(ordered_anchor_indices):
                    raise ValueError(f"No housing anchor after {month} for {name}")
                lower_index = ordered_anchor_indices[upper_position - 1]
                upper_index = ordered_anchor_indices[upper_position]
                lower_month = ordered_anchor_months[upper_position - 1]
                upper_month = ordered_anchor_months[upper_position]
                weight = (current_index - lower_index) / (upper_index - lower_index)
                value = anchors[lower_month] + weight * (anchors[upper_month] - anchors[lower_month])
            interpolated[name][month] = value

    return interpolated, kind


def parse_cbr(raw: bytes) -> dict[str, float]:
    root = ET.fromstring(raw)
    latest: dict[str, tuple[date, float]] = {}
    for record in root.findall("Record"):
        record_date = datetime.strptime(record.attrib["Date"], "%d.%m.%Y").date()
        if not date(START_YEAR, 1, 1) <= record_date <= date(END_YEAR, 12, 31):
            continue
        nominal = parse_decimal(record.findtext("Nominal", "1"))
        value = parse_decimal(record.findtext("Value", "0")) / nominal
        key = record_date.strftime("%Y-%m")
        if key not in latest or record_date > latest[key][0]:
            latest[key] = (record_date, value)
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
        if f"{START_YEAR}-01" <= key <= f"{END_YEAR}-12":
            values[key] = float(row[close_index])
    return values


def parse_yahoo(raw: bytes) -> dict[str, float]:
    payload = json.loads(raw)
    result = payload.get("chart", {}).get("result")
    if not result:
        raise ValueError(f"Yahoo response has no result: {payload.get('chart', {}).get('error')}")
    timestamps = result[0]["timestamp"]
    closes = result[0]["indicators"]["quote"][0]["close"]
    values: dict[str, float] = {}
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        key = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m")
        if f"{START_YEAR}-01" <= key <= f"{END_YEAR}-12":
            values[key] = float(close)
    return values


def require_complete_series(name: str, values: dict[str, float]) -> None:
    expected_months = month_keys()
    missing = [month for month in expected_months if month not in values]
    extras = sorted(set(values).difference(expected_months))
    invalid = [month for month in expected_months if month in values and (not math.isfinite(values[month]) or values[month] <= 0)]
    if missing or extras or invalid:
        raise ValueError(f"Invalid {name} series: missing={missing[:5]}, extras={extras[:5]}, invalid={invalid[:5]}")
    if len(values) != len(expected_months):
        raise ValueError(f"{name} has {len(values)} months; expected {len(expected_months)}")


def build_rows(downloads: dict[str, Download]) -> tuple[list[dict[str, object]], int]:
    quarterly, housing_count = parse_housing(downloads["housing"].body)
    housing, housing_kind = interpolate_housing(quarterly)
    usd_rub = parse_cbr(downloads["usd"].body)
    eur_rub = parse_cbr(downloads["eur"].body)
    imoex = parse_moex(downloads["imoex"].body)
    sp500 = parse_yahoo(downloads["sp500"].body)
    nasdaq100 = parse_yahoo(downloads["nasdaq100"].body)
    russell2000 = parse_yahoo(downloads["russell2000"].body)

    named_series = {
        "Moscow secondary housing": housing["moscow_secondary"],
        "Saint Petersburg secondary housing": housing["spb_secondary"],
        "Moscow primary housing": housing["moscow_primary"],
        "Saint Petersburg primary housing": housing["spb_primary"],
        "USD/RUB": usd_rub,
        "EUR/RUB": eur_rub,
        "S&P 500": sp500,
        "IMOEX": imoex,
        "Nasdaq-100": nasdaq100,
        "Russell 2000": russell2000,
    }
    for name, values in named_series.items():
        require_complete_series(name, values)

    rows: list[dict[str, object]] = []
    for month in month_keys():
        rows.append(
            {
                "month": month,
                "housing_observation_kind": housing_kind[month],
                "moscow_secondary_rub_m2": round(housing["moscow_secondary"][month], 2),
                "spb_secondary_rub_m2": round(housing["spb_secondary"][month], 2),
                "moscow_primary_rub_m2": round(housing["moscow_primary"][month], 2),
                "spb_primary_rub_m2": round(housing["spb_primary"][month], 2),
                "usd_rub": round(usd_rub[month], 6),
                "eur_rub": round(eur_rub[month], 6),
                "sp500_close": round(sp500[month], 6),
                "imoex_close": round(imoex[month], 6),
                "nasdaq100_close": round(nasdaq100[month], 6),
                "russell2000_close": round(russell2000[month], 6),
            }
        )

    if [row["month"] for row in rows] != month_keys():
        raise ValueError("Monthly rows are not unique and chronologically complete")
    return rows, housing_count


def csv_bytes(rows: list[dict[str, object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def build_metadata(downloads: dict[str, Download], housing_count: int) -> dict[str, object]:
    series_metadata = {
        "moscow_secondary_rub_m2": {
            "source": "housing",
            "unit": "RUB per square meter",
            "monthly_aggregation": "quarter-end anchors with linear monthly interpolation; Jan-Feb 2000 backfilled from Q1",
            "currency_transformation": {"RUB": "native", "USD": "divide by USD/RUB"},
        },
        "spb_secondary_rub_m2": {
            "source": "housing",
            "unit": "RUB per square meter",
            "monthly_aggregation": "quarter-end anchors with linear monthly interpolation; Jan-Feb 2000 backfilled from Q1",
            "currency_transformation": {"RUB": "native", "USD": "divide by USD/RUB"},
        },
        "moscow_primary_rub_m2": {
            "source": "housing",
            "unit": "RUB per square meter",
            "monthly_aggregation": "quarter-end anchors with linear monthly interpolation; Jan-Feb 2000 backfilled from Q1",
            "currency_transformation": {"RUB": "native", "USD": "divide by USD/RUB"},
        },
        "spb_primary_rub_m2": {
            "source": "housing",
            "unit": "RUB per square meter",
            "monthly_aggregation": "quarter-end anchors with linear monthly interpolation; Jan-Feb 2000 backfilled from Q1",
            "currency_transformation": {"RUB": "native", "USD": "divide by USD/RUB"},
        },
        "usd_rub": {
            "source": "usd",
            "unit": "RUB per USD",
            "monthly_aggregation": "last official rate published in the calendar month",
            "currency_transformation": {"RUB": "standard USD/RUB quote", "USD": "standard USD/RUB quote"},
        },
        "eur_rub": {
            "source": "eur",
            "unit": "RUB per EUR",
            "monthly_aggregation": "last official rate published in the calendar month",
            "currency_transformation": {"RUB": "standard EUR/RUB quote", "USD": "standard EUR/RUB quote"},
        },
        "sp500_close": {
            "source": "sp500",
            "unit": "price-index points",
            "monthly_aggregation": "monthly close",
            "currency_transformation": {"RUB": "multiply by USD/RUB", "USD": "native"},
        },
        "imoex_close": {
            "source": "imoex",
            "unit": "price-index points",
            "monthly_aggregation": "monthly close",
            "currency_transformation": {"RUB": "native", "USD": "divide by USD/RUB"},
        },
        "nasdaq100_close": {
            "source": "nasdaq100",
            "unit": "price-index points",
            "monthly_aggregation": "monthly close",
            "currency_transformation": {"RUB": "multiply by USD/RUB", "USD": "native"},
        },
        "russell2000_close": {
            "source": "russell2000",
            "unit": "price-index points",
            "monthly_aggregation": "monthly close",
            "currency_transformation": {"RUB": "multiply by USD/RUB", "USD": "native"},
        },
    }
    return {
        "coverage": {"start": f"{START_YEAR}-01", "end": f"{END_YEAR}-12", "months": 312},
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "housing": {
            "indicator": 31452,
            "markets": ["primary", "secondary"],
            "apartment_types": "all",
            "raw_frequency": "quarterly; anchored to quarter-end months",
            "monthly_method": "linear interpolation between quarter-end anchors; Jan-Feb 2000 backfilled from Q1",
            "raw_observations": housing_count,
            "unit": "RUB per square meter",
        },
        "monthly_aggregation": {
            "cbr_fx": "last official rate published in each calendar month",
            "market_indices": "monthly closing value",
        },
        "normalization": {
            "formula": "100 * selected_currency_value / selected_currency_value_in_January_of_start_year",
            "russian_assets_usd": "native RUB value / USD_RUB",
            "us_indices_rub": "native index close * USD_RUB",
            "fx_pairs": "USD/RUB and EUR/RUB remain in standard quotes in both display currencies",
            "dividends": "excluded",
            "inflation_adjustment": "excluded",
        },
        "sources": {
            key: {"name": item.name, "url": item.url, "raw_file": f"raw/{item.raw_filename}"}
            for key, item in downloads.items()
        },
        "series": series_metadata,
        "columns": {
            "moscow_secondary_rub_m2": "Moscow secondary housing, all apartment types, RUB/m2",
            "spb_secondary_rub_m2": "Saint Petersburg secondary housing, all apartment types, RUB/m2",
            "moscow_primary_rub_m2": "Moscow primary housing, all apartment types, RUB/m2",
            "spb_primary_rub_m2": "Saint Petersburg primary housing, all apartment types, RUB/m2",
            "usd_rub": "RUB per USD",
            "eur_rub": "RUB per EUR",
            "sp500_close": "S&P 500 price index monthly close, points",
            "imoex_close": "MOEX Russia Index monthly close, points",
            "nasdaq100_close": "Nasdaq-100 price index monthly close, points",
            "russell2000_close": "Russell 2000 price index monthly close, points",
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
    downloads = download_sources()
    rows, housing_count = build_rows(downloads)
    metadata = build_metadata(downloads, housing_count)
    write_outputs(downloads, rows, metadata)
    print(f"Built {CSV_PATH.relative_to(ROOT)} and {DASHBOARD_PATH.relative_to(ROOT)}: {len(rows)} months, 10 series")


if __name__ == "__main__":
    main()
