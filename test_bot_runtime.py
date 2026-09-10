import io
import json
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from telegram import Update
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest

import bot


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
        elif endpoint in ("sendMessage", "editMessageText"):
            result = {
                "message_id": 100, "date": 1, "from": identity,
                "chat": {"id": params["chat_id"], "type": "private"},
                "text": params["text"],
            }
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
