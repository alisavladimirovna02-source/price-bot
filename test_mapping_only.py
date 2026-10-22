"""Regression checks for the bot's Registry-free matching and writes."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from telegram import Update
from telegram.ext import ApplicationBuilder

import bot
import mapping_only
import parse_prices
import v2_bridge
from test_bot_runtime import FakeTelegramRequest


class FakeRepository:
    mapping_path = "mapping.txt"

    def __init__(self, content=""):
        self.content = content
        self.writes = 0
        self.fail = False

    def _get_file(self, path, missing_ok=False):
        self.assert_path(path)
        return self.content, "sha"

    def _put_file(self, path, content, message, sha):
        self.assert_path(path)
        if self.fail:
            raise OSError("GitHub unavailable")
        self.content = content
        self.writes += 1

    def assert_path(self, path):
        if path != self.mapping_path:
            raise AssertionError(path)


class MappingOnlyTests(unittest.TestCase):
    def test_new_sku_can_be_saved_without_database(self):
        repo = FakeRepository("Old phone = old-sku\n")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.txt"
            entries = mapping_only.parse_entries("Honor Turbo 8/256 Beige = HONORTurboBeige8/256")
            checks = mapping_only.save_entries(entries, path, repo)
            self.assertEqual([item.status for item in checks], ["ADD"])
            self.assertEqual(repo.writes, 1)
            self.assertIn("HONORTurboBeige8/256", path.read_text())
            rows, missing = parse_prices.parse_prices(
                [{"name": "Honor Turbo 8/256 Beige", "price": 100, "country": ""}],
                parse_prices.parse_mapping(repo.content),
            )
            self.assertEqual(rows[0][3:6], ["HONORTurboBeige8/256", 100, "OK"])
            self.assertFalse(missing)

    def test_conflicting_name_is_not_written(self):
        repo = FakeRepository("Same phone = first-sku\n")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.txt"
            path.write_text("local unchanged\n")
            checks = mapping_only.save_entries([("Same phone", "second-sku")], path, repo)
            self.assertEqual(checks[0].status, "CONFLICT")
            self.assertEqual(repo.writes, 0)
            self.assertEqual(path.read_text(), "local unchanged\n")

    def test_remote_failure_does_not_update_local_mapping(self):
        repo = FakeRepository()
        repo.fail = True
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.txt"
            path.write_text("local unchanged\n")
            with self.assertRaises(OSError):
                mapping_only.save_entries([("New phone", "new-sku")], path, repo)
            self.assertEqual(path.read_text(), "local unchanged\n")

    def test_price_never_uses_similar_name_or_conflicting_alias(self):
        mapping = parse_prices.parse_mapping(
            "Honor Turbo 8/256 Beige = one\nHonor Turbo 8/256 Beige = two\n"
            "Honor Turbo 8/256 Black = black"
        )
        self.assertEqual(parse_prices.match_from_mapping("Honor Turbo 8/256 Beige", mapping), ("", "MAPPING_CONFLICT"))
        self.assertEqual(parse_prices.match_from_mapping("Honor Turbo 8/256 Green", mapping), ("", "NOT_FOUND"))
        self.assertEqual(parse_prices.match_from_mapping("Honor Turbo 8/256 Black", mapping), ("black", "OK"))

    def test_registry_controls_are_absent(self):
        self.assertEqual(v2_bridge.MENU_BUTTONS, (
            "ℹ️ Инструкция", "🗂 Проверить mapping", "➕ Пополнить mapping"
        ))
        keyboard = bot.get_main_keyboard().keyboard
        self.assertFalse(any("SKU" in button.text or "Registry" in button.text for row in keyboard for button in row))


class MappingTelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.user_store.clear()
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "mapping.txt"
        self.repo = FakeRepository()
        self.transport = FakeTelegramRequest()
        builder = ApplicationBuilder().request(self.transport).get_updates_request(self.transport)
        with patch.object(bot, "TOKEN", "123456:TEST_TOKEN"), \
                patch.object(bot, "ApplicationBuilder", return_value=builder):
            self.app = bot.build_application()
        v2_bridge.install(self.app, bot.check_access)
        await self.app.initialize()
        self.transport.calls.clear()

    async def asyncTearDown(self):
        await self.app.shutdown()
        bot.user_store.clear()

    async def send(self, text):
        user = bot.ALLOWED_USERS[0]
        update = Update.de_json({"update_id": 1, "message": {
            "message_id": 1, "date": 1, "text": text,
            "from": {"id": user, "is_bot": False, "first_name": "User"},
            "chat": {"id": user, "type": "private"},
        }}, self.app.bot)
        await self.app.process_update(update)

    async def confirm(self, token):
        user = bot.ALLOWED_USERS[0]
        update = Update.de_json({"update_id": 2, "callback_query": {
            "id": "confirm", "chat_instance": "chat", "data": f"v2bulkmap:{token}:save",
            "from": {"id": user, "is_bot": False, "first_name": "User"},
            "message": {"message_id": 100, "date": 1, "text": "Preview",
                        "chat": {"id": user, "type": "private"}},
        }}, self.app.bot)
        await self.app.process_update(update)

    async def test_preview_then_confirm_adds_unknown_sku(self):
        with patch.object(v2_bridge, "github_repository", return_value=self.repo), \
                patch.object(mapping_only, "github_repository", return_value=self.repo), \
                patch.object(v2_bridge, "MAPPING_PATH", self.path):
            await self.send(v2_bridge.BUTTON_ADD_MAPPING)
            await self.send("New device = new-sku")
            self.assertEqual(self.repo.writes, 0)
            self.assertIn("Новых: 1", self.transport.calls[-1][1]["text"])
            token = self.app.user_data[bot.ALLOWED_USERS[0]]["pending_mappings"][0]
            await self.confirm(token)
            self.assertEqual(self.repo.writes, 1)
            self.assertIn("New device = new-sku", self.path.read_text())
            self.assertIn("Добавлено: 1", self.transport.calls[-1][1]["text"])

    async def test_old_registry_button_is_disabled(self):
        await self.send("➕ Добавить SKU")
        self.assertIn("Registry отключён", self.transport.calls[-1][1]["text"])
        self.assertFalse(bot.user_store)


if __name__ == "__main__":
    unittest.main()
