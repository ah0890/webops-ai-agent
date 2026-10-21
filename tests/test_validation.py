from app.models.extraction import Candidate, ExtractionResult
from app.services.validation import PreviousObservation, ProductContext, ValidationPolicy, validate_extraction

POLICY = ValidationPolicy(supported_currencies={"USD", "GBP", "EUR"}, max_plausible_change_pct=80)
PRODUCT = ProductContext(name="Widget Pro", url="https://shop.test/widget-pro", sku="WP-1", currency="USD")


def result(*cands: Candidate, **kw) -> ExtractionResult:
    from app.services.structured_data import choose

    r = ExtractionResult(url=PRODUCT.url, final_url=kw.pop("final_url", PRODUCT.url), candidates=list(cands), **kw)
    r.chosen = choose(r.candidates)
    return r


def cand(**kw) -> Candidate:
    base = {"source": "selector", "price": 10.0, "price_text": "$10.00", "currency": "USD",
            "availability": "in_stock", "product_name": "Widget Pro", "sku": "WP-1"}
    return Candidate(**{**base, **kw})


def codes(o):
    return {i.code for i in o.issues}


def test_clean_selector_extraction_is_verified():
    o = validate_extraction(result(cand()), PRODUCT, None, POLICY)
    assert o.status == "VERIFIED" and o.price == 10.0


def test_single_structured_source_is_likely_valid():
    o = validate_extraction(result(cand(source="json_ld")), PRODUCT, None, POLICY)
    assert o.status == "LIKELY_VALID"


def test_two_agreeing_sources_are_verified():
    o = validate_extraction(result(cand(source="json_ld"), cand(source="meta")), PRODUCT, None, POLICY)
    assert o.status == "VERIFIED"


def test_missing_price_fails():
    o = validate_extraction(result(cand(price=None, price_text=None)), PRODUCT, None, POLICY)
    assert o.status == "FAILED" and "missing_price" in codes(o)


def test_missing_price_when_out_of_stock_is_not_failure():
    o = validate_extraction(result(cand(price=None, price_text=None, availability="out_of_stock")), PRODUCT, None, POLICY)
    assert o.status == "LIKELY_VALID" and "price_missing_unavailable" in codes(o)


def test_negative_and_zero_price():
    assert validate_extraction(result(cand(price=-5)), PRODUCT, None, POLICY).status == "FAILED"
    assert validate_extraction(result(cand(price=0)), PRODUCT, None, POLICY).status == "NEEDS_REVIEW"


def test_currency_mismatch_and_change():
    o = validate_extraction(result(cand(currency="EUR")), PRODUCT, PreviousObservation(10, "USD", "in_stock"), POLICY)
    assert o.status == "NEEDS_REVIEW" and {"currency_mismatch", "currency_changed"} <= codes(o)


def test_unsupported_currency():
    p = ProductContext(name="Widget Pro", url=PRODUCT.url)
    o = validate_extraction(result(cand(currency="XYZ")), p, None, POLICY)
    assert "unsupported_currency" in codes(o)


def test_currency_assumed_from_config():
    o = validate_extraction(result(cand(currency=None)), PRODUCT, None, POLICY)
    assert o.status == "LIKELY_VALID" and o.currency == "USD" and "currency_assumed" in codes(o)


def test_implausible_change_needs_review():
    o = validate_extraction(result(cand(price=100.0)), PRODUCT, PreviousObservation(10.0, "USD", "in_stock"), POLICY)
    assert o.status == "NEEDS_REVIEW" and "implausible_change" in codes(o)


def test_sources_disagree():
    o = validate_extraction(result(cand(price=10.0), cand(source="json_ld", price=12.0)), PRODUCT, None, POLICY)
    assert "sources_disagree" in codes(o) and o.status == "NEEDS_REVIEW"


def test_wrong_product_and_sku_mismatch():
    p = ProductContext(name="Widget Pro", url=PRODUCT.url, sku="WP-1", currency="USD", expected_name_contains="Widget Pro")
    o = validate_extraction(result(cand(product_name="Gizmo Max", sku="GM-9")), p, None, POLICY)
    assert {"wrong_product", "sku_mismatch"} <= codes(o) and o.status == "NEEDS_REVIEW"


def test_sku_normalisation_ignores_punctuation_and_case():
    o = validate_extraction(result(cand(sku="wp 1")), PRODUCT, None, POLICY)
    assert "sku_mismatch" not in codes(o)


def test_redirect_detected():
    o = validate_extraction(result(cand(), final_url="https://shop.test/"), PRODUCT, None, POLICY)
    assert "redirected" in codes(o)


def test_changed_page_structure_downgrades():
    o = validate_extraction(result(cand(source="json_ld"), selector_missing=["price"]), PRODUCT, None, POLICY)
    assert "selector_missing" in codes(o) and o.status == "LIKELY_VALID"


def test_http_error_and_no_data():
    assert validate_extraction(result(cand(), http_status=404), PRODUCT, None, POLICY).status == "FAILED"
    o = validate_extraction(result(), PRODUCT, None, POLICY)
    assert o.status == "FAILED" and "no_data" in codes(o)


def test_ambiguous_price_needs_review():
    o = validate_extraction(result(cand(ambiguous=True, notes=["multiple prices"])), PRODUCT, None, POLICY)
    assert o.status == "NEEDS_REVIEW"


def test_llm_value_capped_at_likely_valid():
    o = validate_extraction(result(cand(source="llm")), PRODUCT, None, POLICY)
    assert o.status == "LIKELY_VALID" and "llm_extracted" in codes(o)
