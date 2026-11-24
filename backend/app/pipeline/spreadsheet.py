"""Structured extraction from spreadsheet / CSV tables.

Handles: header row not on the first line (title rows above it), synonym column names,
one-row-per-invoice tables, one-row-per-line-item tables (grouped by invoice number),
and key/value sheets ("Invoice Number | INV-1") which are turned into text for the rule parser.
"""
from __future__ import annotations

import re

import pandas as pd

from .normalize import detect_currency, infer_dayfirst, parse_date, parse_money
from .types import HEADER_FIELDS, ExtractedInvoice

SYNONYMS: dict[str, list[str]] = {
    "invoice_number": ["invoice number", "invoice no", "invoice #", "invoice num", "inv no", "inv #", "invoice id", "invoice", "bill no", "document no", "doc no", "inv number", "invoice_number", "invoice_no"],
    "invoice_date": ["invoice date", "date", "inv date", "bill date", "issue date", "invoice_date", "document date"],
    "supplier_name": ["supplier", "supplier name", "vendor", "vendor name", "seller", "from", "issuer", "supplier_name", "company"],
    "customer_name": ["customer", "customer name", "client", "client name", "buyer", "bill to", "sold to", "customer_name"],
    "subtotal": ["subtotal", "sub total", "sub-total", "net amount", "net total", "amount before tax", "net"],
    "tax": ["tax", "vat", "gst", "tax amount", "vat amount", "sales tax", "tax total"],
    "total": ["total", "grand total", "total amount", "amount due", "invoice total", "gross amount", "gross total", "total due"],
    "currency": ["currency", "curr", "ccy", "currency code"],
    "description": ["description", "item", "item description", "product", "service", "particulars", "line description", "details"],
    "quantity": ["quantity", "qty", "units", "hours", "hrs"],
    "unit_price": ["unit price", "price", "rate", "unit cost", "price per unit", "unit_price"],
    "discount_pct": ["discount", "disc", "disc %", "discount %", "disc.%", "discount pct", "discount_pct"],
    "amount": ["amount", "line total", "line amount", "line_total", "extended price", "ext price", "item total", "net line"],
}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9#]+", " ", str(s).lower()).strip()


def _match_column(header: str) -> str | None:
    h = _norm(header)
    if not h:
        return None
    for field, syns in SYNONYMS.items():
        if h in (_norm(x) for x in syns):
            return field
    return None


def _find_header_row(df: pd.DataFrame, max_scan: int = 25) -> tuple[int | None, dict[int, str]]:
    best_row, best_map = None, {}
    for r in range(min(max_scan, len(df))):
        mapping: dict[int, str] = {}
        for c, val in enumerate(df.iloc[r].tolist()):
            if pd.isna(val):
                continue
            f = _match_column(val)
            if f and f not in mapping.values():
                mapping[c] = f
        if len(mapping) > len(best_map):
            best_row, best_map = r, mapping
    if len(best_map) < 2:
        return None, {}
    return best_row, best_map


def _kv_text(df: pd.DataFrame) -> str:
    lines = []
    for row in df.itertuples(index=False):
        cells = [str(v).strip() for v in row if not pd.isna(v) and str(v).strip()]
        if cells:
            lines.append("   ".join(cells))
    return "\n".join(lines)


def extract_table(name: str, df: pd.DataFrame, page_number: int) -> tuple[list[ExtractedInvoice], str | None]:
    """Return (invoices, fallback_text). fallback_text is set when the sheet is not tabular."""
    hdr_row, mapping = _find_header_row(df)
    has_header_fields = hdr_row is not None and any(f in mapping.values() for f in ("invoice_number", "total", "supplier_name"))
    if not has_header_fields:
        return [], _kv_text(df)

    body = df.iloc[hdr_row + 1:].dropna(how="all")
    records = []
    for _, row in body.iterrows():
        rec = {f: row.iloc[c] for c, f in mapping.items()}
        rec = {k: (None if (v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "") else v) for k, v in rec.items()}
        if not any(v is not None for v in rec.values()):
            continue
        records.append(rec)

    # Group rows into invoices (rows with same invoice number = line items of one invoice).
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for i, rec in enumerate(records):
        key = str(rec.get("invoice_number")).strip() if rec.get("invoice_number") is not None else f"__row{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(rec)

    dayfirst = infer_dayfirst(r.get("invoice_date") for r in records if isinstance(r.get("invoice_date"), str))
    invoices: list[ExtractedInvoice] = []
    sheet_text = _kv_text(df)
    sheet_currency, cur_conf = detect_currency(sheet_text)
    for key in order:
        rows = groups[key]
        first = rows[0]
        inv = ExtractedInvoice(method="spreadsheet", page_start=page_number, page_end=page_number)
        conf: dict[str, float] = {}

        def pick(field):
            for r in rows:
                if r.get(field) is not None:
                    return r[field]
            return None

        num = pick("invoice_number")
        if num is not None:
            inv.invoice_number = str(int(num)) if isinstance(num, float) and num.is_integer() else str(num).strip()
            conf["invoice_number"] = 0.98
        raw_date = pick("invoice_date")
        if raw_date is not None:
            iso, amb = parse_date(raw_date, dayfirst)
            inv.invoice_date = iso
            conf["invoice_date"] = 0.7 if amb else 0.98
            if amb:
                inv.mark_uncertain("invoice_date", "Ambiguous day/month order in spreadsheet date")
            if iso is None:
                inv.mark_uncertain("invoice_date", f"Unparseable date value '{raw_date}'")
        for f in ("supplier_name", "customer_name"):
            v = pick(f)
            if v is not None:
                setattr(inv, f, str(v).strip())
                conf[f] = 0.98
        for f in ("subtotal", "tax", "total"):
            v = pick(f)
            if v is not None:
                parsed = parse_money(v)
                setattr(inv, f, parsed)
                conf[f] = 0.98 if parsed is not None else 0.0
                if parsed is None:
                    inv.mark_uncertain(f, f"Non-numeric {f} value '{v}'")
        cur = pick("currency")
        if cur is not None:
            inv.currency, conf["currency"] = str(cur).strip().upper()[:8], 0.98
        elif sheet_currency:
            inv.currency, conf["currency"] = sheet_currency, cur_conf

        if "description" in mapping.values() or "amount" in mapping.values():
            for r in rows:
                if r.get("description") is None and r.get("amount") is None:
                    continue
                inv.line_items.append({
                    "description": None if r.get("description") is None else str(r["description"]).strip(),
                    "quantity": parse_money(r.get("quantity")),
                    "unit_price": parse_money(r.get("unit_price")),
                    "discount_pct": parse_money(str(r["discount_pct"]).rstrip("%")) if r.get("discount_pct") is not None else None,
                    "amount": parse_money(r.get("amount")),
                })
        # If the table is line-item level and has no subtotal column, leave subtotal None (don't invent).
        for f in HEADER_FIELDS:
            if getattr(inv, f) in (None, ""):
                conf[f] = 0.0
        inv.confidence = conf
        inv.source_text = "\n".join(" | ".join(f"{k}={v}" for k, v in r.items() if v is not None) for r in rows)
        invoices.append(inv)
    return invoices, None
