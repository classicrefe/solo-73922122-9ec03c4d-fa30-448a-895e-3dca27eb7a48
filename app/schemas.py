"""Request and response models.

Validation here is deliberately structural and cheap.  Business rules that
need the whole batch (weight sums, quote coverage, replacement overlap) live
in :mod:`app.index_math` so that drafts can be edited incrementally and are
only judged at publish time.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from .months import InvalidMonth, parse_month


def _check_month(value: str) -> str:
    try:
        parse_month(value)
    except InvalidMonth as exc:
        raise ValueError(str(exc)) from exc
    return value


Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SlotInput(StrictModel):
    slot_key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    weight_bp: int = Field(ge=1, le=10000, description="integer ten-thousandths")


class ItemInput(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    slot_key: str = Field(min_length=1, max_length=64)
    active_from: Month
    active_through: Month | None = None
    predecessor_code: str | None = Field(default=None, max_length=64)
    prices: dict[Month, PositiveInt] = Field(default_factory=dict)


class BasketCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    base_month: Month
    slots: list[SlotInput] = Field(min_length=1)
    items: list[ItemInput] = Field(min_length=1)

    @model_validator(mode="after")
    def _base_month(self) -> "BasketCreate":
        _check_month(self.base_month)
        return self


class PriceEntry(StrictModel):
    item_code: str = Field(min_length=1, max_length=64)
    month: Month
    price_cents: int = Field(gt=0, le=10**12)


class PriceBatch(StrictModel):
    entries: list[PriceEntry] = Field(min_length=1, max_length=5000)


class WeightUpdate(StrictModel):
    weight_bp: int = Field(ge=1, le=10000)


class ReplacementCreate(StrictModel):
    slot_key: str = Field(min_length=1, max_length=64)
    month: Month
    new_code: str = Field(min_length=1, max_length=64)
    new_name: str = Field(min_length=1, max_length=200)
    old_price_cents: int = Field(gt=0, le=10**12)
    new_price_cents: int = Field(gt=0, le=10**12)

