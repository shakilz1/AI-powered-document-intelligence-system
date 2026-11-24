"""Task 3 — Validation rules. Pure functions: take an invoice dict, return a list of issues.

Duplicate detection needs the database and lives in `find_duplicates` (called by the processor).
An invoice "needs manual review" if it has any error-severity issue or any uncertain field.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..config import settings
from .normalize import ISO_CURRENCIES, money_close, norm_name

REQUIRED = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "total", "currency"]
RECOMMENDED = ["subtotal", "tax"]  # can legitimately be absent (e.g. no tax charged)


@dataclass
class Issue:
    code: str
    severity: str  # error|warning|info
    message: str
    field: str | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message, "field": self.field}


def _close(a, b) -> bool:
    return money_close(a, b, settings.money_abs_tolerance, settings.money_rel_tolerance)


def validate_invoice(inv: dict, low_conf_threshold: float = 0.7) -> list[Issue]:
    issues: list[Issue] = []
    g = inv.get

    # 1. Missing fields
    for f in REQUIRED:
        if g(f) in (None, ""):
            issues.append(Issue("missing_field", "error", f"Required field '{f}' is missing", f))
    for f in RECOMMENDED:
        if g(f) in (None, ""):
            issues.append(Issue("missing_field", "warning", f"Field '{f}' is missing", f))

    # 2. Incorrect numeric values
    for f in ("subtotal", "tax", "total"):
        v = g(f)
        if v is None:
            continue
        if not isinstance(v, (int, float)):
            issues.append(Issue("invalid_number", "error", f"'{f}' is not a number: {v!r}", f))
            continue
        if v < 0:
            issues.append(Issue("negative_amount", "warning", f"'{f}' is negative ({v}); credit note?", f))
        if abs(v) > 1e9:
            issues.append(Issue("implausible_amount", "error", f"'{f}' is implausibly large ({v})", f))
    sub, tax, total = g("subtotal"), g("tax"), g("total")
    if isinstance(tax, (int, float)) and isinstance(sub, (int, float)) and sub > 0:
        rate = tax / sub
        if rate > 0.5:
            issues.append(Issue("implausible_tax_rate", "warning", f"Tax is {rate:.0%} of subtotal", "tax"))
    if isinstance(total, (int, float)) and isinstance(sub, (int, float)) and total + 0.005 < sub and (tax or 0) >= 0:
        issues.append(Issue("total_below_subtotal", "error", f"Total {total} is lower than subtotal {sub}", "total"))

    # 3. Arithmetic consistency
    if all(isinstance(x, (int, float)) for x in (sub, tax, total)):
        if not _close(sub + tax, total):
            issues.append(Issue("total_mismatch", "error",
                                f"subtotal + tax = {sub + tax:.2f} but total = {total:.2f} (diff {total - sub - tax:+.2f})", "total"))
    items = g("line_items") or []
    amounts = [li.get("amount") for li in items]
    if isinstance(sub, (int, float)) and isinstance(total, (int, float)) and tax is None and not _close(sub, total):
        issues.append(Issue("tax_missing", "error", f"Total {total} differs from subtotal {sub} but no tax amount was found", "tax"))
    if items and all(isinstance(a, (int, float)) for a in amounts):
        s = round(sum(amounts), 2)
        ref, ref_name = (sub, "subtotal") if isinstance(sub, (int, float)) else (
            (total - (tax or 0), "total - tax") if isinstance(total, (int, float)) else (None, None))
        # tolerance grows with the number of lines (each line may be rounded to the cent)
        tol = max(settings.money_abs_tolerance, 0.01 * len(items))
        if ref is not None and abs(s - ref) > tol:
            issues.append(Issue("line_items_mismatch", "error",
                                f"Sum of line items = {s:.2f} but {ref_name} = {ref:.2f} (diff {ref - s:+.2f})", "line_items"))
    for i, li in enumerate(items, 1):
        q, u, a = li.get("quantity"), li.get("unit_price"), li.get("amount")
        d = li.get("discount_pct") or 0
        if all(isinstance(x, (int, float)) for x in (q, u, a)):
            expected = q * u * (1 - d / 100)
            if not _close(expected, a):
                disc = f" - {d:g}%" if d else ""
                issues.append(Issue("line_item_math", "error",
                                    f"Line {i}: {q} x {u}{disc} = {expected:.2f} but amount = {a:.2f}", "line_items"))
        if a is None:
            issues.append(Issue("line_item_missing_amount", "warning", f"Line {i} has no amount", "line_items"))

    sup, cus = g("supplier_name"), g("customer_name")
    if sup and cus and norm_name(sup) == norm_name(cus):
        issues.append(Issue("supplier_equals_customer", "error", "Supplier and customer are the same — likely mis-extraction", "customer_name"))

    # 4. Date & currency sanity
    d = g("invoice_date")
    if d:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            if dt > date.today():
                issues.append(Issue("future_date", "warning", f"Invoice date {d} is in the future", "invoice_date"))
            if dt.year < 1990:
                issues.append(Issue("implausible_date", "warning", f"Invoice date {d} looks wrong", "invoice_date"))
        except ValueError:
            issues.append(Issue("invalid_date", "error", f"Invoice date '{d}' is not a valid ISO date", "invoice_date"))
    cur = g("currency")
    if cur and cur not in ISO_CURRENCIES:
        issues.append(Issue("unknown_currency", "warning", f"Currency '{cur}' is not a recognised ISO code", "currency"))

    # 5. Extraction confidence
    for f in inv.get("uncertain_fields") or []:
        issues.append(Issue("uncertain_value", "warning", f"Extraction of '{f}' is uncertain", f))
    for f, c in (inv.get("confidence") or {}).items():
        if g(f) not in (None, "") and c < low_conf_threshold and f not in (inv.get("uncertain_fields") or []):
            issues.append(Issue("low_confidence", "warning", f"Low confidence ({c:.2f}) for '{f}'", f))
    return issues


def needs_review(issues: list[Issue] | list[dict]) -> bool:
    for i in issues:
        sev = i.severity if isinstance(i, Issue) else i.get("severity")
        code = i.code if isinstance(i, Issue) else i.get("code")
        if sev == "error" or code in ("uncertain_value", "low_confidence", "duplicate_invoice"):
            return True
    return False
