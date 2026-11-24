"""Deterministic (regex + layout) invoice extractor.

Used (a) when no LLM key is configured, (b) as a cross-check against LLM output, and
(c) as the fallback when the LLM call fails. It never guesses: a field it cannot locate
stays None, and heuristic matches get a low confidence and are listed as uncertain.
"""
from __future__ import annotations

import re
import unicodedata

from .normalize import detect_currency, find_amounts, fix_ocr_number, parse_date, parse_money, split_camel
from .types import HEADER_FIELDS, ExtractedInvoice

COL_SPLIT = re.compile(r"\s{3,}|\t|\s\|\s")

# NB: multi-word labels use \s* because OCR frequently drops spaces ("InvoiceDate:").
INV_NO_RE = re.compile(
    r"\b(?:invoice|inv(?![\-/]?\d)|bill)\.?\s*(?:no\.?|number|num\.?|#|id|ref\.?)?\s*[:#.\-]?\s*"
    r"([A-Z0-9][A-Z0-9\-/_.]*\d[A-Z0-9\-/_.]*)",
    re.I,
)
DATE_LABEL_RE = re.compile(r"(invoice\s*date|date\s*of\s*issue|issue\s*date|issued|bill\s*date|\bdated?)\s*[:.\-]?\s*", re.I)
BAD_DATE_LABEL_RE = re.compile(r"(due|delivery|ship|shipping|order|payment|period|service)\s*date|\bdate\s*due\b", re.I)

SUPPLIER_LABEL_RE = re.compile(r"^\s*(supplier|vendor|seller|from|issued\s*by|billed\s*by|sold\s*by|company)\b\s*(name)?\s*[:\-]?\s*", re.I)
CUSTOMER_LABEL_RE = re.compile(r"^\s*(customer|client|buyer|bill\s*to|billed\s*to|sold\s*to|invoice\s*to|party|m/s|messrs|consignee|to)\b\s*(name)?\s*[:\-]?\s*", re.I)

SUBTOTAL_RE = re.compile(r"(sub\s*-?\s*total|net\s*(?:amount|total)|total\s*(?:before|excl\.?|excluding)\s*(?:tax|vat)|amount\s*before\s*tax)", re.I)
TAX_RE = re.compile(r"(total\s*tax|sales\s*tax|tax\s*amount|\bvat\b|\bgst\b|\bhst\b|\btax\b)", re.I)
TAX_EXCLUDE_RE = re.compile(r"(tax\s*(id|no\b|number|reg)|vat\s*(no\b|number|reg|id)|gstin|tax\s*invoice|before\s*tax|\bexcl|including|\bincl)", re.I)
TOTAL_STRONG_RE = re.compile(r"(grand\s*total|total\s*amount\s*due|total\s*due|amount\s*due|balance\s*due|invoice\s*total|total\s*amount|total\s*payable|amount\s*payable)", re.I)
TOTAL_RE = re.compile(r"(?<!sub)(?<!sub-)(?<!sub )\btotal\b", re.I)

ITEM_HEADER_RE = re.compile(r"(descrip\w*|\bitems?\b|product|service|particulars|details|goods)", re.I)
QTY_HEADER_RE = re.compile(r"\b(qty|qnty|quantity|units?|hrs|hours)\b", re.I)
AMT_HEADER_RE = re.compile(r"\b(amount|total|line\s+total|price|taka|tk)\b", re.I)
UNIT_RE = re.compile(r"^(?:pcs?|pieces?|nos?\.?|units?|ea|each|sets?|kgs?|mtrs?|meters?|ltrs?|box(?:es)?|pkts?|pairs?|hrs?|hours?|days?|rolls?|coils?|per)$", re.I)
QTY_UNIT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:pcs?|pieces?|nos?|units?|ea|sets?|kgs?|mtrs?|ltrs?|box(?:es)?|pkts?|pairs?|hrs?|rolls?|coils?)\.?$", re.I)
_MONEY = r"(?:[$€£₹¥৳]|tk\.?|rs\.?)?\d[\d,.']*"
MONEY_TOKEN_RE = re.compile(rf"^(?:\(-?{_MONEY}\)|-?{_MONEY})$", re.I)  # "(12.00)" ok, "100)" is not a number
ITEM_ROW_RE = re.compile(
    r"^(?P<desc>.*?[A-Za-z].*?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[$€£₹¥]?\s?[\d,.']+\d)\s+(?P<amt>[$€£₹¥]?\s?[\d,.']+\d)\s*$"
)
TITLE_WORDS = {"invoice", "tax invoice", "commercial invoice", "receipt", "bill", "statement", "proforma invoice"}


def _lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines()]


def _after_label(line: str, m: re.Match) -> str:
    rest = line[m.end():].strip()
    # value is the first column after the label
    return COL_SPLIT.split(rest)[0].strip(" :-") if rest else ""


