"""Ordered user updates and isolated, persistent price jobs."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from telegram.ext import BaseUpdateProcessor


logger = logging.getLogger("price_bot.jobs")


class UserUpdateProcessor(BaseUpdateProcessor):
    """Different users run concurrently; one user's commands keep their order."""

    def __init__(self):
        super().__init__(64)
        self._users = {}

    async def initialize(self):
        pass

    async def shutdown(self):
        self._users.clear()

    async def do_process_update(self, update, coroutine):
        user = getattr(update, "effective_user", None)
        key = user.id if user else None
        entry = self._users.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                await coroutine
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._users.pop(key, None)


@dataclass(frozen=True)
class PriceJob:
    job_id: str
    owner_id: int
    chat_id: int
    directory: Path


def session_directory(root, owner_id, chat_id):
    return Path(root).resolve() / "users" / str(int(owner_id)) / str(int(chat_id))


def create_job(root, owner_id, chat_id, entries):
    job_id = uuid4().hex
    directory = session_directory(root, owner_id, chat_id) / "jobs" / job_id
    directory.mkdir(parents=True)
    (directory / "prices_input.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8",
    )
    return PriceJob(job_id, owner_id, chat_id, directory)


def publish_job(job):
    """Publish only a completed parser result; interrupted jobs cannot replace it."""
    session = job.directory.parent.parent
    pending = session / f"latest-{job.job_id}.tmp"
    pending.write_text(json.dumps({"job_id": job.job_id}), encoding="utf-8")
    pending.replace(session / "latest.json")


def latest_report_directory(root, owner_id, chat_id):
    session = session_directory(root, owner_id, chat_id)
    try:
        job_id = json.loads((session / "latest.json").read_text(encoding="utf-8"))["job_id"]
        if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
            return None
        directory = session / "jobs" / job_id
        return directory if directory.is_dir() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def run_parser(script, input_json, directory, timeout=180):
    import sys

    command = [sys.executable, str(script)]
    if input_json is not None:
        command += ["--input-json", str(input_json)]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=directory, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        logger.error("Парсер завершился с кодом %s: %s", process.returncode,
                     stderr.decode("utf-8", errors="replace")[-2000:])
        raise RuntimeError(f"Парсер завершился с кодом {process.returncode}")
    logger.info("Парсер завершён: %s", stdout.decode("utf-8", errors="replace").strip())
