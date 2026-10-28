"""Ingredient extraction and workbook export checks."""

import unittest
from io import BytesIO
from threading import Event
from unittest.mock import patch

from openpyxl import load_workbook

from image_find import IngredientJob
from ingredients_find import ingredients_from_html, inspect_ingredients, make_ingredient_workbook, run_ingredient_search


class IngredientFindTests(unittest.TestCase):
    def test_dantesse_section_stops_before_other_product_details(self):
        html = """<html><body><h1>Vital</h1><span title="Referenz ist 105430">105430</span>
        <h3>Zutaten:</h3><p>Zucker, Kokosraspeln 24%, Glucosesirup,
        <strong>Kakaopaste</strong>.</p><h3>Nährwerte:</h3><p>123 kcal</p></body></html>"""
        self.assertEqual(ingredients_from_html(html),
                         "Zucker, Kokosraspeln 24%, Glucosesirup, Kakaopaste.")

    def test_inline_ingredients_and_next_label(self):
        html = "<div><strong>Ingredients:</strong> Flour, milk, cocoa.</div><p>Allergens: milk</p>"
        self.assertEqual(ingredients_from_html(html), "Flour, milk, cocoa.")
        self.assertEqual(ingredients_from_html("<p>Product description: Flour and cocoa.</p>"), "")

    def test_product_105430_takes_only_paragraph_below_ingredients(self):
        expected = ("Sugar, almonds 28%, glucose, pistachios 2%, fructose, honey, "
                    "invert sugar, wafers (potato starch), egg whites, natural vanilla flavoring")
        html = ("<h5>Ingredients:</h5><p>Sugar, almonds 28%, glucose, pistachios 2%, "
                "fructose, honey, invert sugar, wafers (potato starch), egg whites, "
                "natural vanilla flavoring</p><p>Other product text.</p>"
                "<h5>Average nutritional values per 100g/ml:</h5><p>Energy: 474 kcal</p>")
        self.assertEqual(ingredients_from_html(html), expected)
        workbook = load_workbook(BytesIO(make_ingredient_workbook([
            {"code": "105430", "ingredients": ingredients_from_html(html), "status": "Found", "url": ""}
        ]))).active
        self.assertEqual(workbook["B2"].value, expected)

    def test_long_ingredient_paragraph_is_not_cut_at_4000_characters(self):
        expected = ("Sugar, " + "almonds, " * 700).strip()
        text = ingredients_from_html(f"<h5>Ingredients:</h5><p>{expected}</p>")
        self.assertEqual(text, expected)
        sheet = load_workbook(BytesIO(make_ingredient_workbook([
            {"code": "LONG1", "ingredients": text, "status": "Found", "url": ""}
        ]))).active
        self.assertEqual(sheet["B2"].value, expected)

    @patch("ingredients_find.fetch")
    def test_dantesse_requires_exact_reference(self, fake_fetch):
        fake_fetch.return_value = (
            b'<span title="Referenz ist 105430">105430</span><h3>Zutaten:</h3><p>Zucker, Honig.</p>',
            "text/html",
        )
        result = inspect_ingredients("105430", "https://dantesse.com/shop/x/y/{code}.html")
        self.assertEqual(result["ingredients"], "Zucker, Honig.")
        self.assertEqual(result["status"], "Found")
        wrong = inspect_ingredients("105420", "https://dantesse.com/shop/x/y/{code}.html")
        self.assertEqual(wrong["status"], "Code could not be verified")
        self.assertEqual(wrong["ingredients"], "")

    @patch("ingredients_find.fetch")
    def test_other_site_uses_exact_sku_and_finds_ingredients(self, fake_fetch):
        html = (b'<script type="application/ld+json">{"@type":"Product","sku":"ABC9"}</script>'
                b'<h2>Ingredients</h2><p>Water, sugar, salt.</p><h2>Nutrition</h2><p>50 kcal</p>')
        fake_fetch.return_value = (html, "text/html")
        result = inspect_ingredients("ABC9", "https://shop.example/products/{code}")
        self.assertEqual(result["ingredients"], "Water, sugar, salt.")
        self.assertEqual(result["status"], "Found")

    @patch("ingredients_find.inspect_ingredients")
    def test_duplicate_codes_are_searched_once(self, inspect):
        inspect.return_value = {"code": "00031", "ingredients": "Milk.", "status": "Found", "url": ""}
        results = run_ingredient_search(["00031", "00031"], "https://shop.example/products/{code}")
        self.assertEqual([result["ingredients"] for result in results], ["Milk.", "Milk."])
        inspect.assert_called_once()

    def test_excel_keeps_codes_as_text_and_includes_source(self):
        data = make_ingredient_workbook([
            {"code": "00031", "ingredients": "Milk, sugar.", "status": "Found", "url": "https://shop.example/p/31"},
            {"code": "ABC9", "ingredients": "", "status": "Ingredients not found", "url": "https://shop.example/p/ABC9"},
        ])
        sheet = load_workbook(BytesIO(data)).active
        self.assertEqual([sheet.cell(1, i).value for i in range(1, 5)],
                         ["Code", "Ingredients", "Status", "Product URL"])
        self.assertEqual(sheet["A2"].value, "00031")
        self.assertEqual(sheet["A2"].number_format, "@")
        self.assertEqual(sheet["B2"].value, "Milk, sugar.")
        self.assertEqual(sheet["D2"].hyperlink.target, "https://shop.example/p/31")
        self.assertEqual(sheet["C3"].value, "Ingredients not found")

    @patch("ingredients_find.run_ingredient_search")
    def test_reset_clears_results_and_download(self, search):
        started, release = Event(), Event()

        def delayed(codes, url, callback, cancel_event):
            started.set()
            release.wait(2)
            return []

        search.side_effect = delayed
        job = IngredientJob()
        job.start("ABC9", "https://shop.example/products/{code}")
        self.assertTrue(started.wait(2))
        job.reset()
        release.set()
        self.assertEqual(job.snapshot()["total"], 0)
        self.assertIsNone(job.download())


if __name__ == "__main__":
    unittest.main()