def _last_amount(line: str) -> float | None:
    amounts = find_amounts(" ".join(fix_ocr_number(t) for t in line.split()))
    return amounts[-1] if amounts else None


def _find_party(lines: list[str], label_re: re.Pattern, other_re: re.Pattern) -> tuple[str | None, float]:
    for i, ln in enumerate(lines[:80]):
        cols = COL_SPLIT.split(ln.strip())
        for ci, col in enumerate(cols):
            m = label_re.match(col)
            if not m:
                continue
            val = col[m.end():].strip(" :-")
            if val and not other_re.match(val):
                return val, 0.9
            # "Supplier   Acme Ltd" (label and value in adjacent columns, e.g. key/value sheets)
            if ci + 1 < len(cols):
                nxt_col = cols[ci + 1].strip(" :-")
                if nxt_col and not label_re.match(nxt_col) and not other_re.match(nxt_col) and len(cols) == 2:
                    return nxt_col, 0.85
            # Value on the next non-empty line; respect column position for two-column blocks.
            for nxt in lines[i + 1: i + 4]:
                if not nxt.strip():
                    continue
                ncols = COL_SPLIT.split(nxt.strip())
                if len(cols) > 1 and len(ncols) >= len(cols):
                    cand = ncols[ci]
                elif len(cols) > 1 and ci > 0 and len(ncols) > 1:
                    cand = ncols[-1]
                else:
                    cand = ncols[0]
                if cand.strip().endswith(":"):  # the "value" is just another label
                    break
                cand = cand.strip(" :-")
                if cand and not label_re.match(cand) and not other_re.match(cand):
                    return cand, 0.85
                break
    return None, 0.0


def _find_line_items(lines: list[str]) -> list[dict]:
    items: list[dict] = []
    start = None
    for i, ln in enumerate(lines):
        if ITEM_HEADER_RE.search(ln) and QTY_HEADER_RE.search(ln) and AMT_HEADER_RE.search(ln) and not find_amounts(ln):
            start = i + 1
            break
    if start is None:
        return items
    for ln in lines[start:]:
        s = ln.strip()
        if not s:
            continue
        if SUBTOTAL_RE.search(s) or TOTAL_STRONG_RE.search(s) or (TOTAL_RE.search(s) and len(COL_SPLIT.split(s)) <= 3) or (
            TAX_RE.search(s) and not TAX_EXCLUDE_RE.search(s) and len(COL_SPLIT.split(s)) <= 3
        ):
            break
        cols = [c for c in COL_SPLIT.split(s) if c.strip()]
        if any(re.match(r"^\s*(grand\s*)?total\b", c, re.I) for c in cols):
            break
        row = parse_item_row(s)
        if row:
            items.append(row)
    return items


def parse_item_row(s: str) -> dict | None:
    """Parse one item-table row: description first, numeric columns read from the right.
    Handles "26Pcs" / "26 Pcs" quantities, unit columns ("per Pcs"), discounts ("13%") and
    currency prefixes ("Tk1,200.00")."""
    tokens = s.split()
    nums: list[float] = []
    discount = None
    i = len(tokens)
    while i > 0:
        t = tokens[i - 1]
        tc = fix_ocr_number(t) if re.search(r"\d", t) and not re.search(r"[A-Za-z]{2,}", t) else t
        m = QTY_UNIT_RE.match(tc)
        if re.fullmatch(r"\d+(?:[.,]\d+)?%", tc):
            discount = float(tc[:-1].replace(",", "."))
        elif UNIT_RE.match(t):
            pass
        elif m:
            nums.insert(0, float(m.group(1).replace(",", ".")))
        elif MONEY_TOKEN_RE.match(tc) and parse_money(tc) is not None:
            nums.insert(0, parse_money(tc))
        else:
            break
        i -= 1
    desc = " ".join(tokens[:i]).strip(" -:|")
    if len(nums) < 2 or not re.search(r"[A-Za-z]{2,}", desc):
        return None
    qty, amt = nums[0], nums[-1]
    unit = nums[1] if len(nums) >= 3 else None
    return {"description": desc, "quantity": qty, "unit_price": unit, "discount_pct": discount, "amount": amt}


