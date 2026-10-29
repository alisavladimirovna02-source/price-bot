"""Concurrency regression checks using real Telegram dispatch and subprocesses."""

import asyncio
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from telegram import Update
from telegram.ext import ApplicationBuilder

import bot
from price_jobs import UserUpdateProcessor, create_job, latest_report_directory, publish_job, run_parser
from test_bot_runtime import FakeTelegramRequest


def write_price_output(directory, entries):
    directory = Path(directory)
    names = [item if isinstance(item, str) else item["name"] for item in entries]
    with (directory / "prices_parsed.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Название", "Цена", "Страна", "SKU", "Score", "Status"])
        writer.writerows([name, 100, "", "", 0, "NOT_FOUND"] for name in names)
    (directory / "not_found.txt").write_text("\n".join(names), encoding="utf-8")


class RecordingTelegramRequest(FakeTelegramRequest):
    def __init__(self):
        super().__init__()
        self.documents = []

    async def do_request(self, url, method, request_data=None, **kwargs):
        if url.endswith("/sendDocument"):
            self.documents.append((request_data.parameters["chat_id"],
                                   request_data.multipart_data["document"][1]))
        return await super().do_request(url, method, request_data, **kwargs)


class ConcurrentPriceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {"REPORTS_DIR": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.transport = RecordingTelegramRequest()
        builder = ApplicationBuilder().request(self.transport).get_updates_request(self.transport)
        with patch.object(bot, "TOKEN", "123456:TEST_TOKEN"), \
                patch.object(bot, "ApplicationBuilder", return_value=builder):
            self.app = bot.build_application()
        await self.app.initialize()
        await self.app.start()
        self.users = bot.ALLOWED_USERS[:2]
        self.serial = 0
        self.events = {user: asyncio.Event() for user in self.users}
        self.release = asyncio.Event()
        bot.user_store.clear()
        self.transport.calls.clear()

    async def asyncTearDown(self):
        self.release.set()
        await self.app.stop()
        await self.app.shutdown()
        bot.user_store.clear()

    def update(self, user, text=None, callback=False):
        self.serial += 1
        identity = {"id": user, "is_bot": False, "first_name": "Test"}
        message = {"message_id": self.serial, "date": 1,
                   "chat": {"id": user, "type": "private"}, "text": text or "Process"}
        if callback:
            data = {"callback_query": {"id": str(self.serial), "chat_instance": "chat",
                                       "data": "done", "from": identity, "message": message}}
        else:
            message["from"] = identity
            if text == "/start":
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": 6}]
            data = {"message": message}
        return Update.de_json({"update_id": self.serial, **data}, self.app.bot)

    async def dispatch(self, update):
        # Exercise the production per-user processor as well as its handlers.
        await self.app.update_processor.process_update(update, self.app.process_update(update))

    async def parser(self, script, input_json, directory):
        owner = int(Path(directory).parents[2].name)
        self.events[owner].set()
        await self.release.wait()
        write_price_output(directory, json.loads(Path(input_json).read_text()))

    async def wait_finished(self):
        async with asyncio.timeout(3):
            while any(self.app.user_data[user].get("active_price_job") for user in self.users):
                await asyncio.sleep(0.001)

    async def test_two_prices_overlap_and_start_stays_responsive(self):
        first, second = self.users
        with patch.object(bot, "run_parser", side_effect=self.parser), \
                patch.object(bot, "compare_mvc", return_value=0):
            await self.dispatch(self.update(first, "Only first user"))
            await self.dispatch(self.update(second, "Only second user"))
            await self.dispatch(self.update(first, callback=True))
            await self.dispatch(self.update(second, callback=True))
            async with asyncio.timeout(1):
                await asyncio.gather(*(event.wait() for event in self.events.values()))
                await self.dispatch(self.update(second, "/start"))
            self.assertTrue(any(method == "sendMessage" and params["chat_id"] == second
                                and "Бот на связи" in params.get("text", "")
                                for method, params in self.transport.calls))
            # New arrivals must survive the completion of the previous snapshot.
            await self.dispatch(self.update(first, "Next price"))
            self.release.set()
            await self.wait_finished()
        self.assertEqual(bot.user_store[first], ["Next price"])
        first_report = latest_report_directory(self.root, first, first)
        second_report = latest_report_directory(self.root, second, second)
        self.assertNotEqual(first_report, second_report)
        self.assertEqual((first_report / "not_found.txt").read_text(), "Only first user")
        self.assertEqual((second_report / "not_found.txt").read_text(), "Only second user")
        for chat, content in self.transport.documents:
            text = content.decode("utf-8")
            self.assertIn("Only first user" if chat == first else "Only second user", text)
            self.assertNotIn("Only second user" if chat == first else "Only first user", text)
        for user, expected in ((first, "Only first user"), (second, "Only second user")):
            await self.dispatch(self.update(user, "📦 Не найдено"))
            self.assertIn(expected, self.transport.calls[-1][1]["text"])

