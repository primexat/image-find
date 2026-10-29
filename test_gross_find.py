"""Checks for gross weight extraction and Excel output."""

import unittest
import base64
from io import BytesIO
from time import monotonic, sleep
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from gross_find import (add_calculations, gross_from_html, inspect_gross, make_gross_workbook,
                        multiply_weight, parse_prefixes, run_gross_search)
from image_find import GrossJob


class GrossFindTests(unittest.TestCase):
    def test_extracts_labeled_weight_only(self):
        html = "<p>Net weight: 500 g</p><dl><dt>Gross weight</dt><dd>0.75 kg</dd></dl>"
        self.assertEqual(gross_from_html(html), "0.75 kg")
        self.assertEqual(gross_from_html("<p>Net weight: 500 g</p>"), "")
        self.assertEqual(gross_from_html("<div>Bruttogewicht: 1,2 kg</div>"), "1,2 kg")

    def test_uses_product_title_weight_when_no_gross_label_exists(self):
        html = ('<div class="single-product"><h3>VITAL - Weicher Nougat - Mandeln 150g</h3>'
                '<span title="Referenz ist 105430">105430</span>'
                '<h3>Durchschnittliche Nährwerte pro 100g/ml:</h3></div>')
        self.assertEqual(gross_from_html(html), "150g")

    @patch("gross_find.fetch")
    def test_verifies_product_code_before_returning_weight(self, fake_fetch):
        fake_fetch.return_value = (
            b'<script type="application/ld+json">{"@type":"Product","sku":"ABC9"}</script>'
            b'<div>Gross weight: 750 g</div>', "text/html"
        )
        found = inspect_gross("ABC9", "https://shop.example/products/{code}")
        self.assertEqual((found["gross"], found["status"]), ("750 g", "Found"))
        wrong = inspect_gross("ABC8", "https://shop.example/products/{code}")
        self.assertEqual((wrong["gross"], wrong["status"]), ("", "Code could not be verified"))

    @patch("gross_find.inspect_gross")
    def test_duplicate_codes_are_searched_once(self, inspect):
        inspect.return_value = {"code": "00031", "gross": "1 kg", "status": "Found", "url": ""}
        results = run_gross_search(["00031", "00031"], "https://shop.example/products/{code}")
        self.assertEqual([result["gross"] for result in results], ["1 kg", "1 kg"])
        inspect.assert_called_once()

    def test_workbook_matches_code_prefix_and_total_layout(self):
        data = make_gross_workbook([{"code": "00031", "gross": "1,2 kg", "status": "Found",
                                    "url": "https://shop.example/p/31"}])
        sheet = load_workbook(BytesIO(data)).active
        self.assertEqual([sheet.cell(1, i).value for i in range(1, 5)],
                         ["Code", "Gross weight", None, "TOTALI"])
        self.assertEqual(sheet["A2"].value, "00031")
        self.assertEqual(sheet["A2"].number_format, "@")
        self.assertEqual(sheet["B2"].value, "1,2 kg")
        self.assertEqual([sheet.cell(i, 1).value for i in range(3, 7)],
                         ["01-00031", "03-00031", "06-00031", "12-00031"])
        self.assertEqual([sheet.cell(i, 2).value for i in range(3, 7)], ["1,2 kg"] * 4)
        self.assertEqual([sheet.cell(i, 3).value for i in range(3, 7)],
                         ["(1 x 1,2 kg)", "(3 x 1,2 kg)", "(6 x 1,2 kg)", "(12 x 1,2 kg)"])
        self.assertEqual([sheet.cell(i, 4).value for i in range(3, 7)],
                         ["1,2 kg", "3,6 kg", "7,2 kg", "14,4 kg"])
        self.assertEqual(sheet["A3"].number_format, "@")
        self.assertEqual(sheet["A2"].hyperlink.target, "https://shop.example/p/31")
        self.assertEqual(sheet.auto_filter.ref, "A1:D6")

    def test_each_code_starts_its_own_five_row_group(self):
        results = [
            {"code": "00031", "gross": "150g", "status": "Found", "url": ""},
            {"code": "105430", "gross": "", "status": "Gross weight not found", "url": ""},
        ]
        sheet = load_workbook(BytesIO(make_gross_workbook(results))).active
        self.assertEqual(sheet["A7"].value, "105430")
        self.assertEqual([sheet.cell(i, 1).value for i in range(8, 12)],
                         ["01-105430", "03-105430", "06-105430", "12-105430"])
        self.assertTrue(all(sheet.cell(i, 4).value is None for i in range(8, 12)))

    def test_prefix_calculations_and_validation(self):
        self.assertEqual(parse_prefixes(["01", "03", "06", "12"]), ["01", "03", "06", "12"])
        self.assertEqual(parse_prefixes(["01", "03", "06", ""]), ["01", "03", "06"])
        self.assertEqual(parse_prefixes(["", "", "", ""]), [])
        self.assertEqual(multiply_weight("150g", "12"), "1800g")
        self.assertEqual(add_calculations({"gross": "150g"}, ["01", "03", "06", "12"])["calculated"],
                         ["150g", "450g", "900g", "1800g"])
        self.assertEqual(multiply_weight("", "03"), "")
        with self.assertRaises(ValueError):
            parse_prefixes(["01", "01", "06", "12"])

    def test_blank_prefixes_are_omitted_from_excel(self):
        result = {"code": "105430", "gross": "150g", "status": "Found", "url": ""}
        sheet = load_workbook(BytesIO(make_gross_workbook([result], ["01", "", "06", ""]))).active
        self.assertEqual(sheet.max_row, 4)
        self.assertEqual([sheet.cell(i, 1).value for i in range(2, 5)],
                         ["105430", "01-105430", "06-105430"])
        self.assertEqual([sheet.cell(i, 4).value for i in range(3, 5)], ["150g", "900g"])
        base_only = load_workbook(BytesIO(make_gross_workbook([result], ["", "", "", ""]))).active
        self.assertEqual(base_only.max_row, 2)

    @patch("gross_find.run_gross_search")
    def test_uploads_excel_codes_without_clipboard(self, search):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Code"])
        sheet.append(["00031"])
        sheet.append(["105430"])
        data = BytesIO()
        workbook.save(data)

        def run(codes, _url, callback, _cancel_event):
            results = [{"code": code, "gross": "150g", "status": "Found", "url": ""} for code in codes]
            for index, result in enumerate(results):
                callback(index, result)
            return results

        search.side_effect = run
        job = GrossJob()
        job.start("", "https://shop.example/products/{code}", base64.b64encode(data.getvalue()).decode(),
                  prefixes=["01", "03", "06", "12"])
        deadline = monotonic() + 2
        while job.snapshot()["running"] and monotonic() < deadline:
            sleep(0.01)
        self.assertEqual([item["code"] for item in job.snapshot()["results"]], ["00031", "105430"])
        self.assertEqual(job.snapshot()["results"][0]["calculated"],
                         ["150g", "450g", "900g", "1800g"])
        self.assertIsNotNone(job.download())


if __name__ == "__main__":
    unittest.main()
