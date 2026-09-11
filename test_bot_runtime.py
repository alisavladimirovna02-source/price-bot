import io
import csv
import json
import logging
import os
import time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import Document, Update
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest

import bot
import parse_prices
from price_files import MAX_FILE_BYTES
from test_price_files import CATALOG, make_xlsx


class FakeTelegramRequest(BaseRequest):
    """Exercise real Telegram dispatch without contacting Telegram."""
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        identity = {"id": 123456, "is_bot": True, "first_name": "Test", "username": "test_bot"}
        if endpoint == "getMe":
            result = identity
        elif endpoint in ("sendMessage", "editMessageText", "sendDocument"):
            result = {
                "message_id": 100, "date": 1, "from": identity,
                "chat": {"id": params["chat_id"], "type": "private"},
                "text": params.get("text", ""),
            }
        elif endpoint == "answerCallbackQuery":
            result = True
        else:
            raise AssertionError(f"Unexpected API method: {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.user_store.clear()
        self.transport = FakeTelegramRequest()
        builder = ApplicationBuilder().request(self.transport).get_updates_request(self.transport)
        with patch.object(bot, "TOKEN", "123456:TEST_TOKEN"), \
                patch.object(bot, "ApplicationBuilder", return_value=builder):
            self.app = bot.build_application()
        await self.app.initialize()
        self.transport.calls.clear()

    async def asyncTearDown(self):
        await self.app.shutdown()
        bot.user_store.clear()

    async def send(self, text, user_id=None, command=False):
        if user_id is None:
            user_id = bot.ALLOWED_USERS[0]
        message = {
            "message_id": 1, "date": 1,
            "from": {"id": user_id, "is_bot": False, "first_name": "User"},
            "chat": {"id": user_id, "type": "private"}, "text": text,
        }
        if command:
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        update = Update.de_json({"update_id": 1, "message": message}, self.app.bot)
        await self.app.process_update(update)

    async def send_document(self, filename, content=b"", user_id=None, size=None):
        user_id = bot.ALLOWED_USERS[0] if user_id is None else user_id
        message = {
            "message_id": 2, "date": 1,
            "from": {"id": user_id, "is_bot": False, "first_name": "User"},
            "chat": {"id": user_id, "type": "private"},
            "document": {"file_id": "test-file", "file_unique_id": "unique-file",
                         "file_name": filename, "file_size": len(content) if size is None else size},
        }
        update = Update.de_json({"update_id": 2, "message": message}, self.app.bot)
        download = AsyncMock(return_value=bytearray(content))
        with patch.object(Document, "get_file", new_callable=AsyncMock,
                          return_value=SimpleNamespace(download_as_bytearray=download)) as get_file:
            await self.app.process_update(update)
        return get_file, download

    async def test_csv_xlsx_txt_and_messages_share_the_process_button(self):
        await self.send("Original - 500")
        await self.send_document("catalog.csv", CATALOG.encode("utf-8-sig"))
        await self.send_document("price.txt", b"Text file - 300\n")
        await self.send_document("price.xlsx", make_xlsx([["Название", "Цена"], ["Device 512", 100]]))
        entries = bot.user_store[bot.ALLOWED_USERS[0]]
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[0], "Original - 500")
        self.assertEqual(entries[1]["name"], "16 Plus 512 Pink")
        self.assertEqual(entries[1]["price"], 92500)
        self.assertEqual(entries[-1]["price"], 100)
        params = self.transport.calls[-1][1]
        self.assertIn("Файл принят: 1", params["text"])
        self.assertIn("Всего в прайсе: 5", params["text"])
        self.assertEqual(params["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "done")

    async def test_document_to_done_dispatch_writes_and_sends_processed_csv(self):
        await self.send_document("catalog.csv", CATALOG.encode())
        await self.send("Text item - 100")
        user_id = bot.ALLOWED_USERS[0]
        update = Update.de_json({"update_id": 3, "callback_query": {
            "id": "done-query", "chat_instance": "chat", "data": "done",
            "from": {"id": user_id, "is_bot": False, "first_name": "User"},
            "message": {"message_id": 100, "date": 1, "text": "Process",
                        "chat": {"id": user_id, "type": "private"}},
        }}, self.app.bot)
        mapping = parse_prices.parse_mapping(
            "16 Plus 512 Pink = eu-sku\n16 Plus 512 Pink esim = us-sku\nText item = text-sku"
        )
        def run_parser(command, check):
            self.assertTrue(check)
            self.assertEqual(command[1:], ["parse_prices.py", "--input-json", "prices_input.json"])
            parse_prices.main(command[-1])
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.object(bot.subprocess, "run", side_effect=run_parser), \
                        patch.object(parse_prices, "load_mapping_from_github", return_value=mapping), \
                        patch.object(bot, "compare_mvc", return_value=0), \
                        patch("sys.stdout", new_callable=io.StringIO):
                    await self.app.process_update(update)
                with Path("prices_parsed.csv").open(encoding="utf-8", newline="") as output:
                    rows = list(csv.DictReader(output))
                self.assertEqual([row["SKU"] for row in rows], ["eu-sku", "us-sku", "text-sku"])
                self.assertEqual([row["Цена"] for row in rows], ["92500", "82200", "100"])
                self.assertTrue(all(row["Status"] == "OK" for row in rows))
                self.assertEqual(sum(method == "sendDocument" for method, _ in self.transport.calls), 1)
                self.assertFalse(bot.user_store[user_id])
            finally:
                os.chdir(previous)

    async def test_invalid_files_leave_existing_prices_intact(self):
        await self.send("Original - 500")
        for filename, content in (("bad.csv", b"invalid"), ("empty.txt", b""), ("bad.xlsx", b"bad")):
            with self.subTest(filename=filename):
                await self.send_document(filename, content)
                self.assertEqual(bot.user_store[bot.ALLOWED_USERS[0]], ["Original - 500"])
                self.assertIn("Файл не добавлен", self.transport.calls[-1][1]["text"])

    async def test_unsupported_and_oversize_files_are_rejected_before_download(self):
        for filename, size in (("price.pdf", 20), ("price.xlsx", MAX_FILE_BYTES + 1)):
            with self.subTest(filename=filename):
                get_file, download = await self.send_document(filename, size=size)
                get_file.assert_not_awaited()
                download.assert_not_awaited()
                self.assertFalse(bot.user_store)
                self.assertIn("Файл не добавлен", self.transport.calls[-1][1]["text"])

    async def test_unknown_user_cannot_download_or_add_a_file(self):
        with self.assertLogs(bot.logger, level="WARNING"):
            get_file, download = await self.send_document("price.csv", CATALOG.encode(), user_id=987654321)
        get_file.assert_not_awaited()
        download.assert_not_awaited()
        self.assertFalse(bot.user_store)
        self.assertIn("не добавлен в список доступа", self.transport.calls[-1][1]["text"])

    async def test_file_in_wb_mode_does_not_become_a_price_or_stock_request(self):
        user_id = bot.ALLOWED_USERS[0]
        state = SimpleNamespace(chat_id=user_id, phase="input", touched_at=time.monotonic(), articles=[])
        self.app.user_data[user_id][bot.wb_handlers.KEY] = state
        get_file, download = await self.send_document("price.csv", CATALOG.encode())
        get_file.assert_not_awaited()
        self.assertFalse(bot.user_store)
        self.assertEqual(state.articles, [])
        self.assertIn("режим обнуления WB", self.transport.calls[-1][1]["text"])

    async def test_start_is_answered_and_not_added_to_price(self):
        await self.send("/start", command=True)
        self.assertEqual(len(self.transport.calls), 1)
        method, params = self.transport.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Бот на связи", params["text"])
        self.assertFalse(bot.user_store)

    async def test_authorized_prices_still_reply_and_accumulate(self):
        await self.send("17 512 Black eSim Актив - 80200")
        await self.send("17 512 White eSim Актив - 81700")
        self.assertEqual([method for method, _ in self.transport.calls], [
            "sendMessage", "sendMessage",
        ])
        self.assertIn("Добавлено позиций: 2", self.transport.calls[-1][1]["text"])
        self.assertEqual(len(bot.user_store[bot.ALLOWED_USERS[0]]), 2)

    async def test_price_after_start_gets_a_new_acknowledgement_with_process_button(self):
        await self.send("17 512 Black eSim Актив - 80200")
        await self.send("/start", command=True)
        self.transport.calls.clear()
        await self.send("17 512 White eSim Актив - 81700")
        self.assertEqual(len(self.transport.calls), 1)
        method, params = self.transport.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertIn("Добавлено позиций: 2", params["text"])
        self.assertEqual(params["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "done")

    async def test_unknown_user_gets_explanation_without_access(self):
        for text, command in (("/start", True), ("17 512 Black eSim - 80200", False),
                              ("17 512 Black eSim = forbidden-sku", False)):
            with self.subTest(text=text), self.assertLogs(bot.logger, level="WARNING"), \
                    patch.object(bot, "update_mapping_github") as write_mapping:
                await self.send(text, user_id=987654321, command=command)
                write_mapping.assert_not_called()
                self.assertIn("987654321", self.transport.calls[-1][1]["text"])
                self.assertIn("не добавлен", self.transport.calls[-1][1]["text"])
                self.assertNotIn(987654321, bot.user_store)

    async def test_incoming_message_logs_metadata_without_price_text(self):
        with self.assertLogs(bot.logger, level="INFO") as captured:
            await self.send("PRIVATE_PRICE_TEXT - 123456")
        text = "\n".join(captured.output)
        self.assertIn("Получено обновление", text)
        self.assertIn("доступ=True", text)
        self.assertNotIn("PRIVATE_PRICE_TEXT", text)

    async def test_startup_reports_actual_bot_identity(self):
        with self.assertLogs(bot.logger, level="INFO") as captured:
            await self.app.post_init(self.app)
        self.assertIn("Подключение к Telegram установлено: @test_bot", captured.output[0])

    async def test_errors_are_logged(self):
        error = RuntimeError("test handler failure")
        with self.assertLogs(bot.logger, level="ERROR") as captured:
            await self.app.process_error(update=None, error=error)
        self.assertIn("test handler failure", "\n".join(captured.output))


class StartupTests(unittest.TestCase):
    def test_missing_token_is_reported(self):
        with patch.object(bot, "TOKEN", ""), self.assertRaisesRegex(RuntimeError, "TOKEN"):
            bot.build_application()

    def test_startup_failure_has_error_status_and_log(self):
        with patch.object(bot, "configure_logging"), \
                patch.object(bot, "build_application", side_effect=RuntimeError("offline")), \
                self.assertLogs(bot.logger, level="INFO") as captured:
            self.assertEqual(bot.main(), 1)
        self.assertIn("Не удалось запустить бота", "\n".join(captured.output))

    def test_polling_explicitly_requests_messages_and_buttons(self):
        app = Mock()
        with patch.object(bot, "configure_logging"), \
                patch.object(bot, "build_application", return_value=app):
            self.assertEqual(bot.main(), 0)
        app.run_polling.assert_called_once_with(
            allowed_updates=["message", "callback_query"], bootstrap_retries=0,
        )

    def test_tokens_are_redacted_from_messages_and_tracebacks(self):
        error = RuntimeError("https://api.telegram.org/bot123456:SECRET/getMe github-secret")
        record = logging.LogRecord("test", logging.ERROR, "test", 1,
                                   "Request failed: %s", ("123456:SECRET",),
                                   (RuntimeError, error, None))
        with patch.object(bot, "TOKEN", "123456:SECRET"), \
                patch.dict(os.environ, {"GITHUB_TOKEN": "github-secret"}):
            result = bot.RedactingFormatter("%(message)s").format(record)
        self.assertNotIn("123456:SECRET", result)
        self.assertNotIn("github-secret", result)
        self.assertIn("[REDACTED]", result)

    def test_runtime_logging_goes_to_stdout_without_http_info(self):
        root = logging.getLogger()
        old_handlers, old_level = root.handlers[:], root.level
        http_loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
        old_http_levels = [logger.level for logger in http_loggers]
        try:
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                bot.configure_logging()
                bot.logger.info("runtime marker")
                logging.getLogger("httpx").info("HTTP request must be hidden")
                self.assertIn("runtime marker", stdout.getvalue())
                self.assertNotIn("HTTP request must be hidden", stdout.getvalue())
        finally:
            root.handlers = old_handlers
            root.setLevel(old_level)
            for logger, level in zip(http_loggers, old_http_levels):
                logger.setLevel(level)


if __name__ == "__main__":
    unittest.main()
