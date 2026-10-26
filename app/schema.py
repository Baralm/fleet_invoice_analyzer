from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_type: Literal["labor", "part", "fee", "discount", "core_charge"]
    part_number: str | None
    description: str = Field(min_length=1)
    quantity: float | None
    unit_price: float | None
    amount: float

class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_items_sum_matches_subtotal: bool
    notes: list[str] = Field(default_factory=list)

class InvoiceExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_order_number: str
    unit_number: str
    vin: str | None
    service_date: str
    vendor_name: str
    line_items: list[LineItem]
    subtotal: float
    tax: float
    total: float
    validation: ValidationResult

    @field_validator("service_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        from datetime import date
        date.fromisoformat(value)
        return value
