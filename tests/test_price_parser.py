import pytest

from app.services.price_parser import parse_price


@pytest.mark.parametrize(("text", "expected_ccy", "amount", "currency"), [
    ("£51.77", None, 51.77, "GBP"),
    ("$1,299.99", "USD", 1299.99, "USD"),
    ("1.299,99 €", None, 1299.99, "EUR"),
    ("€ 12,50", None, 12.50, "EUR"),
    ("Rs. 4,500", "PKR", 4500.0, "PKR"),
    ("USD 12", None, 12.0, "USD"),
    ("1 234,56 zł", None, 1234.56, "PLN"),
    ("1.299", "EUR", 1299.0, None),  # no currency shown: parser never invents one
    ("CHF 1'250.00", None, 1250.0, "CHF"),
    ("Save 20% now $8.00", "USD", 8.0, "USD"),
])
def test_parses_real_world_formats(text, expected_ccy, amount, currency):
    r = parse_price(text, expected_ccy)
    assert r.amount == pytest.approx(amount)
    assert r.currency == currency
    assert not r.ambiguous


def test_ambiguous_symbol_is_not_guessed_without_config():
    r = parse_price("$10.00")
    assert r.amount == 10.0
    assert r.currency is None
    assert "USD" in r.currency_candidates


def test_ambiguous_symbol_resolved_by_configured_currency():
    assert parse_price("$10.00", "CAD").currency == "CAD"


def test_multiple_prices_flagged_ambiguous():
    r = parse_price("Was £20.00 Now £15.00")
    assert r.ambiguous


def test_three_decimals_without_context_is_ambiguous():
    assert parse_price("1.299").ambiguous


@pytest.mark.parametrize("text", ["", None, "Call for price"])
def test_no_price(text):
    assert parse_price(text).amount is None


def test_numeric_input():
    assert parse_price(19.5, "USD").amount == 19.5
