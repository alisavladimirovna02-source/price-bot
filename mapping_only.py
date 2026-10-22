"""Confirmed mapping.txt operations without a SKU registry or catalog."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError

from mapping_validation import ValidationResult, ValidationStatus
from parse_prices import normalize_text
from price_bot.repositories import GitHubMappingRepository, parse_mapping_text


_write_lock = threading.RLock()
MAX_ENTRIES = 100


@dataclass(frozen=True)
class MappingCheck:
    name: str
    sku: str
    status: str  # ADD, EXISTS, CONFLICT
    reason: str = ""


def parse_entries(text: str) -> list[tuple[str, str]]:
    """Accept one supplier name = SKU per line and reject internal conflicts."""
    entries: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.count("=") != 1:
            raise ValueError("Каждая строка должна иметь вид: название поставщика = SKU.")
        name, sku = (part.strip() for part in line.rsplit("=", 1))
        validate_fields(name, sku)
        key = normalize_text(name)
        previous = entries.get(key)
        if previous and previous[1] != sku:
            raise ValueError(f"Название «{name}» повторяется с разными SKU. Исправь список.")
        entries[key] = (name, sku)
    if not entries or len(entries) > MAX_ENTRIES:
        raise ValueError(f"Пришли от 1 до {MAX_ENTRIES} записей: название поставщика = SKU.")
    return list(entries.values())


def validate_fields(name: str, sku: str) -> None:
    if not name or len(name) > 300 or any(ord(char) < 32 for char in name):
        raise ValueError("Название должно занимать одну строку длиной до 300 символов.")
    if not normalize_text(name):
        raise ValueError("В названии должны быть буквы или цифры.")
    if not sku or len(sku) > 200 or any(char.isspace() or char == "=" for char in sku):
        raise ValueError("SKU должен содержать 1–200 символов без пробелов и знака =.")


def check_entries(entries: list[tuple[str, str]], content: str) -> list[MappingCheck]:
    existing: dict[str, set[str]] = {}
    for item in parse_mapping_text(content):
        existing.setdefault(normalize_text(item.name), set()).add(item.sku)
    checks = []
    for name, sku in entries:
        validate_fields(name, sku)
        matches = existing.get(normalize_text(name), set())
        if len(matches) > 1 or (matches and sku not in matches):
            checks.append(MappingCheck(name, sku, "CONFLICT", "Название уже связано с другим SKU в mapping.txt"))
        elif matches:
            checks.append(MappingCheck(name, sku, "EXISTS"))
        else:
            checks.append(MappingCheck(name, sku, "ADD"))
            existing.setdefault(normalize_text(name), set()).add(sku)
    return checks


def validation_result(name: str, sku: str, content: str) -> ValidationResult:
    try:
        check = check_entries([(name, sku)], content)[0]
    except ValueError as error:
        return ValidationResult(ValidationStatus.BLOCKED, name, sku, None, mapping_signals=(str(error),))
    if check.status == "CONFLICT":
        return ValidationResult(ValidationStatus.BLOCKED, name, sku, None, mapping_signals=(check.reason,))
    return ValidationResult(ValidationStatus.OK, name, sku, None)


def github_repository() -> GitHubMappingRepository:
    return GitHubMappingRepository(
        token=os.getenv("GITHUB_TOKEN", ""),
        repository=os.getenv("GITHUB_REPO") or os.getenv("GITHUB_REPOSITORY", ""),
        branch=os.getenv("GITHUB_BRANCH", "main"),
    )


def save_entries(entries: list[tuple[str, str]], mapping_path: Path, repository=None) -> list[MappingCheck]:
    """Write a confirmed batch remotely, then atomically refresh local mapping."""
    repository = repository or github_repository()
    with _write_lock:
        for attempt in range(3):
            content, sha = repository._get_file(repository.mapping_path, missing_ok=True)
            checks = check_entries(entries, content)
            if any(item.status == "CONFLICT" for item in checks):
                return checks  # Never partially save a contradictory batch.
            additions = [item for item in checks if item.status == "ADD"]
            if not additions:
                break
            suffix = "" if not content or content.endswith("\n") else "\n"
            updated = content + suffix + "".join(f"{item.name} = {item.sku}\n" for item in additions)
            try:
                repository._put_file(repository.mapping_path, updated, "Add confirmed mappings", sha)
                break
            except HTTPError as error:
                if error.code != 409 or attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))
        remote_content, _ = repository._get_file(repository.mapping_path, missing_ok=True)
        destination = Path(mapping_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".mapping-sync.tmp")
        temporary.write_text(remote_content, encoding="utf-8")
        temporary.replace(destination)
        return checks
