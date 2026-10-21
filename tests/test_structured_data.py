import json

from app.models.extraction import PageSnapshot
from app.services.structured_data import candidates_from_snapshot, choose, normalise_availability


def snap(**kw) -> PageSnapshot:
    return PageSnapshot(url="https://shop.test/p", **kw)


def test_json_ld_product_with_graph_and_offer_list():
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "x"},
        {"@type": "Product", "name": "Widget", "sku": "W-1",
         "offers": [{"@type": "Offer", "price": "19.99", "priceCurrency": "USD",
                     "availability": "https://schema.org/InStock"}]},
    ]}
    c = candidates_from_snapshot(snap(json_ld=[json.dumps(ld)]), None)
    assert len(c) == 1
    assert (c[0].source, c[0].price, c[0].currency, c[0].availability, c[0].sku) == \
        ("json_ld", 19.99, "USD", "in_stock", "W-1")


def test_json_ld_aggregate_offer_range_is_ambiguous():
    ld = {"@type": "Product", "name": "W", "offers": {"@type": "AggregateOffer", "lowPrice": 10, "highPrice": 20,
                                                       "priceCurrency": "EUR"}}
    c = candidates_from_snapshot(snap(json_ld=[json.dumps(ld)]), None)[0]
    assert c.price == 10 and c.ambiguous


def test_invalid_json_ld_is_ignored():
    assert candidates_from_snapshot(snap(json_ld=["{not json"]), None) == []


def test_microdata_and_meta():
    c = candidates_from_snapshot(snap(
        microdata={"price": "25.00", "priceCurrency": "GBP", "availability": "http://schema.org/OutOfStock", "name": "W"},
        meta={"product:price:amount": "25.00", "product:price:currency": "GBP"}), None)
    assert [x.source for x in c] == ["microdata", "meta"]
    assert c[0].availability == "out_of_stock"


def test_selector_values_use_configured_currency_for_ambiguous_symbol():
    c = candidates_from_snapshot(snap(selector_values={"price": "$1,049.00", "availability": "In stock (3 left)"}), "USD")
    assert c[0].price == 1049.0 and c[0].currency == "USD" and c[0].availability == "in_stock"


def test_choose_prefers_priced_candidate_and_fills_gaps():
    cands = candidates_from_snapshot(snap(
        selector_values={"price": "£9.99"},
        json_ld=[json.dumps({"@type": "Product", "name": "Gadget", "sku": "G1", "offers": {"price": "9.99",
                             "priceCurrency": "GBP", "availability": "InStock"}})]), None)
    best = choose(cands)
    assert best.source == "selector" and best.product_name == "Gadget" and best.sku == "G1"
    assert best.availability == "in_stock"


def test_availability_text_rules():
    assert normalise_availability("Currently unavailable") == "out_of_stock"
    assert normalise_availability("Not available") == "out_of_stock"
    assert normalise_availability("Pre-order now") == "preorder"
    assert normalise_availability("Add to basket") == "in_stock"
    assert normalise_availability("Ships soon") == "unknown"
