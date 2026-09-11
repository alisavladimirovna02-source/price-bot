import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from openpyxl import Workbook

import bot
import parse_prices
import price_files
from price_files import PriceFileError, read_price_file


CATALOG_HEADER = ("id товара;Наименование бренда;Наименование категории товара;"
                  "Наименование подкатегории товара;Наименование товара;Страна товара;Цена\n")
CATALOG = (CATALOG_HEADER
           + '=\"P-52291869\";Apple;iPhone;iPhone 16 Plus;16 Plus 512 Pink;EU;92500\n'
           + '=\"P-82690536\";Apple;iPhone;iPhone 16 Plus;16 Plus 512 Pink;US;82200\n')


def make_xlsx(rows, second_sheet=None):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    if second_sheet is not None:
        sheet = book.create_sheet()
        for row in second_sheet:
            sheet.append(row)
    content = io.BytesIO()
    book.save(content)
    book.close()
    return content.getvalue()


class FileReaderTests(unittest.TestCase):
    def test_supplied_catalog_schema_ignores_ids_and_categories(self):
        entries = read_price_file("catalog.CSV", CATALOG.encode("utf-8-sig"))
        self.assertEqual(entries, [
            {"name": "16 Plus 512 Pink", "price": 92500, "country": "EU"},
            {"name": "16 Plus 512 Pink", "price": 82200, "country": "US"},
        ])

    def test_csv_delimiters_and_encodings(self):
        for delimiter in (";", ",", "\t"):
            for encoding in ("utf-8", "utf-8-sig", "cp1251", "utf-16"):
                with self.subTest(delimiter=delimiter, encoding=encoding):
                    output = io.StringIO(newline="")
                    writer = csv.writer(output, delimiter=delimiter)
                    writer.writerows([['Название', 'Цена', 'Страна'],
                                      ['Товар "XL", версия; 2048', '1 990,50', 'ru']])
                    self.assertEqual(read_price_file("price.csv", output.getvalue().encode(encoding)), [
                        {"name": 'Товар "XL", версия; 2048', "price": 1990.5, "country": "RU"},
                    ])

    def test_excel_separator_hint_blank_rows_reordered_headers(self):
        entries = read_price_file("price.csv", (
            "sep=;\n\nЦЕНА; Страна товара ; Наименование товара \n"
            "100;EU;Device 512\n;;\n"
        ).encode())
        self.assertEqual(entries, [{"name": "Device 512", "price": 100, "country": "EU"}])

    def test_country_is_optional_and_multiline_names_are_one_entry(self):
        self.assertEqual(read_price_file("price.csv", b'name,price\n"Device\n512",100\n'), [
            {"name": "Device 512", "price": 100, "country": ""},
        ])

    def test_zero_and_decimal_prices_are_not_extracted_from_name(self):
        entries = read_price_file("price.csv", "Название;Цена\nDevice 2026 512;0\nDevice 2026 256;99.95\n".encode())
        rows, _ = parse_prices.parse_prices(entries, {})
        self.assertEqual([row[:2] for row in rows], [
            ["Device 2026 512", 0], ["Device 2026 256", 99.95],
        ])

    def test_invalid_prices_and_partial_rows_reject_entire_file(self):
        for value in ("", "по запросу", "-100", "NaN", "inf", "1e5", "100/200", "1,234", "=2+2"):
            with self.subTest(value=value), self.assertRaisesRegex(PriceFileError, "Строка 3"):
                read_price_file("price.csv", f"Название;Цена\nValid;100\nInvalid;{value}\n".encode())
        with self.assertRaisesRegex(PriceFileError, "название"):
            read_price_file("price.csv", "Название;Цена\n;100\n".encode())

    def test_missing_or_ambiguous_headers(self):
        for text in ("Артикул;Стоимость\nfoo;100", "Название;Цена;Price\nfoo;100;200"):
            with self.subTest(text=text), self.assertRaises(PriceFileError):
                read_price_file("price.csv", text.encode())

    def test_unquoted_comma_price_is_not_silently_truncated(self):
        with self.assertRaisesRegex(PriceFileError, "Строка 2"):
            read_price_file("price.csv", b"name,price\nDevice,19,990\n")

    def test_empty_corrupt_and_unsupported_files(self):
        for filename, content in (("price.csv", b""), ("price.csv", b"name,price\n"),
                                  ("price.csv", b'name,price\n"broken,123'),
                                  ("price.xlsx", b"broken zip"), ("price.txt", b"\x00\x01"),
                                  ("price.txt", b" \n"), ("price.pdf", b"pdf"),
                                  ("price.xls", b"xls"), ("", b"no filename")):
            with self.subTest(filename=filename, content=content), self.assertRaises(PriceFileError):
                read_price_file(filename, content)

    def test_txt_preserves_text_for_existing_parser(self):
        text = "17 512 Black eSim - 80000\r\n\r\n  17 256 Белый - 70000  \n"
        for encoding in ("utf-8-sig", "cp1251", "utf-16"):
            with self.subTest(encoding=encoding):
                self.assertEqual(read_price_file("price.txt", text.encode(encoding)), [
                    "17 512 Black eSim - 80000", "17 256 Белый - 70000",
                ])

    def test_file_and_row_limits_are_enforced(self):
        with patch.object(price_files, "MAX_FILE_BYTES", 10), self.assertRaisesRegex(PriceFileError, "размер"):
            read_price_file("price.csv", b"x" * 11)
        with patch.object(price_files, "MAX_ROWS", 1), self.assertRaisesRegex(PriceFileError, "строк"):
            read_price_file("price.csv", b"name,price\nA,1\nB,2\n")

    def test_xlsx_reads_numeric_prices_and_ignores_extra_id_formula(self):
        content = make_xlsx([
            ["id товара", "Наименование товара", "Цена", "Страна товара"],
            ['="P-12345"', "Device 2048", 1990.5, "US"],
            [None, "Device 512", 100, "EU"],
        ])
        self.assertEqual(read_price_file("price.xlsx", content), [
            {"name": "Device 2048", "price": 1990.5, "country": "US"},
            {"name": "Device 512", "price": 100, "country": "EU"},
        ])

    def test_xlsx_formula_price_is_rejected(self):
        with self.assertRaisesRegex(PriceFileError, "формулы"):
            read_price_file("price.xlsx", make_xlsx([["Название", "Цена"], ["Device", "=100+1"]]))

    def test_xlsx_error_reports_actual_row_after_blank_lines(self):
        with self.assertRaisesRegex(PriceFileError, "Строка 4"):
            read_price_file("price.xlsx", make_xlsx([[], [], ["Название", "Цена"], ["Device", "bad"]]))

    def test_xlsx_multiple_populated_sheets_are_not_silently_discarded(self):
        rows = [["Название", "Цена"], ["Device", 100]]
        with self.assertRaisesRegex(PriceFileError, "несколько заполненных листов"):
            read_price_file("price.xlsx", make_xlsx(rows, rows))
        self.assertEqual(len(read_price_file("price.xlsx", make_xlsx(rows, []))), 1)

    def test_table_and_text_share_matching_and_deduplication_rules(self):
        entries = read_price_file("catalog.csv", CATALOG.encode())
        entries += ["16 Plus 512 Pink 🇺🇸 - 83000"]
        mapping = parse_prices.parse_mapping("16 Plus 512 Pink = eu-sku\n16 Plus 512 Pink esim = us-sku")
        rows, missing = parse_prices.parse_prices(entries, mapping)
        self.assertFalse(missing)
        self.assertEqual(rows, [
            ["16 Plus 512 Pink", 92500, "EU", "eu-sku", 100, "OK"],
            ["16 Plus 512 Pink esim", 83000, "US", "us-sku", 100, "OK"],
        ])

    def test_structured_names_keep_activation_and_explicit_sim(self):
        entries = read_price_file("price.csv", (
            "Название;Цена;Страна\n17 512 Black eSim Актив;100;US\n"
            "17 512 Black 1Sim+eSim;200;US\n17 512 Black;300;CN\n"
        ).encode())
        mapping = parse_prices.parse_mapping(
            "17 512 Black eSim = esim-sku\n17 512 Black 1Sim+eSim = eu-sku\n"
            "17 512 Black 2sim = cn-sku"
        )
        rows, missing = parse_prices.parse_prices(entries, mapping)
        self.assertEqual([row[-1] for row in rows], ["NOT_FOUND", "OK", "OK"])
        self.assertEqual(missing, {"17 512 Black eSim Актив"})

    def test_main_processes_mixed_json_queue_without_network(self):
        entries = read_price_file("catalog.csv", CATALOG.encode()) + ["Unknown - 50"]
        mapping = parse_prices.parse_mapping("16 Plus 512 Pink = eu-sku")
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                Path("prices_input.json").write_text(json.dumps(entries), encoding="utf-8")
                with patch.object(parse_prices, "load_mapping_from_github", return_value=mapping), \
                        patch("sys.stdout", new_callable=io.StringIO):
                    parse_prices.main("prices_input.json")
                with Path("prices_parsed.csv").open(encoding="utf-8", newline="") as output:
                    rows = list(csv.DictReader(output))
                self.assertEqual(len(rows), 3)
                by_name = {row["Название"]: row for row in rows}
                self.assertEqual(by_name["16 Plus 512 Pink"]["SKU"], "eu-sku")
                self.assertEqual(by_name["16 Plus 512 Pink"]["Цена"], "92500")
                self.assertEqual(by_name["16 Plus 512 Pink esim"]["Страна"], "US")
                self.assertIn("Unknown", Path("not_found.txt").read_text())
            finally:
                os.chdir(previous)


class QueueProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_done_passes_all_inputs_and_retains_queue_on_failure(self):
        for success in (False, True):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as directory:
                previous = Path.cwd()
                try:
                    os.chdir(directory)
                    entries = ["Text - 20", {"name": "Device 512", "price": 100, "country": "RU"}]
                    bot.user_store[1] = entries
                    message = SimpleNamespace(chat=SimpleNamespace(id=1), edit_text=AsyncMock(), reply_text=AsyncMock())
                    update = SimpleNamespace(callback_query=SimpleNamespace(message=message, answer=AsyncMock()))
                    context = SimpleNamespace(user_data={"data": entries})
                    async def process(received_update, input_json):
                        self.assertIs(received_update.message, message)
                        self.assertEqual(json.loads(Path(input_json).read_text()), entries)
                        return success
                    with patch.object(bot, "check_access", AsyncMock(return_value=True)), \
                            patch.object(bot, "process_and_reply", side_effect=process) as processing:
                        await bot.done_button(update, context)
                    processing.assert_awaited_once()
                    self.assertEqual(bot.user_store[1], [] if success else entries)
                    if not success:
                        self.assertIn("Прайс сохранён", message.reply_text.call_args.args[0])
                finally:
                    bot.user_store.clear()
                    os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
