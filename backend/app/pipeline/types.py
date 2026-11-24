from __future__ import annotations

from dataclasses import asdict, dataclass, field

HEADER_FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
MONEY_FIELDS = ["subtotal", "tax", "total"]


@dataclass
class ExtractedInvoice:
    invoice_number: str | None = None
    invoice_date: str | None = None  # ISO yyyy-mm-dd
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    currency: str | None = None
    line_items: list[dict] = field(default_factory=list)  # description, quantity, unit_price, amount
    confidence: dict[str, float] = field(default_factory=dict)
    uncertain_fields: list[str] = field(default_factory=list)
    method: str = "rules"
    notes: list[str] = field(default_factory=list)
    derived_fields: list[str] = field(default_factory=list)  # filled by a business rule, not read from the document
    page_start: int | None = None
    page_end: int | None = None
    source_text: str = ""

    def to_dict(self, include_text: bool = False) -> dict:
        d = asdict(self)
        if not include_text:
            d.pop("source_text", None)
        return d

    def is_empty(self) -> bool:
        return not any(getattr(self, f) not in (None, "") for f in HEADER_FIELDS) and not self.line_items

    def apply_defaults(self) -> None:
        """Business rules applied after extraction (recorded in derived_fields + notes, never silent):
        - no tax on the document -> tax = 0
        - no subtotal on the document -> subtotal = sum of the line-item amounts (only when every line has one)
        The validation rules still compare subtotal + tax with the printed total, so a misread line or a
        missing tax line is still flagged for review."""
        if self.is_empty():
            return
        if self.tax is None:
            self.tax = 0.0
            self.derived_fields.append("tax")
            self.confidence["tax"] = 0.9
            self.notes.append("No tax on the document: tax set to 0")
        amounts = [li.get("amount") for li in self.line_items]
        if self.subtotal is None and amounts and all(
                isinstance(a, (int, float)) and not isinstance(a, bool) for a in amounts):
            self.subtotal = round(sum(amounts), 2)
            self.derived_fields.append("subtotal")
            self.confidence["subtotal"] = 0.9
            self.notes.append(f"No subtotal on the document: subtotal = sum of {len(amounts)} line amounts")

    def mark_uncertain(self, name: str, note: str | None = None) -> None:
        if name not in self.uncertain_fields:
            self.uncertain_fields.append(name)
        if note:
            self.notes.append(note)
