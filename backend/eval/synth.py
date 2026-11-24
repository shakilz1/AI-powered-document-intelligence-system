"""Synthetic invoice generator with ground-truth labels.

Produces realistic variety: 3 PDF layouts, multi-invoice / multi-page PDFs, scanned PDFs,
PNG/JPG/TIFF images with noise/blur/rotation/low resolution, XLSX (multi-sheet, title rows),
XLS, CSV (comma and semicolon/decimal-comma, day-first dates), plus corrupted files.
Used by the test-suite (small fixtures) and by eval/generate_dataset.py (full labeled set).
"""
from __future__ import annotations

import io
import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

SUPPLIERS = ["Acme Industrial Supplies Ltd", "Northwind Traders", "Globex Corporation", "Initech Solutions",
             "Blue Harbor Logistics", "Sunrise Office Products", "Vertex Engineering GmbH", "Pioneer Foods Inc",
             "Delta Electrical Co", "Greenleaf Packaging"]
CUSTOMERS = ["Contoso Retail Group", "Fabrikam Manufacturing", "Wide World Importers", "Tailspin Toys",
             "Adventure Works Cycles", "Litware Systems", "Proseware Analytics", "Coho Winery"]
PRODUCTS = ["Steel bolts M8 (box of 100)", "Office chair ergonomic", "Printer paper A4 (ream)", "Laptop stand aluminium",
            "Hydraulic pump repair", "Consulting services (hours)", "Shipping and handling", "LED panel 60x60",
            "Cardboard boxes large", "Safety gloves (pair)", "Network switch 24-port", "Cleaning service monthly"]
