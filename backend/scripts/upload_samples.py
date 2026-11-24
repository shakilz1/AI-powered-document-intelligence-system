"""Upload a mixed batch of sample files (from eval/dataset) to a running server.

    python scripts/upload_samples.py [--url http://127.0.0.1:8000] [--api-key KEY]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx

SAMPLES = [
    "digital_A_00.pdf", "digital_B_01.pdf", "digital_C_02.pdf", "multi_invoice_0.pdf", "scanned_noisy_1.pdf",
    "image_clean_0.png", "image_lowres_4.jpg", "image_rotated_8.tiff", "image_blurry_7.jpg", "ledger_0.xlsx",
    "legacy_0.xls", "export_semicolon_2.csv", "err_total_mismatch.pdf", "err_line_items.pdf", "dup_original.pdf",
    "dup_copy.pdf", "xv_invoices.pdf", "xv_ledger.csv", "corrupt_truncated.pdf", "corrupt_fake.pdf",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--api-key")
    args = ap.parse_args()
    ds = Path(__file__).resolve().parent.parent / "eval" / "dataset"
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    files = [("files", (n, (ds / n).read_bytes(), "application/octet-stream")) for n in SAMPLES if (ds / n).exists()]
    r = httpx.post(f"{args.url}/api/documents", files=files, headers=headers, timeout=120)
    r.raise_for_status()
    body = r.json()
    print(f"accepted {len(body['accepted'])}, rejected {len(body['rejected'])}")


if __name__ == "__main__":
    main()
