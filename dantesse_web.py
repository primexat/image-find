"""Local product photo finder with optional review before saving images."""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
import webbrowser
from io import BytesIO
from http.cookiejar import CookieJar
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from PIL import Image, ImageOps, UnidentifiedImageError


ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "web" / "dantesse.html"
CODE_PATTERN = re.compile(r"(?=.{1,40}\Z)[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
MAX_CODES = 2000
MAX_IMAGE_BYTES = 15 * 1024 * 1024
_network = threading.local()


def parse_codes(raw: str) -> list[str]:
    """One nonempty line is one product; keep codes as strings and leading zeros."""
    if not isinstance(raw, str):
        raise ValueError("Lista e kodeve duhet të jetë tekst.")
    codes = [line.strip() for line in raw.splitlines() if line.strip()]
    if not codes:
        raise ValueError("Shkruaj të paktën një kod, një kod për rresht.")
    if len(codes) > MAX_CODES:
        raise ValueError(f"Mund të kontrollohen deri në {MAX_CODES} kode për një kërkim.")
    invalid = [code for code in codes if not CODE_PATTERN.fullmatch(code)]
    if invalid:
        raise ValueError(f"Kodi '{invalid[0]}' ka shenja të palejuara.")
    return codes


def normalize_base_url(raw: str) -> str:
    """Accept any HTTP(S) site, sample product URL, or URL with {code}."""
    if not isinstance(raw, str):
        raise ValueError("Shkruaj linkun e faqes.")
    value = raw.strip()
    parsed = urlparse(value.replace("{code}", "CODE_PLACEHOLDER"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Përdor një link të plotë që fillon me http:// ose https://.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Linku nuk duhet të ketë hyrje ose fragment (#...).")
    path = parsed.path
    query = parsed.query
    if "CODE_PLACEHOLDER" in path or "CODE_PLACEHOLDER" in query:
        path = path.replace("CODE_PLACEHOLDER", "{code}")
        query = query.replace("CODE_PLACEHOLDER", "{code}")
        if (path + query).count("{code}") != 1:
            raise ValueError("Vendose {code} vetëm një herë në link.")
    elif parsed.hostname in {"dantesse.com", "www.dantesse.com"} and re.search(r"/[A-Za-z0-9_-]+\.html$", path):
        path = re.sub(r"/[A-Za-z0-9_-]+\.html$", "/{code}.html", path)
    elif parsed.hostname in {"dantesse.com", "www.dantesse.com"}:
        path = "/shop/x/y/{code}.html"
    else:
        # A numeric SKU at the end of a sample product link can be replaced.
        match = re.search(r"/\d{3,}[A-Za-z0-9_-]*(?:\.[A-Za-z0-9]+)?$", path)
        if match:
            suffix = Path(match.group()).suffix
            path = path[:match.start()] + "/{code}" + suffix
        else:
            query_match = re.search(r"(?i)([?&](?:sku|code|product_id|id)=)[A-Za-z0-9_-]+", "?" + query)
            if query_match:
                query = re.sub(r"(?i)((?:^|&)(?:sku|code|product_id|id)=)[A-Za-z0-9_-]+",
                               r"\g<1>{code}", query, count=1)
    path = quote(path, safe="/%{}:@-._~")
    query = quote(query, safe="=&/%{}:+-._~")
    return parsed._replace(path=path, query=query).geturl()


def product_url(pattern: str, code: str) -> str:
    if "{code}" not in pattern:
        raise ValueError("Ky link nuk ka model kodi. Përdor kërkimin e faqes.")
    return pattern.replace("{code}", quote(code, safe=""))


def photo_filename(code: str, extension: str) -> str:
    """Encode slashes so a product code remains one Windows-safe filename."""
    return f"{quote(code, safe='')}{extension}"


def convert_to_png(data: bytes) -> bytes:
    """Decode the actual image and preserve transparency and EXIF orientation."""
    with Image.open(BytesIO(data)) as source:
        if source.format not in {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "AVIF"}:
            raise ValueError("Formati i fotos nuk mbështetet.")
        image = ImageOps.exif_transpose(source)
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        image = image.convert("RGBA" if has_alpha else "RGB")
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def fetch(url: str, *, limit: int = MAX_IMAGE_BYTES, referer: str | None = None) -> tuple[bytes, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ImageFindLocal/1.0)"}
    if referer:
        headers["Referer"] = referer
    request = Request(url, headers=headers)
    if not hasattr(_network, "opener"):
        _network.opener = build_opener(HTTPCookieProcessor(CookieJar()))
    with _network.opener.open(request, timeout=25) as response:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Skedari është tepër i madh.")
    return body, content_type


def has_product_reference(html: str, code: str) -> bool:
    marker = re.compile(r"title=[\"']Referenz ist\s+" + re.escape(code) + r"[\"']", re.IGNORECASE)
    return marker.search(html) is not None


def find_photo_url(html: str, code: str) -> str | None:
    if not has_product_reference(html, code):
        return None
    for image in page_signals(html).images:
        url = unescape(image.get("src", ""))
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname in {"dantesse.com", "www.dantesse.com"}:
            if f"/images/articles/{code}/" in parsed.path:
                return url
    return None


class PageSignals(HTMLParser):
    """Collect product image hints and links without assuming one shop platform."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.images: list[dict[str, str]] = []
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self.json_scripts: list[str] = []
        self._link: str | None = None
        self._link_text: list[str] = []
        self._json_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            if name:
                self.meta[name] = values.get("content", "")
        elif tag == "img":
            self.images.append(values)
        elif tag == "a":
            self._link = values.get("href")
            self._link_text = []
        elif tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._json_text = []

    def handle_data(self, data: str) -> None:
        if self._json_text is not None:
            self._json_text.append(data)
        else:
            self.text.append(data)
        if self._link is not None:
            self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            self.links.append((self._link, " ".join(self._link_text)))
            self._link = None
            self._link_text = []
        elif tag == "script" and self._json_text is not None:
            self.json_scripts.append("".join(self._json_text))
            self._json_text = None


def page_signals(html: str) -> PageSignals:
    signals = PageSignals()
    signals.feed(html)
    return signals


def structured_products(signals: PageSignals) -> list[dict]:
    products: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            kinds = node.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if any(str(kind).lower().endswith("product") for kind in kinds):
                products.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for script in signals.json_scripts:
        try:
            walk(json.loads(script))
        except json.JSONDecodeError:
            continue
    return products


def image_candidates(signals: PageSignals, page_url: str, code: str) -> list[str]:
    candidates: list[str] = []
    for product in structured_products(signals):
        images = product.get("image", [])
        if isinstance(images, (str, dict)):
            images = [images]
        for image in images if isinstance(images, list) else []:
            if isinstance(image, str):
                candidates.append(image)
            elif isinstance(image, dict):
                candidates.append(str(image.get("url") or image.get("contentUrl") or ""))
    ranked: list[tuple[int, str]] = []
    for image in signals.images:
        src = image.get("data-src") or image.get("data-original") or image.get("src") or ""
        if not src and image.get("srcset"):
            src = image["srcset"].split(",")[-1].strip().split(" ")[0]
        details = " ".join((src, image.get("alt", ""), image.get("class", ""), image.get("id", ""))).lower()
        score = (100 if code.lower() in details else 0) + (40 if "product" in details else 0)
        if any(word in details for word in ("logo", "icon", "sprite", "banner", "avatar")):
            score -= 100
        if score >= 0:
            ranked.append((score, src))
    ranked.sort(key=lambda item: item[0], reverse=True)
    candidates.extend(src for score, src in ranked if score >= 100)
    for key in ("og:image:secure_url", "og:image", "twitter:image"):
        if signals.meta.get(key):
            candidates.append(signals.meta[key])
    candidates.extend(src for score, src in ranked if score < 100)
    result: list[str] = []
    for candidate in candidates:
        if not candidate or candidate.startswith("data:"):
            continue
        url = urljoin(page_url, unescape(candidate))
        if urlparse(url).scheme in {"http", "https"} and url not in result:
            result.append(url)
    return result[:12]


def search_urls(site_url: str, code: str) -> list[str]:
    parsed = urlparse(site_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    urls = []
    query = parse_qsl(parsed.query, keep_blank_values=True)
    search_keys = {"q", "s", "search", "query", "keyword", "term"}
    for key, value in query:
        if key.lower() in search_keys:
            updated = [(name, code if name == key else current)
                       for name, current in query]
            urls.append(parsed._replace(query=urlencode(updated)).geturl())
            break
    if parsed.path and parsed.path != "/":
        path = parsed.path.rstrip("/")
        if re.fullmatch(r"/[a-z]{2}(?:-[A-Za-z]{2})?", path):
            urls.append(f"{origin}{path}/products?q={quote(code, safe='')}")
        urls.extend(f"{origin}{path}?{key}={quote(code, safe='')}" for key in ("q", "s"))
    encoded = quote(code, safe="")
    urls.extend((f"{origin}/search?q={encoded}", f"{origin}/products?q={encoded}",
                 f"{origin}/?s={encoded}", f"{origin}/?q={encoded}"))
    return list(dict.fromkeys(urls))


def rendered_product_url(search_url: str, code: str, hostname: str) -> str | None:
    """Read client-rendered result cards when the initial HTML has no products."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                link = page.evaluate("""(code) => {
                    const same = (value) => value?.trim().toLowerCase() === code.toLowerCase();
                    for (const element of document.querySelectorAll('body *')) {
                        if (element.children.length || !same(element.textContent)) continue;
                        let card = element;
                        for (let depth = 0; depth < 5 && card; depth++, card = card.parentElement) {
                            const anchor = card.querySelector('a[href]');
                            if (anchor && card.querySelector('img')) return anchor.href;
                        }
                    }
                    return null;
                }""", code)
                if link and urlparse(link).hostname == hostname:
                    return link
            finally:
                browser.close()
    except Exception:
        return None
    return None


def wt_product_url(code: str) -> str | None:
    """Use WT's own catalog search and require an exact product number."""
    payload = {
        "state": {"searchTerm": code, "current": 1, "resultsPerPage": 12},
        "queryConfig": {
            "search_fields": {"fullNo": {}},
            "result_fields": {"fullNo": {"raw": {}}, "slug": {"raw": {}}},
        },
    }
    request = Request(
        "https://api.wt.group/elasticsearch/search",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            results = json.load(response).get("results", [])
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    for item in results:
        if str(item.get("fullNo", {}).get("raw", "")).casefold() != code.casefold():
            continue
        slug = str(item.get("slug", {}).get("raw", ""))
        if re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            return f"https://wt.group/products/{slug}"
    return None


def riegel_product_url(code: str) -> str | None:
    """Search Riegel's live catalog, regardless of the supplied Riegel page."""
    query = urlencode({"action": "livesuche_search", "hid": "120", "sprachnr": "de",
                       "enter": "livesuche", "uuid": "", "query": code})
    search_url = f"https://www.riegel.de/4DCGI/ezshop?{query}"
    try:
        body, content_type = fetch(search_url, limit=2 * 1024 * 1024)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None
    if content_type != "text/html":
        return None
    for href, _ in page_signals(body.decode("utf-8-sig", errors="replace")).links:
        target = urljoin(search_url, unescape(href))
        parsed = urlparse(target)
        if parsed.hostname in {"riegel.de", "www.riegel.de"} and parsed.path == f"/art/{code}":
            return target
    return None


def search_for_product_url(site_url: str, code: str) -> str | None:
    parsed = urlparse(site_url)
    if parsed.hostname in {"riegel.de", "www.riegel.de"}:
        match = riegel_product_url(code)
        if match:
            return match
    if parsed.hostname in {"wt.group", "www.wt.group"}:
        match = wt_product_url(code)
        if match:
            return match
    urls = search_urls(site_url, code)
    for search_url in urls:
        try:
            body, content_type = fetch(search_url, limit=2 * 1024 * 1024)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            continue
        if content_type != "text/html":
            continue
        signals = page_signals(body.decode("utf-8", errors="replace"))
        matches: list[tuple[int, str]] = []
        for href, text in signals.links:
            target = urljoin(search_url, unescape(href))
            target_parsed = urlparse(target)
            if target_parsed.hostname != parsed.hostname or target == search_url:
                continue
            exact_code = re.compile(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])", re.I)
            if not exact_code.search(target_parsed.path + " " + text):
                continue
            score = (10 if exact_code.search(target_parsed.path) else 0) + (5 if "product" in target.lower() else 0)
            matches.append((score, target))
        if matches:
            return max(matches, key=lambda pair: pair[0])[1]
    # A site's search results may be inserted only after JavaScript runs.
    for search_url in urls[:3]:
        match = rendered_product_url(search_url, code, parsed.hostname)
        if match:
            return match
    return None


def inspect_product(code: str, pattern: str, cache_dir: Path, index: int) -> dict:
    result = {"index": index, "code": code, "url": "", "state": "error", "message": "", "preview": None}
    try:
        if "{code}" in pattern:
            url = product_url(pattern, code)
        else:
            url = search_for_product_url(pattern, code)
            if not url:
                result["message"] = "Kërkimi i kësaj faqeje nuk u verifikua. Jep një link me {code}."
                return result
        result["url"] = url
        body, content_type = fetch(url, limit=2 * 1024 * 1024)
        if content_type != "text/html":
            raise ValueError("Faqja nuk ktheu HTML.")
        html = body.decode("utf-8", errors="replace")
        host = urlparse(url).hostname
        is_dantesse = host in {"dantesse.com", "www.dantesse.com"}
        if is_dantesse and not has_product_reference(html, code):
            result.update(state="missing", message="Produkti nuk gjendet në katalog.")
            return result
        signals = page_signals(html)
        products = structured_products(signals)
        visible_text = " ".join(signals.text)
        code_confirmed = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])",
                                        visible_text, re.IGNORECASE))
        code_confirmed = code_confirmed or any(str(product.get("sku", "")).lower() == code.lower()
                                               for product in products)
        if not is_dantesse and not code_confirmed:
            result["message"] = "Kodi nuk u konfirmua në këtë faqe. Jep link produkti me {code}."
            return result
        candidates = ([find_photo_url(html, code)] if is_dantesse else image_candidates(signals, url, code))
        for photo_url in candidates:
            if not photo_url:
                continue
            try:
                image, image_type = fetch(photo_url, referer=url)
                if not image_type.startswith("image/") or len(image) < 100:
                    continue
                png = convert_to_png(image)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnidentifiedImageError):
                continue
            cache_path = cache_dir / f"{index}.png"
            cache_path.write_bytes(png)
            message = "Fotoja u gjet. Kontrolloje para ruajtjes."
            if not is_dantesse and not code_confirmed:
                message = "Fotoja u gjet, por kodi nuk shfaqet në faqe. Verifikoje me kujdes."
            result.update(state="review", message=message, preview=f"/api/preview/{index}",
                          extension=".png", cache_path=str(cache_path), photo_url=photo_url)
            return result
        result.update(state="missing", message="Faqja u gjet, por nuk ka foto të përdorshme.")
    except HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            result.update(state="missing", message="Faqja e produktit nuk u gjet.")
        else:
            result["message"] = f"Faqja ktheu gabimin HTTP {exc.code}. Provo përsëri."
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        result["message"] = f"Nuk u verifikua: {exc}"
    return result


