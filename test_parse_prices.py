import ast
import base64
import csv
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import parse_prices as parser


ROOT = Path(__file__).resolve().parent
RULES = """17 512 Black eSim = iphone17black512eSIM
17 512 White eSim = iphone17white512eSIM
17 512 Black 1Sim+eSim = iphone17black512
17 512 Black 2sim = test-black-2sim
"""


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.mapping = parser.parse_mapping(RULES)

    def test_formatting_differences_match_in_both_directions(self):
        variants = (
            "17 512 Black eSim",
            "  17\t512  BLACK (eSim) ",
            "17\u00a0512 Black eSIM",
        )
        for key in variants:
            for name in variants:
                with self.subTest(key=key, name=name):
                    mapping = parser.parse_mapping(f"{key} = expected-sku")
                    self.assertEqual(
                        parser.match_from_mapping(name, mapping),
                        ("expected-sku", "OK"),
                    )

    def test_active_items_do_not_use_ordinary_rules(self):
        rows, missing = parser.parse_prices([
            "17 512 Black (eSim) Актив - 80200",
            "17 512 White (eSim) Актив - 81700",
        ], self.mapping)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(missing), 2)
        for row in rows:
            self.assertEqual(row[3:], ["", 0, "NOT_FOUND"])
            self.assertIn("Актив", row[0])
        self.assertEqual([row[1] for row in rows], [80200, 81700])

    def test_specific_active_rule_wins_regardless_of_file_order(self):
        active = "17 512 Black (eSim) Актив = test-active-black"
        for content in (RULES + active, active + "\n" + RULES):
            with self.subTest(content=content):
                rows, missing = parser.parse_prices([
                    "17 512 Black (eSim) Актив - 80200",
                    "17 512 Black (eSim) - 85000",
                ], parser.parse_mapping(content))
                self.assertFalse(missing)
                self.assertEqual([row[3] for row in rows], [
                    "test-active-black", "iphone17black512eSIM",
                ])

    def test_no_partial_or_fuzzy_matches(self):
        for name in (
            "17 512 Black eSim Актив", "17 512 Black eSim Неактив",
            "17 512 Black eSim б/у", "iPhone 17 512 Black eSim",
            "Xiaomi 17 512 Black eSim", "17 256 Black eSim",
            "17 512 Blue eSim", "17 Pro 512 Black eSim",
            "17 512 Black", "17 512 Black 1Sim/eSim",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    parser.match_from_mapping(name, self.mapping),
                    ("", "NOT_FOUND"),
                )

    def test_sim_variants_keep_distinct_skus(self):
        rows, missing = parser.parse_prices([
            "17 512 Black (eSim) - 80000",
            "17 512 Black (1Sim+eSim) - 90000",
            "17 512 Black 2sim - 95000",
        ], self.mapping)
        self.assertFalse(missing)
        self.assertEqual([row[3] for row in rows], [
            "iphone17black512eSIM", "iphone17black512", "test-black-2sim",
        ])

    def test_country_inference_without_duplicate_sim(self):
        cases = [
            ("17 512 Black 🇺🇸 - 80000", "iphone17black512eSIM", "US"),
            ("17 512 Black (eSim) 🇺🇸 - 80000", "iphone17black512eSIM", "US"),
            ("17 512 Black 1Sim+eSim 🇺🇸 - 80000", "iphone17black512", "US"),
            ("17 512 Black 🇭🇰 - 80000", "test-black-2sim", "HK"),
            ("17 512 Black 2sim 🇨🇳 - 80000", "test-black-2sim", "CN"),
        ]
        for line, sku, country in cases:
            with self.subTest(line=line):
                rows, missing = parser.parse_prices([line], self.mapping)
                self.assertFalse(missing)
                self.assertEqual(rows[0][2:], [country, sku, 100, "OK"])

    def test_flag_does_not_hide_activation_status(self):
        for name in ("17 512 Black (eSim) Актив", "17 512 Black Актив"):
            with self.subTest(name=name):
                rows, missing = parser.parse_prices(
                    [name + " 🇺🇸 - 80200"], self.mapping
                )
                self.assertEqual(rows[0][3:], ["", 0, "NOT_FOUND"])
                self.assertTrue(missing)

    def test_duplicate_rule_with_same_sku_is_allowed(self):
        mapping = parser.parse_mapping(
            "17 512 Black eSim = exactSKU\n"
            "17 512 BLACK (eSIM) = exactSKU"
        )
        self.assertEqual(parser.match_from_mapping(
            "17 512 Black eSim", mapping
        ), ("exactSKU", "OK"))

    def test_conflicting_rules_do_not_choose_first_or_last_sku(self):
        for second_key in ("17 512 Black eSim", "17 512 BLACK (eSIM)"):
            content = f"{RULES}{second_key} = different-sku\n{RULES}"
            rows, missing = parser.parse_prices([
                "17 512 Black eSim - 80200", "17 512 White eSim - 81700",
            ], parser.parse_mapping(content))
            self.assertEqual(rows[0][3:], ["", 0, "MAPPING_CONFLICT"])
            self.assertEqual(rows[1][3:], ["iphone17white512eSIM", 100, "OK"])
            self.assertEqual(missing, {"17 512 Black eSim"})

    def test_sku_case_is_preserved_and_not_silently_merged(self):
        mapping = parser.parse_mapping(
            "17 512 Black eSim = skuABC\n17 512 Black eSim = skuabc"
        )
        self.assertEqual(parser.match_from_mapping(
            "17 512 Black eSim", mapping
        ), ("", "MAPPING_CONFLICT"))

    def test_empty_rules_cannot_match_everything(self):
        mapping = parser.parse_mapping("= sku\n17 512 Black eSim =\n# comment\n")
        self.assertEqual(parser.match_from_mapping(
            "17 512 Black eSim", mapping
        ), ("", "NOT_FOUND"))

    def test_same_sku_keeps_existing_highest_price_behavior(self):
        rows, missing = parser.parse_prices([
            "17 512 Black eSim - 80200",
            "17 512 Black eSim - 82000",
            "17 512 Black eSim - 81000",
        ], self.mapping)
        self.assertFalse(missing)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 82000)


