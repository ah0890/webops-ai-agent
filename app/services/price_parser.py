"""Robust parsing of real-world price strings ("£1,299.00", "1.299,99 €", "Rs. 4,500", "USD 12")."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Unambiguous symbols/codes → ISO currency.
_SYMBOLS: dict[str, str] = {
    "€": "EUR", "£": "GBP", "₹": "INR", "₨": "PKR", "zł": "PLN", "₺": "TRY", "₩": "KRW",
    "US$": "USD", "C$": "CAD", "CA$": "CAD", "A$": "AUD", "AU$": "AUD", "NZ$": "NZD", "S$": "SGD",
    "د.إ": "AED", "﷼": "SAR", "CHF": "CHF", "Fr.": "CHF",
}
# Symbols shared by several currencies: we only accept them as *compatible* with a configured currency.
_AMBIGUOUS: dict[str, set[str]] = {
    "$": {"USD", "CAD", "AUD", "NZD", "SGD", "MXN"},
    "¥": {"JPY", "CNY"},
    "kr": {"SEK", "NOK", "DKK"},
    "Rs": {"PKR", "INR"},
    "Rs.": {"PKR", "INR"},
}
_ISO = re.compile(r"\b([A-Z]{3})\b")
_NUMBER = re.compile(r"\d[\d.,'   ]*\d|\d")
DECIMAL_COMMA_CURRENCIES = {"EUR", "PLN", "TRY", "SEK", "NOK", "DKK", "CHF", "BRL"}


@dataclass
class ParsedPrice:
    amount: float | None
    currency: str | None = None
    currency_candidates: set[str] = field(default_factory=set)
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)


def _normalise_number(chunk: str, decimal_comma_hint: bool | None) -> tuple[float | None, bool]:
    """Return (value, ambiguous)."""
    s = re.sub(r"[   ']", "", chunk)
    has_dot, has_comma = "." in s, "," in s
    ambiguous = False
    if has_dot and has_comma:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif has_comma:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = s.replace(",", ".")
        elif len(parts) == 2 and len(parts[1]) == 3 and decimal_comma_hint is None:
            s = s.replace(",", "")  # "1,299" — almost always thousands
        else:
            s = s.replace(",", "")
    elif has_dot:
        parts = s.split(".")
        if len(parts) > 2:
            s = s.replace(".", "")  # 1.299.000
        elif len(parts[1]) == 3:
            if decimal_comma_hint:
                s = s.replace(".", "")  # "1.299" EUR → 1299
            elif decimal_comma_hint is None:
                ambiguous = True
    try:
        return float(s), ambiguous
    except ValueError:
        return None, True


def detect_currency(text: str) -> tuple[str | None, set[str]]:
    for sym in sorted(_SYMBOLS, key=len, reverse=True):
        if sym in text:
            return _SYMBOLS[sym], {_SYMBOLS[sym]}
    m = _ISO.search(text)
    if m and m.group(1) not in {"VAT", "INC", "EXC", "NOW", "WAS", "OFF", "NEW"}:
        return m.group(1), {m.group(1)}
    for sym in sorted(_AMBIGUOUS, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", text):
            return None, set(_AMBIGUOUS[sym])
    return None, set()


def parse_price(text: str | float | int | None, expected_currency: str | None = None) -> ParsedPrice:
    """Parse a price. `expected_currency` (from config) resolves ambiguous symbols and decimal style."""
    if text is None:
        return ParsedPrice(None, notes=["no price text"])
    if isinstance(text, (int, float)):
        return ParsedPrice(float(text), expected_currency, {expected_currency} if expected_currency else set())
    raw = " ".join(str(text).split())
    if not raw:
        return ParsedPrice(None, notes=["empty price text"])

    currency, candidates = detect_currency(raw)
    if currency is None and expected_currency and expected_currency in candidates:
        currency = expected_currency
    hint_ccy = currency or expected_currency
    hint: bool | None = (hint_ccy in DECIMAL_COMMA_CURRENCIES) if hint_ccy else None

    values: list[float] = []
    ambiguous = False
    for m in _NUMBER.finditer(raw):
        tail = raw[m.end(): m.end() + 2].strip()
        if tail.startswith("%"):
            continue  # "Save 20%"
        v, amb = _normalise_number(m.group(0).strip(), hint)
        if v is not None:
            values.append(v)
            ambiguous |= amb

    result = ParsedPrice(None, currency, candidates)
    distinct = sorted(set(values))
    if not distinct:
        result.notes.append(f"no number in {raw!r}")
        return result
    if len(distinct) > 1:
        result.ambiguous = True
        result.notes.append(f"multiple prices in text {raw!r}: {distinct}")
    result.amount = values[0]
    if ambiguous:
        result.ambiguous = True
        result.notes.append(f"ambiguous decimal separator in {raw!r}")
    return result
