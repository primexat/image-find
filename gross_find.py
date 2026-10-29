"""Find gross product weights and export the results to Excel."""

from __future__ import annotations

import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from io import BytesIO
from threading import Event
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from image_find import (CODE_PATTERN, fetch, has_product_reference, normalize_base_url,
                        page_signals, product_url, search_for_product_url, structured_products)


LABEL = re.compile(r"\b(?:gross\s*weight|brutto\s*gewicht|bruttogewicht|poids\s*brut|peso\s*bruto|pesha\s*bruto|gross|brutto)\b", re.I)
WEIGHT = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|gram(?:s|m)?|kilogram(?:s|m)?|lb|lbs|oz)\b", re.I)
WEIGHT_VALUE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)(\s*)(kg|g|grams?|gramm|kilograms?|kilogramm|lbs?|oz)\s*$", re.I)


def parse_prefixes(values: list[str] | None) -> list[str]:
    if values is None:
        values = ["01", "03", "06", "12"]
    if not isinstance(values, list) or len(values) > 4 or any(not isinstance(value, str) for value in values):
        raise ValueError("Provide up to four numeric prefixes.")
    prefixes = [value.strip() for value in values if value.strip()]
    if any(not re.fullmatch(r"\d{1,4}", value) or int(value) < 1 for value in prefixes):
        raise ValueError("Each prefix must be a positive number with up to four digits.")
    if len({int(value) for value in prefixes}) != len(prefixes):
        raise ValueError("Use different prefix numbers.")
    return prefixes


def multiply_weight(weight: str, prefix: str) -> str:
    match = WEIGHT_VALUE.fullmatch(weight)
    if not match:
        return ""
    try:
        amount = Decimal(match.group(1).replace(",", ".")) * int(prefix)
    except InvalidOperation:
        return ""
    number = format(amount.normalize(), "f")
    if "," in match.group(1):
        number = number.replace(".", ",")
    return f"{number}{match.group(2)}{match.group(3)}"


def add_calculations(result: dict, prefixes: list[str]) -> dict:
    return {**result, "calculated": [multiply_weight(result.get("gross", ""), prefix)
                                      for prefix in prefixes]}


class VisibleLines(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.heading: list[str] | None = None
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header"}:
            self.skip += 1
        elif not self.skip and tag in {"p", "div", "section", "article", "li", "dt", "dd", "br", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")
        if not self.skip and tag in {"h1", "h2", "h3", "h4"}:
            self.heading = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header"}:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in {"p", "div", "section", "article", "li", "dt", "dd", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")
        if not self.skip and tag in {"h1", "h2", "h3", "h4"} and self.heading is not None:
            self.headings.append(re.sub(r"\s+", " ", "".join(self.heading)).strip())
            self.heading = None

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)
            if self.heading is not None:
                self.heading.append(data)


def gross_from_html(html: str) -> str:
    parser = VisibleLines()
    parser.feed(html)
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        label = LABEL.search(line)
        if not label:
            continue
        same_line = WEIGHT.search(line[label.end():])
        if same_line:
            return same_line.group().strip()
        if index + 1 < len(lines) and len(line[label.end():].strip(" :")) == 0:
            next_line = lines[index + 1]
            if not LABEL.search(next_line):
                weight = WEIGHT.fullmatch(next_line.strip(" :"))
                if weight:
                    return weight.group().strip()
    # Many shops put the package weight in the product name (for example,
    # "Weicher Nougat - Mandeln 150g") instead of a separate gross-weight field.
    for heading in parser.headings:
        if re.search(r"(?:nährwert|nutrition|ingredient|zutat|per\s*100|pro\s*100)", heading, re.I):
            continue
        weight = WEIGHT.search(heading)
        if weight:
            return weight.group().strip()
    return ""


def inspect_gross(code: str, site_url: str) -> dict:
    result = {"code": code, "gross": "", "status": "Not found", "url": ""}
    if not CODE_PATTERN.fullmatch(code):
        result["status"] = "Invalid code"
        return result
    try:
        url = product_url(site_url, code) if "{code}" in site_url else search_for_product_url(site_url, code)
        if not url:
            return result
        result["url"] = url
        body, content_type = fetch(url, limit=2 * 1024 * 1024)
        if content_type != "text/html":
            result["status"] = "Product page is not HTML"
            return result
        html = body.decode("utf-8", errors="replace")
        host = urlparse(url).hostname
        if host in {"dantesse.com", "www.dantesse.com"}:
            verified = has_product_reference(html, code)
        else:
            signals = page_signals(html)
            visible = " ".join(signals.text)
            verified = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])", visible, re.I))
            verified = verified or any(str(product.get("sku", "")).casefold() == code.casefold()
                                       for product in structured_products(signals))
        if not verified:
            result["status"] = "Code could not be verified"
            return result
        gross = gross_from_html(html)
        result.update(gross=gross, status="Found" if gross else "Gross weight not found")
    except Exception:
        result["status"] = "Search error"
    return result


def run_gross_search(codes: list[str], site_url: str, on_result=None,
                     cancel_event: Event | None = None) -> list[dict | None]:
    if not codes:
        return []
    site_url = normalize_base_url(site_url)
    results: list[dict | None] = [None] * len(codes)
    groups: dict[str, list[int]] = {}
    for index, code in enumerate(codes):
        groups.setdefault(code, []).append(index)
    unique = list(groups)
    workers = min(10, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        next_index = 0
        while pending or next_index < len(unique):
            if cancel_event and cancel_event.is_set():
                for future in pending:
                    future.cancel()
                break
            while next_index < len(unique) and len(pending) < workers:
                code = unique[next_index]
                pending[pool.submit(inspect_gross, code, site_url)] = code
                next_index += 1
            completed, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in completed:
                code = pending.pop(future)
                result = future.result()
                for index in groups[code]:
                    results[index] = dict(result)
                    if on_result:
                        on_result(index, results[index])
    return results


def make_gross_workbook(results: list[dict], prefixes: list[str] | None = None) -> bytes:
    prefixes = parse_prefixes(prefixes)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Gross"
    sheet.append(["Code", "Gross weight", None, "TOTALI"])
    sheet.freeze_panes = "A2"
    for cell in sheet[1][:3]:
        cell.fill = PatternFill("solid", fgColor="475BD7")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center")
    sheet.cell(1, 4).font = Font(bold=True, color="18243C")
    sheet.row_dimensions[1].height = 27
    for letter, width in {"A": 22, "B": 22, "C": 22, "D": 18}.items():
        sheet.column_dimensions[letter].width = width
    for result in results:
        code = result["code"]
        gross = result.get("gross", "")
        calculations = result.get("calculated") or add_calculations(result, prefixes)["calculated"]
        sheet.append([code, gross or None, None, None])
        row = sheet.max_row
        sheet.cell(row, 1).number_format = "@"
        if result.get("url"):
            sheet.cell(row, 1).hyperlink = result["url"]
        for prefix, calculated in zip(prefixes, calculations):
            expression = f"({int(prefix)} x {gross})" if gross else None
            sheet.append([f"{prefix}-{code}", gross or None, expression, calculated or None])
            row = sheet.max_row
            sheet.cell(row, 1).number_format = "@"
    sheet.auto_filter.ref = f"A1:D{sheet.max_row}"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
