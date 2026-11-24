import pytest

from app.pipeline.normalize import (
    detect_currency,
    infer_dayfirst,
    money_close,
    norm_invoice_number,
    parse_date,
    parse_money,
    split_camel,
)


@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", 1234.56), ("1.234,56", 1234.56), ("$ 1 234.50", 1234.5), ("(12.00)", -12.0),
    ("USD 99", 99.0), ("12,50", 12.5), ("1,234", 1234.0), ("€2.500,00", 2500.0), (1500, 1500.0),
    ("abc", None), ("", None), (None, None), (float("nan"), None),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_parse_date_formats_and_ambiguity():
    assert parse_date("2025-03-04") == ("2025-03-04", False)
    assert parse_date("March 4, 2025") == ("2025-03-04", False)
    assert parse_date("January31,2025") == ("2025-01-31", False)  # OCR without spaces
    iso, amb = parse_date("03/04/2025")
    assert amb is True and iso == "2025-03-04"
    assert parse_date("03/04/2025", dayfirst=True) == ("2025-04-03", False)
    assert parse_date("25/12/2025") == ("2025-12-25", False)  # unambiguous
    assert parse_date("no date here") == (None, False)


def test_infer_dayfirst():
    assert infer_dayfirst(["03/04/2025", "25/04/2025"]) is True
    assert infer_dayfirst(["04/25/2025"]) is False
    assert infer_dayfirst(["01/02/2025"]) is None


def test_currency_detection():
    assert detect_currency("Total (EUR): 100")[0] == "EUR"
    code, conf = detect_currency("Total: $100")
    assert code == "USD" and conf < 0.9  # bare $ is ambiguous
    assert detect_currency("nothing")[0] is None


def test_helpers():
    assert norm_invoice_number("inv-2024/001") == "INV2024001"
    assert split_camel("DeltaElectricalCo") == "Delta Electrical Co"
    assert split_camel("Vertex Engineering GmbH") == "Vertex Engineering GmbH"
    assert split_camel("McDonald") == "McDonald"
    assert money_close(100.0, 100.01) and not money_close(100.0, 100.5)
