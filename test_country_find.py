import unittest
import base64
from io import BytesIO
from threading import Event
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.styles import PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

from country_find import country_from_html, inspect_country, make_workbook, read_codes, run_country_search, translate_country
from image_find import CountryJob


class CountryFindTests(unittest.TestCase):
    def sample_excel(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Emri", "Kodi"])
        sheet.append(["Produkt A", "00031"])
        sheet.append(["Produkt B", "73310"])
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_reads_header_column_and_preserves_text_codes(self):
        data = self.sample_excel()
        self.assertEqual(read_codes(data), ["00031", "73310"])
        self.assertEqual(read_codes(data, "B"), ["00031", "73310"])
        self.assertEqual(read_codes(data, "Kodi"), ["00031", "73310"])

    def test_country_is_taken_from_product_data_or_label(self):
        structured = '<script type="application/ld+json">{"@type":"Product","sku":"ABC9","countryOfOrigin":{"name":"Italy"}}</script>'
        self.assertEqual(country_from_html(structured), "Italy")
        self.assertEqual(country_from_html('<dl><dt>Country of origin</dt><dd>Portugal</dd></dl>'), "Portugal")
        self.assertEqual(country_from_html('<footer>Address: Netherlands</footer>'), "")
        self.assertEqual(country_from_html('<a href="/shop/suessigkeiten/vital-be">Nougat von Vital (BE)</a>', dantesse=True), "Belgium")
        self.assertEqual(translate_country("Belgium", "DE"), "Belgien")
        self.assertEqual(translate_country("Frankreich", "EN"), "France")

    @patch("country_find.fetch")
    def test_dantesse_uses_exact_code_and_category_country(self, fake_fetch):
        html = (b'<span title="Referenz ist 105430">105430</span>'
                b'<a href="https://dantesse.com/shop/suessigkeiten/vital-be">Vital (BE)</a>')
        fake_fetch.return_value = (html, "text/html")
        result = inspect_country("105430", "https://dantesse.com/shop/x/y/{code}.html")
        self.assertEqual(result["country"], "Belgium")
        self.assertEqual(result["status"], "Found")

    @patch("country_find.inspect_country")
    def test_repeat_search_uses_cache_and_translates_without_refetch(self, inspect):
        inspect.return_value = {"code": "A101", "country": "Belgium", "status": "Found", "url": "https://shop.example/p/A101"}
        first = run_country_search(["A101"], "https://shop.example/p/{code}", language="EN")
        second = run_country_search(["A101"], "https://shop.example/p/{code}", language="DE")
        self.assertEqual(first[0]["country"], "Belgium")
        self.assertEqual(second[0]["country"], "Belgien")
        inspect.assert_called_once()

    @patch("country_find.inspect_country")
    def test_duplicate_codes_are_fetched_once_and_keep_all_rows(self, inspect):
        inspect.return_value = {"code": "B202", "country": "France", "status": "Found", "url": "https://shop.example/p/B202"}
        results = run_country_search(["B202", "B202"], "https://shop.example/p/{code}")
        self.assertEqual([item["country"] for item in results], ["France", "France"])
        inspect.assert_called_once()

    @patch("country_find.fetch")
    def test_generic_page_requires_matching_code(self, fake_fetch):
        fake_fetch.return_value = (b'<h1>Other product</h1><p>Country: Italy</p>', "text/html")
        result = inspect_country("ABC9", "https://shop.example/products/{code}")
        self.assertEqual(result["status"], "Code could not be verified")
        self.assertEqual(result["country"], "")

    @patch("country_find.fetch")
    def test_generic_page_finds_country_for_exact_sku(self, fake_fetch):
        html = (b'<script type="application/ld+json">'
                b'{"@type":"Product","sku":"ABC9","countryOfOrigin":{"name":"Italy"}}'
                b'</script>')
        fake_fetch.return_value = (html, "text/html")
        result = inspect_country("ABC9", "https://shop.example/products/{code}")
        self.assertEqual(result["country"], "Italy")
        self.assertEqual(result["status"], "Found")

    def test_generated_excel_contains_code_country_and_source(self):
        data = make_workbook([{"code": "00031", "country": "France", "status": "Found",
                               "url": "https://shop.example/p/31"}])
        sheet = load_workbook(BytesIO(data)).active
        self.assertEqual([sheet.cell(2, col).value for col in range(1, 5)],
                         ["00031", "France", "Found", "https://shop.example/p/31"])

    def test_output_preserves_input_sheets_and_fills_selected_column(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Artikujt"
        sheet.append(["KOD", "Emri", "Vendi"])
        sheet.append(["105430", "Vital", None])
        sheet["B2"].font = Font(bold=True)
        workbook.create_sheet("Shënime")["A1"] = "Mbaje këtë fletë"
        source = BytesIO()
        workbook.save(source)
        results = [{"code": "105430", "country": "Belgium", "status": "Found", "url": "https://dantesse.com/x"}]
        output = make_workbook(results, source.getvalue(), [2], "Vendi")
        final = load_workbook(BytesIO(output))
        self.assertEqual(final["Artikujt"]["A2"].value, "105430")
        self.assertEqual(final["Artikujt"]["B2"].value, "Vital")
        self.assertTrue(final["Artikujt"]["B2"].font.bold)
        self.assertEqual(final["Artikujt"]["C2"].value, "Belgium")
        self.assertEqual(final["Shënime"]["A1"].value, "Mbaje këtë fletë")

    def test_output_creates_country_column_when_unspecified(self):
        source = self.sample_excel()
        result = [{"code": "00031", "country": "France", "status": "Found", "url": ""},
                  {"code": "73310", "country": "Netherlands", "status": "Found", "url": ""}]
        sheet = load_workbook(BytesIO(make_workbook(result, source, [2, 3]))).active
        self.assertEqual(sheet["C1"].value, "Country")
        self.assertEqual(sheet["C2"].value, "France")
        self.assertEqual(sheet["C3"].value, "Netherlands")

    def test_short_output_name_is_new_header_not_excel_column_letters(self):
        result = [{"code": "00031", "country": "France", "status": "Found", "url": ""}]
        sheet = load_workbook(BytesIO(make_workbook(result, self.sample_excel(), [2], "SHT"))).active
        self.assertEqual(sheet.max_column, 3)
        self.assertEqual(sheet["C1"].value, "SHT")
        self.assertEqual(sheet["C2"].value, "France")

    def test_output_keeps_layout_and_gives_new_column_matching_style(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["KOD", "Emri"])
        sheet.append(["105430", "Vital"])
        sheet.append(["105424", "Tjetër"])
        sheet["B1"].fill = PatternFill("solid", fgColor="204060")
        sheet["B1"].font = Font(bold=True, color="FFFFFF")
        sheet["B2"].fill = PatternFill("solid", fgColor="E5E5E5")
        sheet.column_dimensions["B"].width = 27
        sheet.row_dimensions[2].height = 29
        sheet.freeze_panes = "B2"
        sheet.auto_filter.ref = "A1:B3"
        table = Table(displayName="Products", ref="A1:B3")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        sheet.add_table(table)
        source = BytesIO()
        workbook.save(source)
        results = [{"code": "105430", "country": "Belgium", "status": "Found", "url": ""},
                   {"code": "105424", "country": "", "status": "Nuk u gjet", "url": ""}]
        final = load_workbook(BytesIO(make_workbook(results, source.getvalue(), [2, 3]))).active
        self.assertEqual(final["A2"].value, "105430")
        self.assertEqual(final["B2"].value, "Vital")
        self.assertEqual(final["C2"].value, "Belgium")
        self.assertIsNone(final["C3"].value)
        self.assertEqual(final["C1"].fill.fgColor.rgb, final["B1"].fill.fgColor.rgb)
        self.assertEqual(final["C2"].fill.fgColor.rgb, final["B2"].fill.fgColor.rgb)
        self.assertEqual(final.column_dimensions["C"].width, 27)
        self.assertEqual(final.row_dimensions[2].height, 29)
        self.assertEqual(final.freeze_panes, "B2")
        self.assertEqual(final.auto_filter.ref, "A1:C3")
        self.assertEqual(final.tables["Products"].ref, "A1:C3")

    def test_existing_country_column_keeps_its_width_and_unknown_values(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["KOD", "Vendi"])
        sheet.append(["105430", None])
        sheet.append(["105424", "Për kontroll"])
        sheet.column_dimensions["B"].width = 14
        source = BytesIO()
        workbook.save(source)
        results = [{"code": "105430", "country": "Belgium", "status": "Found", "url": ""},
                   {"code": "105424", "country": "", "status": "Nuk u gjet", "url": ""}]
        final = load_workbook(BytesIO(make_workbook(results, source.getvalue(), [2, 3], "Vendi"))).active
        self.assertEqual(final.column_dimensions["B"].width, 14)
        self.assertEqual(final["B2"].value, "Belgium")
        self.assertEqual(final["B3"].value, "Për kontroll")

    @patch("country_find.run_country_search")
    def test_refresh_reset_clears_country_results_and_ignores_old_worker(self, search):
        started, release = Event(), Event()
        def delayed(codes, url, on_result, cancel_event, language):
            started.set()
            release.wait(3)
            return []
        search.side_effect = delayed
        job = CountryJob()
        job.start(base64.b64encode(self.sample_excel()).decode(), "https://shop.example/", "Kodi")
        self.assertTrue(started.wait(2))
        job.reset()
        self.assertEqual(job.snapshot()["total"], 0)
        release.set()
        self.assertIsNone(job.download())


if __name__ == "__main__":
    unittest.main()
