"""PDF↔PDF and PDF↔Excel cross-verification.

Matches invoices from two sets of documents (by normalised invoice number, falling back to
supplier + date + total) and reports every field that disagrees, plus unmatched invoices.
"""
from __future__ import annotations

from ..config import settings
from .normalize import money_close, norm_invoice_number, norm_name

COMPARE = ["invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]


def _same(field: str, a, b) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    if a in (None, "") or b in (None, ""):
        return False
    if field in ("subtotal", "tax", "total"):
        return money_close(float(a), float(b), settings.money_abs_tolerance, settings.money_rel_tolerance)
    if field in ("supplier_name", "customer_name"):
        return norm_name(a) == norm_name(b)
    return str(a).strip().upper() == str(b).strip().upper()


def _fallback_key(inv: dict):
    if inv.get("total") is None:
        return None
    return (norm_name(inv.get("supplier_name")), inv.get("invoice_date"), round(float(inv["total"]), 2))


def cross_verify(left: list[dict], right: list[dict]) -> dict:
    by_no = {}
    for inv in right:
        k = norm_invoice_number(inv.get("invoice_number"))
        if k:
            by_no.setdefault(k, []).append(inv)
    by_fb = {}
    for inv in right:
        k = _fallback_key(inv)
        if k:
            by_fb.setdefault(k, []).append(inv)

    used: set[str] = set()
    matches, unmatched_left = [], []
    for a in left:
        cand = None
        how = None
        k = norm_invoice_number(a.get("invoice_number"))
        for b in by_no.get(k, []) if k else []:
            if b["id"] not in used:
                cand, how = b, "invoice_number"
                break
        if cand is None:
            fk = _fallback_key(a)
            for b in by_fb.get(fk, []) if fk else []:
                if b["id"] not in used:
                    cand, how = b, "supplier+date+total"
                    break
        if cand is None:
            unmatched_left.append(a)
            continue
        used.add(cand["id"])
        diffs = [
            {"field": f, "left": a.get(f), "right": cand.get(f)} for f in COMPARE if not _same(f, a.get(f), cand.get(f))
        ]
        la, lb = a.get("line_items") or [], cand.get("line_items") or []
        if la and lb and len(la) != len(lb):
            diffs.append({"field": "line_items_count", "left": len(la), "right": len(lb)})
        matches.append({"left": a, "right": cand, "matched_by": how, "differences": diffs, "consistent": not diffs})
    unmatched_right = [b for b in right if b["id"] not in used]
    return {
        "summary": {
            "left_invoices": len(left),
            "right_invoices": len(right),
            "matched": len(matches),
            "consistent": sum(m["consistent"] for m in matches),
            "with_differences": sum(not m["consistent"] for m in matches),
            "unmatched_left": len(unmatched_left),
            "unmatched_right": len(unmatched_right),
        },
        "matches": matches,
        "unmatched_left": unmatched_left,
        "unmatched_right": unmatched_right,
    }
