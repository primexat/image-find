"""Find ingredient text on verified product pages and export it to Excel."""

from __future__ import annotations

import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from html.parser import HTMLParser
from io import BytesIO
from threading import Event
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from image_find import (CODE_PATTERN, fetch, has_product_reference, normalize_base_url,
                        page_signals, product_url, search_for_product_url, structured_products)


INGREDIENT_LABEL = re.compile(
    r"^(?:zutaten|ingredients|inhaltsstoffe|ingrediënten|ingrédients|ingredienti)\s*(?::\s*(.*))?$",
    re.IGNORECASE,
)
NEXT_SECTION = re.compile(
    r"^(?:allergene?|allergens?|nährwerte?|nährwertangaben|nutrition(?:al)?(?: information| values| facts)?|"
    r"beschreibung|description|produktbeschreibung|product details|details|hinweise?|"
    r"aufbewahrung|storage|bewertungen|reviews?|herkunft|origin|kontakt|contact)\s*:?(?:\s|$)",
    re.IGNORECASE,
)
BLOCK_TAGS = {"p", "div", "section", "article", "li", "dt", "dd", "br", "tr", "td", "th"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header"}
EXCEL_CELL_LIMIT = 32767


class ProductText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.heading_text: list[str] | None = None
        self.paragraph_text: list[str] | None = None
        self.awaiting_ingredients = False
        self.ingredient_paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip += 1
        if self.skip:
            return
        if tag in HEADING_TAGS:
            self.heading_text = []
            self.awaiting_ingredients = False
            self.parts.append("\n§ ")
        elif tag == "p" and self.awaiting_ingredients:
            self.paragraph_text = []
            self.parts.append("\n")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip == 0 and tag in HEADING_TAGS and self.heading_text is not None:
            heading = re.sub(r"\s+", " ", "".join(self.heading_text)).strip()
            self.awaiting_ingredients = INGREDIENT_LABEL.fullmatch(heading) is not None
            self.heading_text = None
        if self.skip == 0 and tag == "p" and self.paragraph_text is not None:
            value = re.sub(r"\s+", " ", "".join(self.paragraph_text)).strip()
            if value:
                self.ingredient_paragraphs.append(value)
            self.paragraph_text = None
            self.awaiting_ingredients = False
        if self.skip == 0 and tag in HEADING_TAGS | BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip == 0:
            self.parts.append(data)
            if self.heading_text is not None:
                self.heading_text.append(data)
            if self.paragraph_text is not None:
                self.paragraph_text.append(data)


def ingredients_from_html(html: str) -> str:
    parser = ProductText()
    parser.feed(html)
    if parser.ingredient_paragraphs:
        return parser.ingredient_paragraphs[0][:EXCEL_CELL_LIMIT]
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        match = INGREDIENT_LABEL.fullmatch(line.removeprefix("§ ").strip())
        if not match:
            continue
        pieces = [match.group(1).strip()] if match.group(1) else []
        for following in lines[index + 1:]:
            if following.startswith("§ ") or NEXT_SECTION.match(following) or INGREDIENT_LABEL.fullmatch(following):
                break
            pieces.append(following)
            if sum(map(len, pieces)) >= EXCEL_CELL_LIMIT:
                break
        value = re.sub(r"\s+", " ", " ".join(pieces)).strip(" :;\u00a0")
        if len(value) >= 3:
            return value[:EXCEL_CELL_LIMIT]
    return ""


def inspect_ingredients(code: str, site_url: str) -> dict:
    result = {"code": code, "ingredients": "", "status": "Not found", "url": ""}
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
        ingredients = ingredients_from_html(html)
        result.update(ingredients=ingredients, status="Found" if ingredients else "Ingredients not found")
    except Exception:
        result["status"] = "Search error"
    return result


def run_ingredient_search(codes: list[str], site_url: str, on_result=None,
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
                pending[pool.submit(inspect_ingredients, code, site_url)] = code
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


def make_ingredient_workbook(results: list[dict]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ingredients"
    sheet.append(["Code", "Ingredients", "Status", "Product URL"])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:D{len(results) + 1}"
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="475BD7")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 27
    for letter, width in {"A": 22, "B": 95, "C": 26, "D": 65}.items():
        sheet.column_dimensions[letter].width = width
    for result in results:
        sheet.append([result["code"], result["ingredients"], result["status"], result["url"]])
        row = sheet.max_row
        sheet.cell(row, 1).number_format = "@"
        sheet.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
        if result["url"]:
            sheet.cell(row, 4).hyperlink = result["url"]
        sheet.row_dimensions[row].height = 45 if result["ingredients"] else 23
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
