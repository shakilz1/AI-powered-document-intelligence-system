"""Generate the labeled evaluation dataset.

    python -m eval.generate_dataset --out eval/dataset --seed 42

Writes the files plus `labels.json` with ground truth for every invoice, the expected
validation flags for injected errors, and which files are expected to fail (corrupted).
"""
from __future__ import annotations

import argparse
import copy
import json
import random
from pathlib import Path

from . import synth


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent / "dataset"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--large-pages", type=int, default=100, help="pages in the large multi-invoice PDF")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.iterdir():
        if f.is_file():
            f.unlink()
    rng = random.Random(args.seed)
    counter = iter(range(1, 100000))
    labels: list[dict] = []

    def new(prefix="INV"):
        return synth.random_invoice(rng, next(counter), prefix)

    def header_only(inv):
        c = copy.deepcopy(inv)
        c["line_items"] = []
        return c

    def add(name, category, invoices, expect_fail=False, expected_flags=None):
        labels.append({"file": name, "category": category, "expect_fail": expect_fail,
                       "invoices": [synth.gt(i) for i in invoices],
                       "expected_flags": expected_flags or {}})

    # 1. Digital PDFs, one invoice each, 3 layouts
    for k in range(12):
        inv, lay = new(), "ABC"[k % 3]
        name = f"digital_{lay}_{k:02d}.pdf"
        synth.render_pdf([inv], out / name, rng, [lay])
        add(name, f"pdf_digital", [inv])
    # 2. Multi-invoice PDFs (several invoices in one file)
    for k in range(3):
        invs = [new() for _ in range(5)]
        name = f"multi_invoice_{k}.pdf"
        synth.render_pdf(invs, out / name, rng, ["A", "B", "C"])
        add(name, "pdf_multi", invs)
    # 3. Large PDF for throughput
    invs = [new("BULK") for _ in range(args.large_pages)]
    synth.render_pdf(invs, out / "large_batch.pdf", rng, ["A", "B", "C"])
    add("large_batch.pdf", "pdf_large", invs)
    # 4. Scanned PDFs (image-only pages -> OCR)
    for k, q in enumerate(["clean", "noisy", "faded", "blurry", "noisy", "rotated"]):
        inv = new()
        name = f"scanned_{q}_{k}.pdf"
        synth.render_scanned_pdf([inv], out / name, rng, q)
        add(name, "pdf_scanned", [inv])
    invs = [new() for _ in range(3)]
    synth.render_scanned_pdf(invs, out / "scanned_multi.pdf", rng, "noisy")
    add("scanned_multi.pdf", "pdf_scanned", invs)
    # 5. Images
    for k, (ext, q) in enumerate([(".png", "clean"), (".png", "clean"), (".png", "noisy"), (".png", "noisy"),
                                  (".jpg", "lowres"), (".jpg", "lowres"), (".jpg", "blurry"), (".jpg", "blurry"),
                                  (".tiff", "rotated"), (".tiff", "faded")]):
        inv = new()
        name = f"image_{q}_{k}{ext}"
        synth.render_image(inv, out / name, rng, q, "ABC"[k % 3])
        add(name, "image", [inv])
    # 6. Spreadsheets
    for k in range(2):
        a, b = [new("XL") for _ in range(4)], [new("XL") for _ in range(4)]
        name = f"ledger_{k}.xlsx"
        synth.render_xlsx(a, b, out / name)
        add(name, "xlsx", a + [header_only(i) for i in b])  # Summary sheet has no line items
    for k in range(2):
        invs = [new("XLS") for _ in range(5)]
        name = f"legacy_{k}.xls"
        synth.render_xls(invs, out / name)
        add(name, "xls", [header_only(i) for i in invs])
    for k, style in enumerate(["comma", "comma", "semicolon", "semicolon"]):
        invs = [new("CSV") for _ in range(4)]
        name = f"export_{style}_{k}.csv"
        synth.render_csv(invs, out / name, style)
        add(name, "csv", invs)
    # 7. Corrupted / unsupported content
    for kind, name in [("truncated_pdf", "corrupt_truncated.pdf"), ("random_png", "corrupt_noise.png"),
                       ("empty_csv", "corrupt_empty.csv"), ("broken_xlsx", "corrupt_broken.xlsx"),
                       ("fake_pdf", "corrupt_fake.pdf")]:
        synth.write_corrupted(out / name, kind, rng)
        add(name, "corrupted", [], expect_fail=True)

    # 8. Validation set: injected errors (ground truth = what is printed on the document)
    inv = new(); inv["total"] = round(inv["total"] + 50, 2)
    synth.render_pdf([inv], out / "err_total_mismatch.pdf", rng, ["A"])
    add("err_total_mismatch.pdf", "validation", [inv], expected_flags={inv["invoice_number"]: ["total_mismatch"]})

    inv = new()
    inv["line_items"][0]["amount"] = round(inv["line_items"][0]["amount"] + 10, 2)
    synth.render_pdf([inv], out / "err_line_items.pdf", rng, ["B"])
    add("err_line_items.pdf", "validation", [inv], expected_flags={inv["invoice_number"]: ["line_items_mismatch", "line_item_math"]})

    inv = new(); inv["customer_name"] = None
    synth.render_pdf([inv], out / "err_missing_customer.pdf", rng, ["C"])
    add("err_missing_customer.pdf", "validation", [inv], expected_flags={inv["invoice_number"]: ["missing_field"]})

    inv = new()
    synth.render_pdf([inv], out / "dup_original.pdf", rng, ["A"])
    add("dup_original.pdf", "validation", [inv])
    synth.render_pdf([copy.deepcopy(inv)], out / "dup_copy.pdf", rng, ["B"])
    add("dup_copy.pdf", "validation", [inv], expected_flags={inv["invoice_number"]: ["duplicate_invoice"]})

    # 9. PDF <-> spreadsheet cross-verification: same 3 invoices, one with a different total in the CSV
    invs = [new("XV") for _ in range(3)]
    synth.render_pdf(invs, out / "xv_invoices.pdf", rng, ["A", "B", "C"])
    add("xv_invoices.pdf", "crossverify", invs, expected_flags={invs[1]["invoice_number"]: ["cross_check_mismatch"]})
    sheet = copy.deepcopy(invs)
    sheet[1]["total"] = round(sheet[1]["total"] + 100, 2)
    synth.render_csv(sheet, out / "xv_ledger.csv", "comma")
    add("xv_ledger.csv", "crossverify", sheet,
        expected_flags={sheet[1]["invoice_number"]: ["cross_check_mismatch", "total_mismatch"]})

    (out / "labels.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")
    n_inv = sum(len(l["invoices"]) for l in labels)
    print(f"wrote {len(labels)} files / {n_inv} labeled invoices to {out}")


if __name__ == "__main__":
    main()