class PhotoJob:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.saving = False
        self.items: list[dict] = []
        self.done = 0
        self.destination: Path | None = None
        self.cache: tempfile.TemporaryDirectory | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "saving": self.saving,
                    "done": self.done, "total": len(self.items),
                    "destination": str(self.destination) if self.destination else "",
                    "items": [{key: value for key, value in item.items() if key != "cache_path"}
                              for item in self.items]}

    def start(self, destination: str, base_url: str, raw_codes: str) -> None:
        codes = parse_codes(raw_codes)
        pattern = normalize_base_url(base_url)
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError("Shkruaj folderin ku do të ruhen fotot.")
        target = Path(destination.strip()).expanduser()
        if not target.is_absolute():
            raise ValueError("Përdor një path absolut për folderin e fotove.")
        if target.exists() and not target.is_dir():
            raise ValueError("Path-i i fotove duhet të jetë folder.")
        with self.lock:
            if self.running or self.saving:
                raise ValueError("Prit derisa të përfundojë puna në proces.")
            if self.cache:
                self.cache.cleanup()
            self.cache = tempfile.TemporaryDirectory(prefix="image_find_photos_")
            self.destination = target
            self.items = [{"index": index, "code": code, "state": "waiting", "message": "Në pritje", "preview": None}
                          for index, code in enumerate(codes)]
            self.done = 0
            self.running = True
            cache_dir = Path(self.cache.name)
        threading.Thread(target=self._run, args=(codes, pattern, cache_dir), daemon=True).start()

    def _run(self, codes: list[str], pattern: str, cache_dir: Path) -> None:
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(inspect_product, code, pattern, cache_dir, index): index
                           for index, code in enumerate(codes)}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # Keep a single bad page from stopping the batch.
                        result = {"index": index, "code": codes[index], "state": "error",
                                  "message": f"Gabim gjatë kontrollit: {exc}", "preview": None}
                    with self.lock:
                        self.items[index] = result
                        self.done += 1
        finally:
            with self.lock:
                self.running = False

    def preview(self, index: int) -> tuple[bytes, str] | None:
        with self.lock:
            if not 0 <= index < len(self.items):
                return None
            item = self.items[index]
            if item["state"] not in {"review", "skipped", "saved", "exists"}:
                return None
            path = Path(item["cache_path"])
        if not path.is_file():
            return None
        return path.read_bytes(), "image/png"

    def save(self, indexes: list[int]) -> dict:
        if not isinstance(indexes, list) or not all(type(index) is int for index in indexes):
            raise ValueError("Zgjedhja e fotove nuk është e vlefshme.")
        with self.lock:
            if self.running or self.saving:
                raise ValueError("Prit derisa të përfundojë puna në proces.")
            if self.destination is None:
                raise ValueError("Nis një kërkim më parë.")
            chosen = [self.items[index] for index in dict.fromkeys(indexes)
                      if 0 <= index < len(self.items) and self.items[index]["state"] == "review"]
            target_dir = self.destination
            if not chosen:
                raise ValueError("Nuk ka foto të gjetura për ruajtje.")
            self.saving = True
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if not target_dir.is_dir():
                raise ValueError("Folderi i fotove nuk mund të krijohet.")
            saved = existing = 0
            for item in chosen:
                source = Path(item["cache_path"])
                target = target_dir / photo_filename(item["code"], ".png")
                if target.exists():
                    state, message = "exists", "Skedari ekziston; nuk u mbishkrua."
                    existing += 1
                else:
                    shutil.copyfile(source, target)
                    state, message = "saved", f"U ruajt: {target}"
                    saved += 1
                with self.lock:
                    item["state"] = state
                    item["message"] = message
            return {"saved": saved, "existing": existing}
        finally:
            with self.lock:
                self.saving = False

    def save_all(self) -> dict:
        with self.lock:
            indexes = [item["index"] for item in self.items if item["state"] == "review"]
        return self.save(indexes)

    def review(self, index: int, action: str) -> None:
        if type(index) is not int or action not in {"skip", "reopen"}:
            raise ValueError("Veprimi nuk është i vlefshëm.")
        with self.lock:
            if self.running or self.saving or not 0 <= index < len(self.items):
                raise ValueError("Prit derisa të përfundojë puna në proces.")
            item = self.items[index]
            if action == "skip" and item["state"] == "review":
                item["state"] = "skipped"
                item["message"] = "U la jashtë; fotoja nuk u ruajt."
            elif action == "reopen" and item["state"] == "skipped":
                item["state"] = "review"
                item["message"] = "Kontrolloje para ruajtjes."
            else:
                raise ValueError("Kjo foto nuk mund të ndryshohet tani.")


