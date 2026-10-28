"""Find a product's country for codes loaded from an Excel workbook."""

from __future__ import annotations

import json
import re
import threading
import time
from copy import copy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from io import BytesIO
from itertools import chain
from threading import Event
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries
from openpyxl.worksheet.table import TableColumn

from image_find import fetch, has_product_reference, normalize_base_url, page_signals, product_url, search_for_product_url, structured_products


CODE_HEADERS = {"code", "codes", "sku", "item code", "product code", "article", "article number",
                "kod", "kodi", "kode", "artikelnummer", "art nr", "art no"}
COUNTRY_LABELS = {"country", "country of origin", "origin country", "origin", "made in",
                  "land", "herkunftsland", "ursprungsland", "pays d'origine", "paese di origine",
                  "shteti", "vendi i origjinës", "vendi i origjines", "producer country"}
CODE_RE = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
COUNTRY_CODES = {"BE": "Belgium", "DE": "Germany", "FR": "France", "IT": "Italy",
                 "ES": "Spain", "PT": "Portugal", "NL": "Netherlands", "AT": "Austria",
                 "CH": "Switzerland", "GB": "United Kingdom", "GR": "Greece", "PL": "Poland",
                 "HU": "Hungary", "CZ": "Czechia", "SK": "Slovakia", "SI": "Slovenia",
                 "HR": "Croatia", "RO": "Romania", "BG": "Bulgaria", "DK": "Denmark",
                 "SE": "Sweden", "NO": "Norway", "FI": "Finland", "IE": "Ireland",
                 "US": "United States", "CA": "Canada", "AU": "Australia", "NZ": "New Zealand",
                 "ZA": "South Africa", "AR": "Argentina", "CL": "Chile", "BR": "Brazil",
                 "JP": "Japan", "CN": "China", "TR": "Turkey", "RS": "Serbia"}
COUNTRY_CODES_DE = {"BE": "Belgien", "DE": "Deutschland", "FR": "Frankreich", "IT": "Italien",
                    "ES": "Spanien", "PT": "Portugal", "NL": "Niederlande", "AT": "Österreich",
                    "CH": "Schweiz", "GB": "Vereinigtes Königreich", "GR": "Griechenland", "PL": "Polen",
                    "HU": "Ungarn", "CZ": "Tschechien", "SK": "Slowakei", "SI": "Slowenien",
                    "HR": "Kroatien", "RO": "Rumänien", "BG": "Bulgarien", "DK": "Dänemark",
                    "SE": "Schweden", "NO": "Norwegen", "FI": "Finnland", "IE": "Irland",
                    "US": "Vereinigte Staaten", "CA": "Kanada", "AU": "Australien", "NZ": "Neuseeland",
                    "ZA": "Südafrika", "AR": "Argentinien", "CL": "Chile", "BR": "Brasilien",
                    "JP": "Japan", "CN": "China", "TR": "Türkei", "RS": "Serbien"}
COUNTRY_NAME_TO_CODE = {name.casefold(): code for mapping in (COUNTRY_CODES, COUNTRY_CODES_DE)
                        for code, name in mapping.items()}
COUNTRY_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
COUNTRY_CACHE_LOCK = threading.Lock()
COUNTRY_CACHE_SECONDS = 60 * 60


def translate_country(country: str, language: str) -> str:
    if not country:
        return ""
    code = country.upper() if country.upper() in COUNTRY_CODES else COUNTRY_NAME_TO_CODE.get(country.casefold())
    if not code:
        return country
    return (COUNTRY_CODES_DE if language == "DE" else COUNTRY_CODES).get(code, country)


def read_code_rows(data: bytes, column: str = "") -> list[tuple[int, str]]:
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("The Excel file is too large (maximum 10 MB).")
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("Upload a valid .xlsx file.") from exc
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        first = next(rows, None)
        if first is None:
            raise ValueError("The Excel file is empty.")
        requested = column.strip()
        if requested:
            header_index = next((i for i, value in enumerate(first)
                                 if str(value or "").strip().casefold() == requested.casefold()), -1)
            if header_index >= 0:
                index = header_index
                header = requested.casefold()
            elif re.fullmatch(r"[A-Za-z]", requested):
                index = column_index_from_string(requested.upper()) - 1
                header = str(first[index] or "").strip().casefold() if index < len(first) else ""
            else:
                raise ValueError("The specified column was not found in the first row.")
        else:
            index = next((i for i, value in enumerate(first)
                          if str(value or "").strip().casefold() in CODE_HEADERS), 0)
            header = str(first[index] or "").strip().casefold() if index < len(first) else ""
        codes = []
        start_row = 2 if header in CODE_HEADERS else 1
        for row_number, row in enumerate(rows if header in CODE_HEADERS else chain((first,), rows), start_row):
            value = row[index] if index < len(row) else None
            if value is None or str(value).strip() == "":
                continue
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            codes.append((row_number, str(value).strip()))
            if len(codes) > 2000:
                raise ValueError("The maximum is 2,000 codes per workbook.")
        if not codes:
            raise ValueError("No codes were found in the selected column.")
        return codes
    finally:
        workbook.close()


