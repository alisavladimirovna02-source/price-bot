from __future__ import annotations

import base64
import csv
import io
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import HistoryEntry, MappingEntry
from .normalization import normalized_key


HISTORY_COLUMNS = [
    "date",
    "telegram_user_id",
    "action",
    "source_name",
    "sku",
    "catalog_name",
    "status",
    "conflicts",
]


class MappingRepository(Protocol):
    def list_entries(self) -> list[MappingEntry]: ...

    def append_mapping(self, name: str, sku: str) -> bool: ...

    def append_history(self, entry: HistoryEntry) -> None: ...

    def write_text(self, path: str, content: str, message: str) -> None: ...


def parse_mapping_text(content: str) -> list[MappingEntry]:
    entries: list[MappingEntry] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, sku = stripped.rsplit("=", maxsplit=1)
        if name.strip() and sku.strip():
            entries.append(MappingEntry(name=name.strip(), sku=sku.strip(), line_number=line_number))
    return entries


class LocalMappingRepository:
    """File-based backend used in development and in non-GitHub deployments."""

    def __init__(self, mapping_path: str | Path, history_path: str | Path) -> None:
        self.mapping_path = Path(mapping_path)
        self.history_path = Path(history_path)

    def list_entries(self) -> list[MappingEntry]:
        if not self.mapping_path.exists():
            return []
        return parse_mapping_text(self.mapping_path.read_text(encoding="utf-8"))

    def append_mapping(self, name: str, sku: str) -> bool:
        existing = self.list_entries()
        name_key, sku_key = normalized_key(name), sku.casefold()
        if any(normalized_key(item.name) == name_key and item.sku.casefold() == sku_key for item in existing):
            return False
        self.mapping_path.parent.mkdir(parents=True, exist_ok=True)
        with self.mapping_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{name.strip()} = {sku.strip()}\n")
        return True

    def append_history(self, entry: HistoryEntry) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.history_path.exists() or self.history_path.stat().st_size == 0
        with self.history_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(entry.__dict__)

    def write_text(self, path: str, content: str, message: str) -> None:
        del message
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="")


class GitHubMappingRepository:
    """GitHub Contents API backend with optimistic retries for concurrent updates."""

    api_root = "https://api.github.com"

    def __init__(
        self,
        token: str,
        repository: str,
        branch: str = "main",
        mapping_path: str = "mapping.txt",
        history_path: str = "mapping_history.csv",
    ) -> None:
        if not token or not repository:
            raise ValueError("Для GitHub backend нужны GITHUB_TOKEN и GITHUB_REPOSITORY")
        self.token = token
        self.repository = repository
        self.branch = branch
        self.mapping_path = mapping_path
        self.history_path = history_path

    def list_entries(self) -> list[MappingEntry]:
        content, _ = self._get_file(self.mapping_path, missing_ok=True)
        return parse_mapping_text(content)

    def append_mapping(self, name: str, sku: str) -> bool:
        name_key, sku_key = normalized_key(name), sku.casefold()
        for attempt in range(3):
            content, sha = self._get_file(self.mapping_path, missing_ok=True)
            existing = parse_mapping_text(content)
            if any(normalized_key(item.name) == name_key and item.sku.casefold() == sku_key for item in existing):
                return False
            suffix = "" if not content or content.endswith("\n") else "\n"
            try:
                self._put_file(
                    self.mapping_path,
                    content + suffix + f"{name.strip()} = {sku.strip()}\n",
                    "Add verified mapping",
                    sha,
                )
                return True
            except HTTPError as error:
                if error.code != 409 or attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))
        return False

    def append_history(self, entry: HistoryEntry) -> None:
        row = io.StringIO(newline="")
        writer = csv.DictWriter(row, fieldnames=HISTORY_COLUMNS)
        writer.writerow(entry.__dict__)
        for attempt in range(3):
            content, sha = self._get_file(self.history_path, missing_ok=True)
            prefix = "" if content else ",".join(HISTORY_COLUMNS) + "\r\n"
            suffix = "" if not content or content.endswith(("\n", "\r")) else "\n"
            try:
                self._put_file(self.history_path, content + suffix + prefix + row.getvalue(), "Record mapping history", sha)
                return
            except HTTPError as error:
                if error.code != 409 or attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def write_text(self, path: str, content: str, message: str) -> None:
        for attempt in range(3):
            _, sha = self._get_file(path, missing_ok=True)
            try:
                self._put_file(path, content, message, sha)
                return
            except HTTPError as error:
                if error.code != 409 or attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def _get_file(self, path: str, missing_ok: bool = False) -> tuple[str, str | None]:
        try:
            payload = self._request("GET", f"{self._url(path)}?ref={quote(self.branch)}")
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return "", None
            raise
        content = base64.b64decode(payload["content"].replace("\n", "")).decode("utf-8")
        return content, payload["sha"]

    def _put_file(self, path: str, content: str, message: str, sha: str | None) -> None:
        payload: dict[str, str] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        self._request("PUT", self._url(path), payload)

    def _url(self, path: str) -> str:
        return f"{self.api_root}/repos/{self.repository}/contents/{quote(path)}"

    def _request(self, method: str, url: str, payload: dict[str, str] | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed GitHub API URL
            return json.loads(response.read().decode("utf-8"))


def index_by_normalized_name(entries: Iterable[MappingEntry]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for entry in entries:
        index.setdefault(normalized_key(entry.name), set()).add(entry.sku)
    return index