CURRENCIES = [("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("USD", "$"), ("INR", "Rs")]


def random_invoice(rng: random.Random, idx: int, prefix: str = "INV") -> dict:
    n_items = rng.randint(1, 6)
    items = []
    for _ in range(n_items):
        qty = rng.randint(1, 20)
        unit = round(rng.uniform(5, 900), 2)
        items.append({"description": rng.choice(PRODUCTS), "quantity": float(qty), "unit_price": unit,
                      "amount": round(qty * unit, 2)})
    subtotal = round(sum(i["amount"] for i in items), 2)
    rate = rng.choice([0.0, 0.05, 0.1, 0.15, 0.2])
    tax = round(subtotal * rate, 2)
    cur, sym = rng.choice(CURRENCIES)
    d = date(2025, 1, 1) + timedelta(days=rng.randint(0, 600))
    return {
        "invoice_number": f"{prefix}-{d.year}-{idx:05d}",
        "invoice_date": d.isoformat(),
        "supplier_name": rng.choice(SUPPLIERS),
        "customer_name": rng.choice(CUSTOMERS),
        "subtotal": subtotal, "tax": tax, "total": round(subtotal + tax, 2),
        "currency": cur, "_symbol": sym, "line_items": items,
    }


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def _date_str(iso: str, style: str) -> str:
    d = date.fromisoformat(iso)
    return {"iso": iso, "long": d.strftime("%B %d, %Y"), "short": d.strftime("%d %b %Y"),
            "us": d.strftime("%m/%d/%Y")}[style]


# ------------------------------------------------------------------ PDF rendering

def _draw_invoice(c: canvas.Canvas, inv: dict, layout: str, rng: random.Random):
    W, H = A4
    y = H - 60
    cur = inv["currency"]
    if layout == "A":  # classic: labels inline
        c.setFont("Helvetica-Bold", 18); c.drawString(50, y, inv["supplier_name"])
        c.setFont("Helvetica", 9); c.drawString(50, y - 14, "123 Commerce Street, Springfield  |  accounts@example.com")
        c.setFont("Helvetica-Bold", 22); c.drawRightString(W - 50, y, "INVOICE")
        y -= 60
        c.setFont("Helvetica", 11)
        c.drawString(50, y, f"Invoice No: {inv['invoice_number']}")
        c.drawString(50, y - 16, f"Invoice Date: {_date_str(inv['invoice_date'], rng.choice(['iso', 'long', 'short']))}")
        c.drawString(50, y - 32, f"Due Date: {_date_str((date.fromisoformat(inv['invoice_date']) + timedelta(days=30)).isoformat(), 'iso')}")
        c.drawString(320, y, "Bill To:")
        c.drawString(320, y - 16, inv["customer_name"] or "")
        c.drawString(320, y - 32, "45 Market Road, Riverside")
        y -= 70
    elif layout == "B":  # two-column party block, supplier explicitly labelled
        c.setFont("Helvetica-Bold", 20); c.drawString(50, y, "TAX INVOICE")
        y -= 40
        c.setFont("Helvetica-Bold", 11); c.drawString(50, y, "From:"); c.drawString(320, y, "Bill To:")
        c.setFont("Helvetica", 11)
        c.drawString(50, y - 16, inv["supplier_name"]); c.drawString(320, y - 16, inv["customer_name"] or "")
        c.drawString(50, y - 32, "Unit 7, Industrial Park"); c.drawString(320, y - 32, "PO Box 990")
        y -= 64
        c.drawString(50, y, f"Invoice Number: {inv['invoice_number']}")
        c.drawString(320, y, f"Date: {_date_str(inv['invoice_date'], rng.choice(['iso', 'long', 'short']))}")
        y -= 20
        c.drawString(50, y, f"Currency: {cur}")
        y -= 36
    else:  # "C": letterhead without supplier label, "Invoice #"
        c.setFont("Helvetica-Bold", 16); c.drawString(50, y, inv["supplier_name"])
        c.setFont("Helvetica", 9); c.drawString(50, y - 13, "VAT No: GB123456789   Tel: +44 20 7946 0000")
        y -= 50
        c.setFont("Helvetica", 11)
        c.drawString(50, y, f"Invoice # {inv['invoice_number']}")
        c.drawString(50, y - 16, f"Issue Date: {_date_str(inv['invoice_date'], rng.choice(['iso', 'long']))}")
        if inv["customer_name"]:
            c.drawString(50, y - 32, f"Customer: {inv['customer_name']}")
        y -= 64
    # items table
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description"); c.drawRightString(360, y, "Qty"); c.drawRightString(450, y, "Unit Price"); c.drawRightString(W - 50, y, "Amount")
    c.line(50, y - 4, W - 50, y - 4)
    y -= 20
    c.setFont("Helvetica", 10)
    for it in inv["line_items"]:
        c.drawString(50, y, it["description"])
        c.drawRightString(360, y, f"{int(it['quantity'])}")
        c.drawRightString(450, y, _fmt(it["unit_price"]))
        c.drawRightString(W - 50, y, _fmt(it["amount"]))
        y -= 16
    c.line(300, y + 6, W - 50, y + 6)
    y -= 12
    rate = round(inv["tax"] / inv["subtotal"] * 100) if inv["subtotal"] else 0
    tax_label = rng.choice(["Tax", "VAT", "Sales Tax"]) if layout != "C" else "VAT"
    for label, val in (("Subtotal", inv["subtotal"]), (f"{tax_label} ({rate}%)", inv["tax"]), (f"Total ({cur})", inv["total"])):
        c.setFont("Helvetica-Bold" if label.startswith("Total") else "Helvetica", 11)
        c.drawString(320, y, label + ":")
        c.drawRightString(W - 50, y, f"{inv['_symbol'] if inv['_symbol'] != 'Rs' else ''}{_fmt(val)}")
        y -= 18
    c.setFont("Helvetica", 8)
    c.drawString(50, 40, "Payment terms: 30 days. Thank you for your business.")


def render_pdf(invoices: list[dict], path: Path, rng: random.Random, layouts: list[str] | None = None) -> None:
    c = canvas.Canvas(path if isinstance(path, io.BytesIO) else str(path), pagesize=A4)
    for k, inv in enumerate(invoices):
        _draw_invoice(c, inv, (layouts or ["A"])[k % len(layouts or ["A"])], rng)
        c.showPage()
    c.save()


def pdf_page_to_image(pdf_bytes: bytes, page: int = 0, dpi: int = 150) -> Image.Image:
    import pymupdf

    d = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = d.load_page(page).get_pixmap(dpi=dpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def degrade(img: Image.Image, quality: str, rng: random.Random) -> Image.Image:
    if quality == "noisy":
        arr = np.asarray(img).astype(np.int16)
        arr = arr + np.random.default_rng(rng.randint(0, 9999)).normal(0, 28, arr.shape).astype(np.int16)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    elif quality == "blurry":
        img = img.filter(ImageFilter.GaussianBlur(1.3))
    elif quality == "rotated":
        img = img.rotate(rng.uniform(-2.5, 2.5), expand=True, fillcolor="white")
    elif quality == "lowres":
        w, h = img.size
        img = img.resize((int(w * 0.55), int(h * 0.55)), Image.BILINEAR)
    elif quality == "faded":
        arr = np.asarray(img).astype(np.float32)
        img = Image.fromarray((255 - (255 - arr) * 0.45).clip(0, 255).astype(np.uint8))
    return img


def render_image(inv: dict, path: Path, rng: random.Random, quality: str = "clean", layout: str = "A") -> None:
    buf = io.BytesIO()
    render_pdf([inv], buf, rng, [layout])  # type: ignore[arg-type]
    img = degrade(pdf_page_to_image(buf.getvalue(), 0, 150), quality, rng)
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        img.convert("RGB").save(path, quality=70)
    elif ext in (".tif", ".tiff"):
        img.convert("L").save(path, compression="tiff_lzw")
    else:
        img.save(path)


def render_scanned_pdf(invoices: list[dict], path: Path, rng: random.Random, quality: str = "noisy") -> None:
    import pymupdf

    out = pymupdf.open()
    for inv in invoices:
        buf = io.BytesIO()
        render_pdf([inv], buf, rng, [rng.choice("ABC")])  # type: ignore[arg-type]
        img = degrade(pdf_page_to_image(buf.getvalue(), 0, 150), quality, rng)
        ib = io.BytesIO(); img.convert("L").save(ib, format="PNG")
        page = out.new_page(width=A4[0], height=A4[1])
        page.insert_image(page.rect, stream=ib.getvalue())
    out.save(str(path))


# ------------------------------------------------------------------ spreadsheets

def _line_rows(invoices: list[dict], date_fmt: str = "iso") -> list[dict]:
    rows = []
    for inv in invoices:
        for it in inv["line_items"]:
            rows.append({
                "Invoice No": inv["invoice_number"], "Invoice Date": _date_str(inv["invoice_date"], date_fmt) if date_fmt != "dmy" else date.fromisoformat(inv["invoice_date"]).strftime("%d/%m/%Y"),
                "Supplier": inv["supplier_name"], "Customer": inv["customer_name"], "Description": it["description"],
                "Qty": it["quantity"], "Unit Price": it["unit_price"], "Line Total": it["amount"],
                "Subtotal": inv["subtotal"], "Tax": inv["tax"], "Total": inv["total"], "Currency": inv["currency"],
            })
    return rows


def _header_rows(invoices: list[dict]) -> list[dict]:
    return [{"Invoice Number": i["invoice_number"], "Date": i["invoice_date"], "Vendor": i["supplier_name"],
             "Client": i["customer_name"], "Net Amount": i["subtotal"], "VAT": i["tax"], "Grand Total": i["total"],
             "Currency": i["currency"]} for i in invoices]


def render_xlsx(sheet_a: list[dict], sheet_b: list[dict], path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df = pd.DataFrame(_line_rows(sheet_a))
        df.to_excel(xw, sheet_name="Line Items", index=False, startrow=2)
        xw.sheets["Line Items"]["A1"] = "Purchase ledger export"  # title row above the header
        pd.DataFrame(_header_rows(sheet_b)).to_excel(xw, sheet_name="Summary", index=False)
        pd.DataFrame().to_excel(xw, sheet_name="Notes", index=False)  # empty sheet


def render_xls(invoices: list[dict], path: Path) -> None:
    import xlwt

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Invoices")
    rows = _header_rows(invoices)
    for c, k in enumerate(rows[0].keys()):
        ws.write(0, c, k)
    for r, row in enumerate(rows, 1):
        for c, v in enumerate(row.values()):
            ws.write(r, c, v)
    wb.save(str(path))


def render_csv(invoices: list[dict], path: Path, style: str = "comma") -> None:
    if style == "comma":
        pd.DataFrame(_line_rows(invoices)).to_csv(path, index=False)
    else:  # European: semicolon separator, decimal comma, day-first dates
        df = pd.DataFrame(_line_rows(invoices, "dmy"))
        for col in ("Qty", "Unit Price", "Line Total", "Subtotal", "Tax", "Total"):
            df[col] = df[col].map(lambda v: f"{v:.2f}".replace(".", ","))
        df.to_csv(path, index=False, sep=";")


# ------------------------------------------------------------------ corrupted files

def write_corrupted(path: Path, kind: str, rng: random.Random) -> None:
    if kind == "truncated_pdf":
        buf = io.BytesIO(); render_pdf([random_invoice(rng, 99999)], buf, rng)
        path.write_bytes(buf.getvalue()[: len(buf.getvalue()) // 5])
    elif kind == "random_png":
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(rng.getrandbits(8) for _ in range(4000)))
    elif kind == "empty_csv":
        path.write_bytes(b"")
    elif kind == "broken_xlsx":
        path.write_bytes(b"PK\x03\x04" + bytes(rng.getrandbits(8) for _ in range(3000)))
    elif kind == "fake_pdf":
        path.write_bytes(bytes(rng.getrandbits(8) for _ in range(5000)).replace(b"\x00", b"\x01"))


def gt(inv: dict) -> dict:
    """Ground truth without private render keys."""
    return {k: v for k, v in inv.items() if not k.startswith("_")}