def read_codes(data: bytes, column: str = "") -> list[str]:
    return [code for _, code in read_code_rows(data, column)]


def wt_country(code: str) -> tuple[str, str] | None:
    """WT catalog exposes the country alongside the exact product code."""
    payload = {"state": {"searchTerm": code, "current": 1, "resultsPerPage": 12},
               "queryConfig": {"search_fields": {"fullNo": {}},
                               "result_fields": {"fullNo": {"raw": {}}, "slug": {"raw": {}},
                                                 "country": {"raw": {}}}}}
    request = Request("https://api.wt.group/elasticsearch/search",
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        results = json.load(response).get("results", [])
    for item in results:
        if str(item.get("fullNo", {}).get("raw", "")).casefold() != code.casefold():
            continue
        country = item.get("country", {}).get("raw", {})
        slug = str(item.get("slug", {}).get("raw", ""))
        if isinstance(country, dict):
            country = country.get("en") or country.get("nl") or next(iter(country.values()), "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            return str(country or "").strip(), f"https://wt.group/products/{slug}"
    return None


def _country_value(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("en") or value.get("value") or ""
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else ""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if 2 <= len(text) <= 60 and not re.search(r"[\d<>]", text) else ""


def country_from_html(html: str, *, dantesse: bool = False) -> str:
    signals = page_signals(html)
    if dantesse:
        for href, label in signals.links:
            if "/shop/" in href:
                match = re.search(r"\(([A-Z]{2})\)\s*$", label.strip())
                if match:
                    return COUNTRY_CODES.get(match.group(1), match.group(1))
    for product in structured_products(signals):
        for key in ("countryOfOrigin", "countryOfManufacture", "country", "originCountry"):
            country = _country_value(product.get(key))
            if country:
                return country
        for prop in product.get("additionalProperty", []):
            if isinstance(prop, dict) and str(prop.get("name", "")).strip().casefold() in COUNTRY_LABELS:
                country = _country_value(prop.get("value"))
                if country:
                    return country
    # Match nearby visible labels; avoid guessing from the company's footer address.
    from html.parser import HTMLParser
    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hidden = 0
            self.parts = []
        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style", "footer"}:
                self.hidden += 1
        def handle_endtag(self, tag):
            if tag in {"script", "style", "footer"} and self.hidden:
                self.hidden -= 1
        def handle_data(self, data):
            if not self.hidden and data.strip():
                self.parts.append(data.strip())
    parser = VisibleText()
    parser.feed(html)
    for i, part in enumerate(parser.parts):
        label = part.casefold().rstrip(":： ")
        if label in COUNTRY_LABELS and i + 1 < len(parser.parts):
            country = _country_value(parser.parts[i + 1])
            if country and country.casefold() not in COUNTRY_LABELS:
                return country
        for prefix in COUNTRY_LABELS:
            match = re.fullmatch(re.escape(prefix) + r"\s*[:：]\s*(.+)", part, re.I)
            if match:
                country = _country_value(match.group(1))
                if country:
                    return country
    return ""


def inspect_country(code: str, site_url: str) -> dict:
    result = {"code": code, "country": "", "status": "Not found", "url": ""}
    if not CODE_RE.fullmatch(code):
        result["status"] = "Invalid code"
        return result
    try:
        host = urlparse(site_url).hostname
        if host in {"wt.group", "www.wt.group"}:
            match = wt_country(code)
            if match:
                country, url = match
                result.update(country=country, url=url, status="Found" if country else "Country not found")
                return result
            return result
        url = product_url(site_url, code) if "{code}" in site_url else search_for_product_url(site_url, code)
        if not url:
            return result
        result["url"] = url
        body, content_type = fetch(url, limit=2 * 1024 * 1024)
        if content_type != "text/html":
            result["status"] = "The page is not HTML"
            return result
        html = body.decode("utf-8", errors="replace")
        if urlparse(url).hostname in {"dantesse.com", "www.dantesse.com"}:
            if not has_product_reference(html, code):
                result["status"] = "Code could not be verified"
                return result
            match = re.search(r'<a\b[^>]*href=["\'][^"\']*/shop/[^"\']*["\'][^>]*>\s*[^<]*\(([A-Z]{2})\)\s*</a>', html)
            country = COUNTRY_CODES.get(match.group(1), match.group(1)) if match else country_from_html(html, dantesse=True)
            result.update(country=country, status="Found" if country else "Country not found")
            return result
        signals = page_signals(html)
        visible = " ".join(signals.text)
        products = structured_products(signals)
        exact = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])", visible, re.I))
        exact = exact or any(str(product.get("sku", "")).casefold() == code.casefold() for product in products)
        if not exact:
            result["status"] = "Code could not be verified"
            return result
        country = country_from_html(html, dantesse=urlparse(url).hostname in {"dantesse.com", "www.dantesse.com"})
        result.update(country=country, status="Found" if country else "Country not found")
    except Exception:
        result["status"] = "Search error"
    return result


def cached_country(code: str, site_url: str) -> dict:
    key = (site_url, code)
    with COUNTRY_CACHE_LOCK:
        cached = COUNTRY_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < COUNTRY_CACHE_SECONDS:
            return dict(cached[1])
    result = inspect_country(code, site_url)
    if result["status"] != "Search error":
        with COUNTRY_CACHE_LOCK:
            if len(COUNTRY_CACHE) >= 5000:
                COUNTRY_CACHE.clear()
            COUNTRY_CACHE[key] = (time.monotonic(), dict(result))
    return result


def make_workbook(results: list[dict], source: bytes | None = None,
                  row_numbers: list[int] | None = None, output_column: str = "") -> bytes:
    if source is None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Product countries"
        sheet.append(["Code", "Country", "Status", "Product URL"])
        for result in results:
            sheet.append([result["code"], result["country"], result["status"], result["url"]])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:D{sheet.max_row}"
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor="116B5D")
            cell.font = Font(bold=True, color="FFFFFF")
        for col, width in {"A": 20, "B": 25, "C": 25, "D": 72}.items():
            sheet.column_dimensions[col].width = width
        for row in sheet.iter_rows(min_row=2):
            row[0].number_format = "@"
            row[3].hyperlink = row[3].value if row[3].value else None
    else:
        workbook = load_workbook(BytesIO(source))
        sheet = workbook.active
        original_max_column = sheet.max_column
        target = output_column.strip()
        if target and re.fullmatch(r"[A-Za-z]", target):
            existing = next((cell.column for cell in sheet[1] if str(cell.value or "").strip().casefold() == target.casefold()), None)
            column_index = existing or column_index_from_string(target.upper())
            if sheet.cell(1, column_index).value is None:
                sheet.cell(1, column_index, "Country")
        elif target:
            column_index = next((cell.column for cell in sheet[1] if str(cell.value or "").strip().casefold() == target.casefold()), None)
            if column_index is None:
                column_index = sheet.max_column + 1
                sheet.cell(1, column_index, target)
        else:
            column_index = sheet.max_column + 1
            sheet.cell(1, column_index, "Country")
        if row_numbers and results and str(sheet.cell(row_numbers[0], column_index).value or "").strip() == results[0]["code"]:
            raise ValueError("The country column cannot be the same as the code column.")
        new_column = column_index > original_max_column
        if new_column:
            source_column = max(1, original_max_column)
            header_source = sheet.cell(1, source_column)
            header_target = sheet.cell(1, column_index)
            if header_source.has_style:
                header_target._style = copy(header_source._style)
            source_letter = get_column_letter(source_column)
            target_letter = get_column_letter(column_index)
            source_dimension = sheet.column_dimensions.get(source_letter)
            if source_dimension and source_dimension.width:
                sheet.column_dimensions[target_letter].width = source_dimension.width
            if sheet.auto_filter.ref:
                min_col, min_row, max_col, max_row = range_boundaries(sheet.auto_filter.ref)
                if max_col == original_max_column and min_row == 1:
                    sheet.auto_filter.ref = f"{get_column_letter(min_col)}{min_row}:{target_letter}{max_row}"
            for table in sheet.tables.values():
                min_col, min_row, max_col, max_row = range_boundaries(table.ref)
                if max_col == original_max_column and min_row == 1:
                    table.ref = f"{get_column_letter(min_col)}{min_row}:{target_letter}{max_row}"
                    if table.autoFilter:
                        table.autoFilter.ref = table.ref
                    if table.tableColumns:
                        table.tableColumns.append(TableColumn(
                            id=max(item.id for item in table.tableColumns) + 1,
                            name=str(header_target.value),
                        ))
        for row_number, result in zip(row_numbers or [], results):
            if not result["country"]:
                continue
            cell = sheet.cell(row_number, column_index)
            if not cell.has_style:
                adjacent = sheet.cell(row_number, max(1, column_index - 1))
                if adjacent.has_style:
                    cell._style = copy(adjacent._style)
                    cell.number_format = "General"
            cell.value = result["country"]
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def run_country_search(codes: list[str], site_url: str, on_result=None,
                       cancel_event: Event | None = None, language: str = "EN") -> list[dict | None]:
    if language not in {"DE", "EN"}:
        raise ValueError("Choose DE or EN as the language.")
    if not codes:
        return []
    site_url = normalize_base_url(site_url)
    results = [None] * len(codes)
    groups: dict[str, list[int]] = {}
    for index, code in enumerate(codes):
        groups.setdefault(code, []).append(index)
    unique_codes = list(groups)
    workers = min(12 if urlparse(site_url).hostname in {"dantesse.com", "www.dantesse.com"} else 8, len(unique_codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        next_index = 0
        while pending or next_index < len(unique_codes):
            if cancel_event and cancel_event.is_set():
                for future in pending:
                    future.cancel()
                break
            while next_index < len(unique_codes) and len(pending) < workers:
                code = unique_codes[next_index]
                future = pool.submit(cached_country, code, site_url)
                pending[future] = code
                next_index += 1
            completed, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in completed:
                code = pending.pop(future)
                original = future.result()
                for index in groups[code]:
                    results[index] = dict(original)
                    results[index]["country"] = translate_country(results[index]["country"], language)
                    if on_result:
                        on_result(index, results[index])
    return results