    async def test_repeated_button_cannot_start_same_job_twice(self):
        user = self.users[0]
        with patch.object(bot, "run_parser", side_effect=self.parser) as parser, \
                patch.object(bot, "compare_mvc", return_value=0):
            await self.dispatch(self.update(user, "Original"))
            await self.dispatch(self.update(user, callback=True))
            await self.events[user].wait()
            await self.dispatch(self.update(user, "Next"))
            await self.dispatch(self.update(user, callback=True))
            self.assertEqual(parser.await_count, 1)
            self.assertEqual(bot.user_store[user], ["Next"])
            self.release.set()
            await self.wait_finished()

    async def test_failed_job_keeps_new_entries_and_other_users_finish(self):
        first, second = self.users
        async def parser(script, input_json, directory):
            await self.parser(script, input_json, directory)
            if int(Path(directory).parents[2].name) == first:
                raise RuntimeError("test failure")
        with patch.object(bot, "run_parser", side_effect=parser), \
                patch.object(bot, "compare_mvc", return_value=0), \
                self.assertLogs(bot.logger, level="ERROR"):
            for user in self.users:
                await self.dispatch(self.update(user, "Original"))
                await self.dispatch(self.update(user, callback=True))
            await self.events[first].wait()
            await self.dispatch(self.update(first, "New arrival"))
            self.release.set()
            await self.wait_finished()
        self.assertEqual(bot.user_store[first], ["Original", "New arrival"])
        self.assertIsNone(latest_report_directory(self.root, first, first))
        self.assertIsNotNone(latest_report_directory(self.root, second, second))

    async def test_slow_mvc_does_not_block_messages(self):
        gate = threading.Event()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        def slow_mvc(directory):
            loop.call_soon_threadsafe(entered.set)
            gate.wait(2)
            return 0
        self.release.set()
        first, second = self.users
        try:
            with patch.object(bot, "run_parser", side_effect=self.parser), \
                    patch.object(bot, "compare_mvc", side_effect=slow_mvc):
                await self.dispatch(self.update(first, "Original"))
                await self.dispatch(self.update(first, callback=True))
                await entered.wait()
                async with asyncio.timeout(0.5):
                    await self.dispatch(self.update(second, "/start"))
                gate.set()
                await self.wait_finished()
        finally:
            gate.set()


class JobInfrastructureTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_is_ordered_without_blocking_other_users(self):
        processor = UserUpdateProcessor()
        gate = asyncio.Event()
        started = asyncio.Event()
        order = []
        async def slow():
            order.append("first started")
            started.set()
            await gate.wait()
            order.append("first finished")
        async def fast(label):
            order.append(label)
        one = SimpleNamespace(effective_user=SimpleNamespace(id=1))
        two = SimpleNamespace(effective_user=SimpleNamespace(id=2))
        task = asyncio.create_task(processor.process_update(one, slow()))
        await started.wait()
        queued = asyncio.create_task(processor.process_update(one, fast("same user")))
        await processor.process_update(two, fast("other user"))
        self.assertEqual(order, ["first started", "other user"])
        gate.set()
        await asyncio.gather(task, queued)
        self.assertEqual(order[-2:], ["first finished", "same user"])
        self.assertFalse(processor._users)

    async def test_parser_subprocesses_have_separate_working_directories_and_timeout(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "parser.py"
            script.write_text(
                "import json,time,sys\nfrom pathlib import Path\n"
                "p=Path(sys.argv[2]); data=json.loads(p.read_text())\n"
                "time.sleep(data['delay'])\n"
                "Path('result.txt').write_text(data['name'])\n", encoding="utf-8",
            )
            first = create_job(root, 1, 1, {"delay": 0.05, "name": "first"})
            second = create_job(root, 2, 2, {"delay": 0.05, "name": "second"})
            await asyncio.gather(*(run_parser(script, job.directory / "prices_input.json", job.directory)
                                   for job in (first, second)))
            self.assertEqual((first.directory / "result.txt").read_text(), "first")
            self.assertEqual((second.directory / "result.txt").read_text(), "second")
            publish_job(first)
            self.assertEqual(latest_report_directory(root, 1, 1), first.directory)
            stalled = create_job(root, 1, 1, {"delay": 10, "name": "stalled"})
            with self.assertRaises(TimeoutError):
                await run_parser(script, stalled.directory / "prices_input.json", stalled.directory, timeout=0.05)
            self.assertEqual(latest_report_directory(root, 1, 1), first.directory)
            self.assertFalse((stalled.directory / "result.txt").exists())


if __name__ == "__main__":
    unittest.main()
