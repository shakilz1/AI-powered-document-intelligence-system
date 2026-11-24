"""Value normalisation helpers shared by extraction, validation and cross-verification."""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime

from dateutil import parser as dateparser

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY", "₩": "KRW", "₦": "NGN", "৳": "BDT", "R$": "BRL"}
ISO_CURRENCIES = {
    "USD", "EUR", "GBP", "INR", "JPY", "CNY", "AUD", "CAD", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
    "SGD", "HKD", "NZD", "ZAR", "AED", "SAR", "BDT", "PKR", "BRL", "MXN", "KRW", "TRY", "RUB", "NGN", "EGP",
}
CURRENCY_WORDS = {"dollar": "USD", "dollars": "USD", "euro": "EUR", "euros": "EUR", "pound": "GBP", "pounds": "GBP",
                  "rupee": "INR", "rupees": "INR", "taka": "BDT", "yen": "JPY"}

_OCR_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8", "Z": "2"})
_NUM_RE = re.compile(r"\(?-?\s*[$€£₹¥]?\s*\d[\d,.\s']*\d(?:\.\d+)?\)?|\(?-?[$€£₹¥]?\d+(?:[.,]\d+)?\)?")


def parse_money(value) -> float | None:
    """Parse '1,234.56', '1.234,56', '$ 1 234.56', '(12.00)', 'USD 99' -> float. None if not a number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None
        return round(float(value), 2)
    s = str(value).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.endswith("-")
    s = re.sub(r"[A-Za-z]{3}\b", "", s)  # ISO codes
    s = re.sub(r"[^\d,.\-' ]", "", s).strip().strip("-").replace("'", "").replace(" ", "")
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.234,56 (EU)
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts[-1]) == 2 and len(parts) == 2:  # 12,50 decimal comma
            s = s.replace(",", ".")
        else:  # 1,234 thousands
            s = s.replace(",", "")
    elif s.count(".") > 1:  # 1.234.567 thousands
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + ("." + tail if len(tail) == 2 else tail)
    try:
        v = float(s)
    except ValueError:
        return None
    return round(-v if neg else v, 2)


def fix_ocr_number(token: str) -> str:
    """Repair common OCR confusions inside numeric tokens (1O0.00 -> 100.00)."""
    if sum(c.isdigit() for c in token) >= max(1, len(token) // 2):
        return token.translate(_OCR_DIGIT_FIX)
    return token


def find_amounts(line: str) -> list[float]:
    out = []
    for m in re.finditer(r"\(?-?[$€£₹¥]?\s?\d{1,3}(?:[,.' ]\d{3})*(?:[.,]\d{1,2})?\)?|\(?-?[$€£₹¥]?\d+(?:[.,]\d{1,2})?\)?", line):
        tok = m.group(0)
        if re.fullmatch(r"\d{1,2}", tok.strip("()$€£₹¥ ")) and "%" in line[m.end(): m.end() + 2]:
            continue  # percentage like 20%
        v = parse_money(tok)
        if v is not None:
            out.append(v)
    return out


def infer_dayfirst(values) -> bool | None:
    """Look at a set of date strings (e.g. a spreadsheet column) to decide d/m vs m/d order."""
    for v in values:
        m = re.match(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", str(v))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            return True
        if b > 12 >= a:
            return False
    return None


def parse_date(value, dayfirst: bool | None = None) -> tuple[str | None, bool]:
    """Return (ISO date, ambiguous?). Ambiguous = day/month order could not be determined.
    Pass `dayfirst` when the order is known from context (e.g. other values in the column)."""
    if value is None:
        return None, False
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d"), False
    s = str(value).strip()
    if not s or s.lower() in {"nan", "nat", "none"}:
        return None, False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T].*)?", s):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d"), False
        except ValueError:
            return None, False
    if not re.search(r"\d", s):
        return None, False
    # OCR often drops spaces: "January31,2025" -> "January 31, 2025"
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)
    s = re.sub(r",(\S)", r", \1", s)
    # OCR-misread month names in "1-Sop-26" style dates -> nearest month ("Sep")
    m = re.search(r"\d{1,2}\s*[-/. ]\s*([A-Za-z]{3,9})\s*[-/., ]\s*\d{2,4}", s)
    if m and m.group(1).lower()[:3] not in _MONTHS:
        close = difflib.get_close_matches(m.group(1).lower()[:3], _MONTHS, n=1, cutoff=0.6)
        if close:
            s = s[:m.start(1)] + close[0] + s[m.end(1):]
    try:
        d1 = dateparser.parse(s, dayfirst=False, fuzzy=True)
        d2 = dateparser.parse(s, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None, False
    # Reject garbage parses (e.g. OCR noise "Date926" -> year 926).
    if not (1990 <= d1.year <= date.today().year + 2):
        return None, False
    if d1.date() == d2.date():
        return d1.strftime("%Y-%m-%d"), False
    if dayfirst is not None:
        return (d2 if dayfirst else d1).strftime("%Y-%m-%d"), False
    return d1.strftime("%Y-%m-%d"), True


def detect_currency(text: str) -> tuple[str | None, float]:
    """Return (ISO code, confidence)."""
    if not text:
        return None, 0.0
    counts: dict[str, int] = {}
    for code in re.findall(r"\b([A-Z]{3})\b", text):
        if code in ISO_CURRENCIES:
            counts[code] = counts.get(code, 0) + 3
    for sym, code in CURRENCY_SYMBOLS.items():
        n = text.count(sym)
        if n:
            counts[code] = counts.get(code, 0) + n
    for word, code in CURRENCY_WORDS.items():
        if re.search(rf"\b{word}\b", text, re.I):
            counts[code] = counts.get(code, 0) + 1
    n_tk = len(re.findall(r"\bTk\.?\s?\d", text))  # Bangladeshi "Tk 1,200" / "Tk1,200"
    if n_tk:
        counts["BDT"] = counts.get("BDT", 0) + 2 * n_tk
    if not counts:
        return None, 0.0
    best = max(counts, key=counts.get)
    explicit_iso = re.search(rf"\b{best}\b", text) is not None
    conf = 0.95 if explicit_iso else (0.7 if best == "USD" else 0.85)  # "$" alone is ambiguous (USD/CAD/AUD…)
    return best, conf


def norm_invoice_number(v: str | None) -> str | None:
    if not v:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", str(v)).upper()
    return s or None


def norm_name(v: str | None) -> str | None:
    if not v:
        return None
    s = re.sub(r"[^a-z0-9 ]", " ", str(v).lower())
    s = re.sub(r"\b(inc|ltd|llc|limited|corp|corporation|co|gmbh|plc|pvt|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip() or None


def split_camel(s: str | None) -> str | None:
    """OCR often drops spaces between words: 'DeltaElectricalCo' -> 'Delta Electrical Co'.
    Only applied to tokens with 2+ lower->Upper transitions so 'McDonald' stays intact."""
    if not s:
        return s
    def fix(tok: str) -> str:
        if len(tok) < 8 or not re.search(r"[a-z][A-Z]", tok) or re.match(r"(Mc|Mac)[A-Z]", tok):
            return tok
        parts = re.split(r"(?<=[a-z])(?=[A-Z])", tok)
        out: list[str] = []
        for p in parts:  # keep 1-letter tails attached: "GmbH" stays "GmbH"
            if out and len(p) == 1:
                out[-1] += p
            else:
                out.append(p)
        return " ".join(out)
    return " ".join(fix(t) for t in s.split(" "))


def money_close(a: float | None, b: float | None, abs_tol: float = 0.02, rel_tol: float = 0.0) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))