class IntegrationTests(unittest.TestCase):
    def test_main_uses_github_mapping_and_writes_statuses(self):
        content = RULES + "17 512 White eSim = conflicting-white\n"
        response = Mock()
        response.json.return_value = {
            "content": base64.b64encode(content.encode()).decode()
        }
        # Run the real file workflow in a disposable directory, without network.
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                Path("prices_utf8.txt").write_text(
                    "17 512 Black (eSim) Актив - 80200\n"
                    "17 512 White (eSim) - 81700\n"
                    "17 512 Black (eSim) 🇺🇸 - 85000\n", encoding="utf-8-sig"
                )
                with patch.dict(os.environ, {
                    "GITHUB_TOKEN": "test-token", "GITHUB_REPO": "test/repo"
                }), patch.object(parser.requests, "get", return_value=response), \
                        patch("sys.stdout", new_callable=io.StringIO):
                    parser.main()
                with Path("prices_parsed.csv").open(newline="", encoding="utf-8") as f:
                    records = list(csv.DictReader(f))
                self.assertEqual([r["Status"] for r in records], [
                    "NOT_FOUND", "MAPPING_CONFLICT", "OK",
                ])
                self.assertEqual(records[2]["SKU"], "iphone17black512eSIM")
                self.assertEqual(Path("not_found.txt").read_text().splitlines(), [
                    "17 512 Black eSim Актив", "17 512 White eSim",
                ])
            finally:
                os.chdir(previous)

    def test_existing_price_sample_remains_compatible(self):
        mapping = parser.parse_mapping((ROOT / "mapping.txt").read_text())
        rows, missing = parser.parse_prices(
            (ROOT / "prices_utf8.txt").read_text().splitlines(), mapping
        )
        self.assertEqual(len(rows), 23)
        self.assertFalse(missing)
        by_name = {row[0]: row for row in rows}
        self.assertEqual(by_name["15 512 Green esim"][3], "iphone15green512eSIM")
        self.assertEqual(by_name["15 Pro 128 Blue esim"][3], "iphone15problue128eSIM")
        self.assertEqual(by_name["15 Pro 128 Blue"][3], "iphone15problue128")


class BotFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_parser_failure_does_not_send_stale_csv(self):
        # Load only this handler so the test cannot start Telegram polling.
        module = ast.parse((ROOT / "bot.py").read_text())
        handler = next(node for node in module.body if
                       isinstance(node, ast.AsyncFunctionDef) and
                       node.name == "process_and_reply")
        namespace = {
            "Update": object, "subprocess": subprocess, "sys": sys,
            "os": os, "csv": csv,
        }
        exec(compile(ast.Module(body=[handler], type_ignores=[]),
                     "bot.py", "exec"), namespace)
        message = SimpleNamespace(
            reply_text=AsyncMock(), reply_document=AsyncMock()
        )
        failure = subprocess.CalledProcessError(1, "parse_prices.py")
        with patch.object(subprocess, "run", side_effect=failure), \
                patch("builtins.open") as open_file:
            await namespace["process_and_reply"](SimpleNamespace(message=message))
        open_file.assert_not_called()
        message.reply_document.assert_not_awaited()
        self.assertIn("Ошибка обработки прайса", message.reply_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