JOB = PhotoJob()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return

    def send_json(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def request_json(self) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 512_000:
                raise ValueError("Kërkesa është e zbrazët ose tepër e madhe.")
            data = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Të dhënat nuk janë të vlefshme.")
            return data
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(str(exc)) from exc

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self.send_json(JOB.snapshot())
        elif path.startswith("/api/preview/"):
            try:
                index = int(path.rsplit("/", 1)[1])
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            preview = JOB.preview(index)
            if preview is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body, mime = preview
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            data = self.request_json()
            path = urlparse(self.path).path
            if path == "/api/start":
                JOB.start(data.get("destination"), data.get("base_url"), data.get("codes"))
                self.send_json({"ok": True, "message": "Kontrolli nisi."})
            elif path == "/api/save":
                result = JOB.save(data.get("indexes"))
                self.send_json({"ok": True, **result})
            elif path == "/api/save-all":
                result = JOB.save_all()
                self.send_json({"ok": True, **result})
            elif path == "/api/review":
                JOB.review(data.get("index"), data.get("action"))
                self.send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, OSError) as exc:
            self.send_json({"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8766), Handler)
    print("Image Find: http://127.0.0.1:8766", flush=True)
    if "--open" in sys.argv:
        webbrowser.open("http://127.0.0.1:8766")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