def extract_rules(text: str, ocr_confidence: float | None = None) -> ExtractedInvoice:
    inv = ExtractedInvoice(method="rules", source_text=text)
    text = unicodedata.normalize("NFKC", text)  # full-width ":" etc. from OCR
    lines = _lines(text)
    conf: dict[str, float] = {}

    # invoice number
    for ln in lines:
        m = INV_NO_RE.search(ln)
        if m and not re.search(r"\bdate\b", ln[m.start(): m.start(1)], re.I):
            inv.invoice_number = m.group(1).strip(".-/")
            conf["invoice_number"] = 0.9
            break

    # invoice date (prefer explicit "invoice date", ignore due/delivery dates)
    best_date = None
    for ln in lines:
        if BAD_DATE_LABEL_RE.search(ln) and not re.search(r"invoice\s+date", ln, re.I):
            continue
        m = DATE_LABEL_RE.search(ln)
        if not m:
            continue
        val = _after_label(ln, m)
        iso, ambiguous = parse_date(val) if val else (None, False)
        if iso:
            score = 2 if "invoice" in m.group(1).lower() or "issue" in m.group(1).lower() else 1
            if best_date is None or score > best_date[2]:
                best_date = (iso, ambiguous, score)
    if best_date:
        inv.invoice_date = best_date[0]
        conf["invoice_date"] = 0.6 if best_date[1] else 0.9
        if best_date[1]:
            inv.mark_uncertain("invoice_date", "Date is ambiguous (day/month order); month-first assumed")

    # parties
    inv.supplier_name, conf["supplier_name"] = _find_party(lines, SUPPLIER_LABEL_RE, CUSTOMER_LABEL_RE)
    inv.customer_name, conf["customer_name"] = _find_party(lines, CUSTOMER_LABEL_RE, SUPPLIER_LABEL_RE)
    if not inv.supplier_name:
        # Heuristic: the letterhead (first meaningful line that is not a title/label/number line).
        def letterhead_ok(s: str) -> bool:
            return bool(
                s
                and s.lower() not in TITLE_WORDS
                and not re.search(r"\d{3,}|@|www\.|invoice|date|page|\+\d", s, re.I)
                and (len(s) >= 6 or " " in s)  # skip logo fragments like "bec"
                and re.search(r"[A-Za-z]{3,}", s)
                and not CUSTOMER_LABEL_RE.match(s)
                and not SUPPLIER_LABEL_RE.match(s)
            )

        found = False
        for li, ln in enumerate(lines[:6]):
            for ci, s in enumerate(c.strip() for c in COL_SPLIT.split(ln.strip())):
                if ci > 1:
                    break
                if letterhead_ok(s):
                    # First line of the page = letterhead, the issuer in the vast majority of invoices.
                    inv.supplier_name, conf["supplier_name"] = s, (0.75 if li == 0 and ci == 0 else 0.5)
                    inv.notes.append("Supplier inferred from letterhead (no explicit label)")
                    found = True
                    break
            if found:
                break

    # totals — scan bottom-up so summary blocks win over header mentions
    for ln in reversed(lines):
        if inv.subtotal is None and SUBTOTAL_RE.search(ln):
            inv.subtotal = _last_amount(ln)
        elif inv.tax is None and TAX_RE.search(ln) and not TAX_EXCLUDE_RE.search(ln) and not SUBTOTAL_RE.search(ln):
            v = _last_amount(ln)
            if v is not None:
                inv.tax = v
    strong = [ln for ln in lines if TOTAL_STRONG_RE.search(ln) and not SUBTOTAL_RE.search(ln) and _last_amount(ln) is not None]
    weak = [ln for ln in lines if TOTAL_RE.search(ln) and not SUBTOTAL_RE.search(ln) and not re.search(r"total\s+(tax|qty|quantity|items?)", ln, re.I) and _last_amount(ln) is not None]
    if strong:
        inv.total, conf["total"] = _last_amount(strong[-1]), 0.9
    elif weak:
        inv.total, conf["total"] = _last_amount(weak[-1]), 0.8
    else:
        # OCR sometimes loses the "Total" label. Accept an amount-only line directly below the
        # tax/subtotal block, but with low confidence so it is flagged for review.
        for i, ln in enumerate(lines):
            if (TAX_RE.search(ln) or SUBTOTAL_RE.search(ln)) and _last_amount(ln) is not None:
                nxt = next((x for x in lines[i + 1:] if x.strip()), "")
                if nxt and not re.search(r"[A-Za-z]{3,}", nxt) and _last_amount(nxt) is not None and not (
                    TAX_RE.search(nxt) or SUBTOTAL_RE.search(nxt)):
                    inv.total, conf["total"] = _last_amount(nxt), 0.55
                    inv.mark_uncertain("total", "Total label not found; took the amount below the tax line")

    if inv.subtotal is not None:
        conf["subtotal"] = 0.9
    if inv.tax is not None:
        conf["tax"] = 0.85

    inv.currency, conf["currency"] = detect_currency(text)
    inv.line_items = _find_line_items(lines)

    # OCR'd text is less trustworthy: repair dropped spaces, scale confidences by OCR confidence.
    if ocr_confidence is not None:
        inv.supplier_name = split_camel(inv.supplier_name)
        inv.customer_name = split_camel(inv.customer_name)
        for li in inv.line_items:
            li["description"] = split_camel(li["description"])
        factor = min(1.0, ocr_confidence + 0.05)
        conf = {k: round(v * factor, 3) for k, v in conf.items()}
    for f in HEADER_FIELDS:
        if getattr(inv, f) in (None, ""):
            conf[f] = 0.0
        elif conf.get(f, 0) < 0.7:
            inv.mark_uncertain(f)
    inv.confidence = conf
    return inv
