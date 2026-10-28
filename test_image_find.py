"""Focused checks for code separation and the live-product requirement."""

import unittest
import hashlib
import http.client
import json
from threading import Event
from threading import Thread
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from image_find import (find_photo_url, image_candidates, inspect_product,
                          normalize_base_url, page_signals, parse_codes, PhotoJob,
                          convert_to_png, convert_to_jpg, photo_filename, product_url, riegel_product_url,
                          search_for_product_url, search_urls, Handler, LOGIN_SALT,
                          wt_product_url)


class ImageFindTests(unittest.TestCase):
    def test_login_protects_pages_and_api(self):
        test_hash = hashlib.pbkdf2_hmac("sha256", b"test-password", LOGIN_SALT, 200_000)
        with patch("image_find.LOGIN_EMAIL", "user@example.com"), patch("image_find.LOGIN_HASH", test_hash):
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                worker = Thread(target=server.serve_forever, daemon=True)
                worker.start()

                def request(method, path, payload=None, cookie=""):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    headers = {"Cookie": cookie} if cookie else {}
                    if payload is not None:
                        headers["Content-Type"] = "application/json"
                    connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
                    response = connection.getresponse()
                    result = response.status, dict(response.getheaders()), response.read()
                    connection.close()
                    return result

                try:
                    status, _, page = request("GET", "/")
                    self.assertEqual(status, 200)
                    self.assertIn(b"Login", page)
                    self.assertNotIn(b"Find product images by code.", page)
                    self.assertEqual(request("GET", "/api/status")[0], 401)
                    self.assertEqual(request("POST", "/api/reset", {})[0], 401)
                    self.assertEqual(request("GET", "/app.css")[0], 200)
                    self.assertEqual(request("POST", "/api/login", {"email": "user@example.com", "password": "wrong"})[0], 401)
                    status, headers, _ = request("POST", "/api/login", {"email": "user@example.com", "password": "test-password"})
                    self.assertEqual(status, 200)
                    self.assertIn("HttpOnly", headers["Set-Cookie"])
                    cookie = headers["Set-Cookie"].split(";", 1)[0]
                    self.assertIn(b"Find product images by code.", request("GET", "/", cookie=cookie)[2])
                    self.assertEqual(request("GET", "/api/status", cookie=cookie)[0], 200)
                    self.assertEqual(request("POST", "/api/logout", cookie=cookie)[0], 200)
                    self.assertEqual(request("GET", "/api/status", cookie=cookie)[0], 401)
                finally:
                    server.shutdown()
                    worker.join(timeout=3)

    @staticmethod
    def image_bytes(format_name, mode="RGB"):
        output = BytesIO()
        Image.new(mode, (20, 20), (42, 84, 126, 128) if mode == "RGBA" else (42, 84, 126)).save(
            output, format=format_name)
        return output.getvalue()

    def test_each_line_is_a_separate_code_with_leading_zeros(self):
        self.assertEqual(parse_codes("000\r\n1111\n\n 103210 \n"), ["000", "1111", "103210"])

    def test_codes_with_slashes_are_one_product_and_one_safe_file(self):
        self.assertEqual(parse_codes("209/24\n6244/24"), ["209/24", "6244/24"])
        self.assertEqual(photo_filename("209/24", ".png"), "209%2F24.png")
        self.assertEqual(search_urls("https://shop.example/products", "209/24")[0],
                         "https://shop.example/products?q=209%2F24")

    def test_product_link_replaces_only_final_code(self):
        pattern = normalize_base_url(
            "https://www.dantesse.com/shop/aufstriche/fruchtaufstriche-gelee-von-miot/103210.html"
        )
        self.assertEqual(
            product_url(pattern, "105424"),
            "https://www.dantesse.com/shop/aufstriche/fruchtaufstriche-gelee-von-miot/105424.html",
        )

    def test_unicode_category_is_encoded(self):
        pattern = normalize_base_url("https://dantesse.com/shop/süßigkeiten/nougat/105423.html")
        self.assertIn("/s%C3%BC%C3%9Figkeiten/", pattern)

    def test_category_link_uses_valid_product_route(self):
        pattern = normalize_base_url("https://www.dantesse.com/shop/aufstriche")
        self.assertEqual(product_url(pattern, "103210"),
                         "https://www.dantesse.com/shop/x/y/103210.html")

    def test_photo_without_matching_product_reference_is_missing(self):
        image = '<img src="https://www.dantesse.com/images/articles/105424/105424_photo_1.jpg">'
        self.assertIsNone(find_photo_url(image, "105424"))
        self.assertEqual(
            find_photo_url('<span title="Referenz ist 105424"></span>' + image, "105424"),
            "https://www.dantesse.com/images/articles/105424/105424_photo_1.jpg",
        )

    def test_any_site_can_use_code_in_path_or_query(self):
        self.assertEqual(product_url(normalize_base_url("https://shop.example/products/{code}"), "000"),
                         "https://shop.example/products/000")
        self.assertEqual(product_url(normalize_base_url("https://shop.example/item/123456"), "1111"),
                         "https://shop.example/item/1111")
        self.assertEqual(product_url(normalize_base_url("https://shop.example/search?q={code}"), "ABC9"),
                         "https://shop.example/search?q=ABC9")

    def test_generic_product_image_from_structured_data(self):
        html = ('<script type="application/ld+json">'
                '{"@type":"Product","sku":"ABC9","image":"/images/ABC9.jpg"}'
                '</script><meta property="og:image" content="/fallback.jpg">')
        self.assertEqual(image_candidates(page_signals(html), "https://shop.example/products/ABC9", "ABC9")[0],
                         "https://shop.example/images/ABC9.jpg")

    def test_product_image_url_with_spaces_is_encoded(self):
        html = ('<script type="application/ld+json">'
                '{"@type":"Product","sku":"73310",'
                '"image":"/resources/products/73310/images/Ontwerp zonder titel (54).png"}'
                '</script>')
        self.assertEqual(
            image_candidates(page_signals(html), "https://wt.group/products/example", "73310")[0],
            "https://wt.group/resources/products/73310/images/Ontwerp%20zonder%20titel%20(54).png",
        )

    @patch("image_find.fetch")
    def test_generic_site_search_finds_product_link(self, fake_fetch):
        fake_fetch.return_value = (b'<a href="/products/ABC9">Product ABC9</a>', "text/html")
        self.assertEqual(search_for_product_url("https://shop.example/", "ABC9"),
                         "https://shop.example/products/ABC9")

    def test_search_starts_from_the_supplied_sites_search_page(self):
        url = "https://wt.group/en/products?q=219&size=n_12_n&sort-field=_score"
        self.assertEqual(search_urls(url, "000")[0],
                         "https://wt.group/en/products?q=000&size=n_12_n&sort-field=_score")

    @patch("image_find.fetch")
    @patch("image_find.rendered_product_url")
    def test_client_rendered_results_can_supply_product_link(self, render, fake_fetch):
        fake_fetch.return_value = (b"<html><body>Loading products</body></html>", "text/html")
        render.return_value = "https://shop.example/products/tawny"
        self.assertEqual(search_for_product_url("https://shop.example/products?q=other", "219"),
                         "https://shop.example/products/tawny")
        self.assertEqual(render.call_args.args[0], "https://shop.example/products?q=219")

    @patch("image_find.wt_product_url")
    def test_wt_uses_its_catalog_search(self, wt_search):
        wt_search.return_value = "https://wt.group/products/quinta-do-vesuvio-vintage-port"
        self.assertEqual(search_for_product_url("https://wt.group/en/products", "209/24"),
                         wt_search.return_value)
        wt_search.assert_called_once_with("209/24")

    @patch("image_find.fetch")
    def test_riegel_live_search_requires_exact_article_link(self, fake_fetch):
        fake_fetch.return_value = (
            b'<a href="/art/578005">Wrong item</a><a href="/art/57800">Right item</a>',
            "text/html",
        )
        self.assertEqual(riegel_product_url("57800"), "https://www.riegel.de/art/57800")
        self.assertTrue(any("query=57800" in call.args[0] for call in fake_fetch.call_args_list))

    @patch("image_find.riegel_product_url")
    def test_riegel_listing_link_uses_its_live_search(self, riegel_search):
        riegel_search.return_value = "https://www.riegel.de/art/58035"
        self.assertEqual(search_for_product_url("https://www.riegel.de/4DCGI/ezshop?action=showPAGE", "58035"),
                         riegel_search.return_value)

    @patch("image_find.fetch")
    def test_generic_product_can_be_reviewed_without_dantesse_markup(self, fake_fetch):
        html = (b'<h1>Product ABC9</h1><script type="application/ld+json">'
                b'{"@type":"Product","sku":"ABC9","image":"/images/ABC9.jpg"}</script>')
        fake_fetch.side_effect = [(html, "text/html"), (self.image_bytes("JPEG"), "image/jpeg")]
        with TemporaryDirectory() as folder:
            result = inspect_product("ABC9", "https://shop.example/products/{code}", Path(folder), 0)
            self.assertEqual(result["state"], "review")
            self.assertEqual(result["photo_url"], "https://shop.example/images/ABC9.jpg")
            self.assertEqual(result["extension"], ".png")
            self.assertTrue(Path(result["cache_path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertTrue(Path(result["cache_path"]).is_file())

    def test_webp_with_transparency_is_converted_to_png(self):
        output = convert_to_png(self.image_bytes("WEBP", "RGBA"))
        with Image.open(BytesIO(output)) as converted:
            self.assertEqual(converted.format, "PNG")
            self.assertEqual(converted.mode, "RGBA")
            self.assertEqual(converted.size, (20, 20))

    def test_jpg_export_has_white_background_for_transparency(self):
        output = BytesIO()
        Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(output, format="PNG")
        with Image.open(BytesIO(convert_to_jpg(output.getvalue()))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((10, 10)), (255, 255, 255))

    def test_save_all_can_write_jpg_without_changing_preview(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            preview = base / "preview.png"
            preview.write_bytes(convert_to_png(self.image_bytes("WEBP", "RGBA")))
            job = PhotoJob()
            job.destination = base / "output"
            job.items = [{"index": 0, "code": "209/24", "state": "review", "cache_path": str(preview)}]
            self.assertEqual(job.save_all("JPG"), {"saved": 1, "existing": 0})
            target = job.destination / "209%2F24.jpg"
            with Image.open(target) as image:
                self.assertEqual(image.format, "JPEG")
            self.assertTrue(preview.read_bytes().startswith(b"\x89PNG"))

    def test_save_all_ignores_skipped_and_missing(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            image = base / "preview.png"
            image.write_bytes(convert_to_png(self.image_bytes("JPEG")))
            job = PhotoJob()
            job.destination = base / "output"
            job.items = [
                {"index": 0, "code": "209/24", "state": "review", "cache_path": str(image), "extension": ".png"},
                {"index": 1, "code": "1111", "state": "skipped", "cache_path": str(image), "extension": ".png"},
                {"index": 2, "code": "2222", "state": "missing"},
            ]
            self.assertEqual(job.save_all(), {"saved": 1, "existing": 0})
            self.assertEqual([path.name for path in job.destination.iterdir()], ["209%2F24.png"])
            self.assertEqual(job.items[1]["state"], "skipped")
            self.assertFalse(job.saving)

    @patch("image_find.inspect_product")
    def test_refresh_reset_clears_photo_results_and_stops_queued_work(self, inspect):
        started, release = Event(), Event()
        def delayed(code, pattern, cache_dir, index):
            started.set()
            release.wait(3)
            return {"index": index, "code": code, "state": "missing", "preview": None}
        inspect.side_effect = delayed
        with TemporaryDirectory() as folder:
            job = PhotoJob()
            job.start(folder, "https://shop.example/products/{code}", "\n".join(str(n) for n in range(20)))
            self.assertTrue(started.wait(2))
            job.reset()
            self.assertEqual(job.snapshot()["total"], 0)
            release.set()


if __name__ == "__main__":
    unittest.main()
