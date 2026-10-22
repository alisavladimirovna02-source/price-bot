from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ValidationStatus(StrEnum):
    OK = "OK"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    SKU_NOT_FOUND = "SKU_NOT_FOUND"


class SkuParseStatus(StrEnum):
    """How safely an internal SKU can be interpreted without a reference card."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    UNPARSEABLE = "UNPARSEABLE"


@dataclass(frozen=True)
class Difference:
    field: str
    source: str | None
    catalog: str | None
    reason: str | None = None

    def display(self) -> str:
        label = FIELD_LABELS.get(self.field, self.field)
        if self.reason:
            return f"{label}: {self.reason}"
        return f"{label}: {self.source or 'не указано'} ≠ {self.catalog or 'не указано'}"


FIELD_LABELS = {
    "brand": "Бренд",
    "category": "Категория",
    "model": "Модель",
    "variant": "Версия",
    "screen": "Диагональ",
    "chip": "Чип",
    "ram": "Оперативная память",
    "storage": "Память",
    "network": "Сеть",
    "sim": "SIM",
    "connectivity": "Подключение",
    "generation": "Поколение",
    "watch_size": "Размер корпуса",
    "case_type": "Кейс",
    "color": "Цвет",
}


@dataclass(frozen=True)
class ProductFeatures:
    raw_name: str
    normalized_name: str
    brand: str | None = None
    category: str | None = None
    model: str | None = None
    variant: str | None = None
    screen: str | None = None
    chip: str | None = None
    ram: str | None = None
    storage: str | None = None
    network: str | None = None
    sim: str | None = None
    connectivity: str | None = None
    generation: str | None = None
    watch_size: str | None = None
    case_type: str | None = None
    color: str | None = None
    region: str | None = None
    region_original: str | None = None
    model_code: str | None = None

    def value(self, field_name: str) -> str | None:
        return getattr(self, field_name)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SkuParseResult:
    """A SKU and only the facts that can actually be read from it.

    ``features`` deliberately contains ``None`` for information which is not
    encoded in an SKU.  Consumers must never replace those values with a
    guessed configuration.
    """

    sku: str
    status: SkuParseStatus
    features: ProductFeatures
    comment: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = self.features.as_dict()
        data.update({"sku": self.sku, "parse_status": self.status.value, "comment": self.comment})
        return data


@dataclass(frozen=True)
class ValidationResult:
    status: ValidationStatus
    source_name: str
    sku: str
    catalog_name: str | None
    source_features: ProductFeatures
    catalog_features: ProductFeatures | None
    conflicts: tuple[Difference, ...] = ()
    reviews: tuple[Difference, ...] = ()
    matches: tuple[Difference, ...] = ()
    mapping_signals: tuple[str, ...] = ()

    @property
    def can_add_without_override(self) -> bool:
        return self.status in {ValidationStatus.OK, ValidationStatus.REVIEW}

    @property
    def reason(self) -> str:
        differences = self.conflicts or self.reviews
        if differences:
            return "; ".join(item.display() for item in differences)
        if self.mapping_signals:
            return "; ".join(self.mapping_signals)
        return "—"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "source_name": self.source_name,
            "sku": self.sku,
            "catalog_name": self.catalog_name,
            "conflicts": [asdict(item) for item in self.conflicts],
            "reviews": [asdict(item) for item in self.reviews],
            "matches": [asdict(item) for item in self.matches],
            "mapping_signals": list(self.mapping_signals),
        }


@dataclass(frozen=True)
class MappingEntry:
    name: str
    sku: str
    line_number: int | None = None


@dataclass(frozen=True)
class HistoryEntry:
    date: str
    telegram_user_id: str
    action: str
    source_name: str
    sku: str
    catalog_name: str | None
    status: str
    conflicts: str


@dataclass
class AuditReport:
    rows: list[dict[str, str]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        result = {status.value: 0 for status in ValidationStatus}
        for row in self.rows:
            result[row["Статус"]] = result.get(row["Статус"], 0) + 1
        return result
