import asyncio
import json

import pytest
import yaml
from sqlmodel import select

from app.browser.selector_healer import (
    Anchor,
    LLMHealBudget,
    SelectorProposal,
    find_anchors,
    llm_anchors,
    propose,
    rank,
    unanchored_fields,
)
from app.config.schema import SelectorSet, WebsiteConfig, WebsitesFile
from app.database.models import Product, SelectorSuggestion, Website
from app.models.extraction import Candidate, ExtractionResult
from app.services import selector_repair
from app.services.config_sync import sync_websites


def extraction(*cands, missing=()):
    return ExtractionResult(url="https://shop.test/p", candidates=list(cands), selector_missing=list(missing))


def test_anchor_from_structured_data_when_selector_missing():
    ex = extraction(Candidate(source="json_ld", price=19.99), Candidate(source="meta", price=19.99), missing=["price"])
    [(a, old)] = find_anchors(ex, SelectorSet(price=".old"), previous_price=None)
    assert (a.value, a.source, a.strength, old) == (19.99, "json_ld", "strong", ".old")


def test_anchor_when_selector_returns_wrong_value():
    ex = extraction(Candidate(source="selector", price=5.0), Candidate(source="json_ld", price=19.99))
    assert find_anchors(ex, SelectorSet(price=".promo"), None)[0][0].value == 19.99


def test_no_anchor_when_structured_sources_disagree_or_selector_fine():
    bad = extraction(Candidate(source="json_ld", price=10.0), Candidate(source="meta", price=12.0), missing=["price"])
    assert find_anchors(bad, SelectorSet(price=".x"), None) == []
    ok = extraction(Candidate(source="selector", price=10.0), Candidate(source="json_ld", price=10.0))
    assert find_anchors(ok, SelectorSet(price=".x"), None) == []
    assert find_anchors(ok, None, None) == []


def test_weak_anchor_from_previous_price_only_when_selector_missing():
    ex = extraction(missing=["price"])
    [(a, _)] = find_anchors(ex, SelectorSet(price=".x"), previous_price=42.0)
    assert a.strength == "weak" and a.source == "previous_observation"


RAW = [
    {"text": "$24.99", "font_size": 13, "struck": True,
     "selectors": [{"kind": "class", "selector": "span.was", "matches": 1, "selects_element": True}]},
    {"text": "$19.99", "font_size": 28, "struck": False,
     "selectors": [{"kind": "class", "selector": "span.amount", "matches": 3, "selects_element": True},
                   {"kind": "scoped", "selector": "div.buybox span.amount", "matches": 1, "selects_element": True},
                   {"kind": "data", "selector": '[data-testid="price"]', "matches": 1, "selects_element": True}]},
    {"text": "$19.99", "font_size": 12, "struck": False,
     "selectors": [{"kind": "class", "selector": "span.related-price", "matches": 1, "selects_element": True}]},
]


def test_rank_prefers_stable_unique_selector_and_ignores_struck_prices():
    p = rank(RAW, Anchor("price", 19.99, "json_ld", "strong"), ".old", "USD")
    assert p.selector == '[data-testid="price"]' and p.kind == "data"
    assert "span.was" not in [p.selector, *p.alternatives]
    assert "div.buybox span.amount" in p.alternatives
    weak = rank(RAW, Anchor("price", 19.99, "previous_observation", "weak"), ".old", "USD")
    assert weak.confidence < p.confidence


def test_rank_returns_none_without_matching_value():
    assert rank(RAW, Anchor("price", 7.5, "json_ld", "strong"), None, "USD") is None


@pytest.fixture()
def site_with_suggestion(session):
    site = Website(name="Shop", base_url="https://shop.test", allowed_domains=["shop.test"],
                   config={"selectors": {"price": ".old"}})
    session.add(site)
    session.flush()
    prod = Product(website_id=site.id, name="P", url="https://shop.test/p")
    session.add(prod)
    session.commit()
    prop = SelectorProposal(field="price", old_selector=".old", selector=".new", kind="class", sample_text="$1",
                            anchor_value="1.0", anchor_source="json_ld", confidence=0.8, alternatives=[".alt"])
    s, new = selector_repair.record(session, website=site, product=prod, run_id=None, proposal=prop)  # type: ignore[arg-type]
    s2, new2 = selector_repair.record(session, website=site, product=prod, run_id=None, proposal=prop)  # type: ignore[arg-type]
    assert new and not new2 and s2.id == s.id and s2.occurrences == 2
    return site, prod, s


def test_approve_updates_db_and_writes_override(session, site_with_suggestion, tmp_path):
    site, _, s = site_with_suggestion
    f = tmp_path / "ov.yaml"
    with pytest.raises(ValueError):
        selector_repair.approve(session, s.id, f, selector=".not-offered")
    selector_repair.approve(session, s.id, f, selector=".alt")
    session.refresh(site)
    assert site.config["selectors"]["price"] == ".alt"
    ov = yaml.safe_load(f.read_text(encoding="utf-8"))["overrides"]
    assert ov == [{**ov[0], "website": "Shop", "field": "price", "selector": ".alt", "replaces": ".old"}]
    with pytest.raises(ValueError):
        selector_repair.approve(session, s.id, f)


def test_reject(session, site_with_suggestion):
    _, _, s = site_with_suggestion
    assert selector_repair.reject(session, s.id).status == "rejected"


def test_override_applies_only_while_yaml_unchanged(session, tmp_path):
    f = tmp_path / "ov.yaml"
    f.write_text(yaml.safe_dump({"overrides": [
        {"website": "Shop", "product_url": None, "field": "price", "selector": ".fixed", "replaces": ".old"}]}),
        encoding="utf-8")

    def cfg(price_sel):
        return WebsitesFile(websites=[WebsiteConfig(name="Shop", base_url="https://shop.test",
                                                    selectors=SelectorSet(price=price_sel),
                                                    products=[{"name": "P", "url": "https://shop.test/p"}])])

    sync_websites(session, cfg(".old"), f)
    assert session.exec(select(Website)).one().config["selectors"]["price"] == ".fixed"
    sync_websites(session, cfg(".hand-edited"), f)  # user changed YAML afterwards: their edit wins
    assert session.exec(select(Website)).one().config["selectors"]["price"] == ".hand-edited"


def test_suggestions_api(session, site_with_suggestion, settings, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    monkeypatch.setattr(settings, "selector_overrides_file", tmp_path / "ov.yaml")
    with TestClient(create_app()) as c:
        items = c.get("/api/selector-suggestions").json()
        assert items[0]["proposed_selector"] == ".new" and items[0]["website"] == "Shop"
        assert c.get("/api/dashboard/overview").json()["selector_repairs_pending"] == 1
        assert c.post(f"/api/selector-suggestions/{items[0]['id']}/approve").json()["status"] == "approved"
        assert c.post(f"/api/selector-suggestions/{items[0]['id']}/reject").status_code == 409
        assert c.post("/api/selector-suggestions/9999/approve").status_code == 404
    session.expire_all()  # the API wrote through its own session
    assert session.exec(select(SelectorSuggestion)).one().status == "approved"


@pytest.mark.browser
def test_propose_on_real_dom():
    html = """<html><head><script type="application/ld+json">%s</script></head><body>
      <div class="col-md-6 buybox"><h1>Widget</h1><del class="was">$24.99</del>
        <span class="amount current" data-testid="product-price">$19.99</span></div>
      <aside><span class="amount">$5.00</span></aside></body></html>""".replace(
        "%s", json.dumps({"@type": "Product", "name": "Widget", "offers": {"price": "19.99", "priceCurrency": "USD"}}))

    async def go():
        from playwright.async_api import async_playwright

        from app.browser.extractor import DataExtractor
        from app.config.schema import AvailabilityRules

        async with async_playwright() as p:
            try:
                b = await p.chromium.launch()
            except Exception as e:
                pytest.skip(f"chromium unavailable: {e}")
            page = await b.new_page()
            await page.set_content(html)
            sel = SelectorSet(price=".price-box .price")  # site was redesigned; this no longer exists
            ex = await DataExtractor().extract(page, requested_url="about:blank", http_status=200, selectors=sel,
                                               rules=AvailabilityRules(), expected_currency="USD", product_name="Widget")
            props = await propose(page, ex, sel, None, "USD")
            # The proposal must really select the $19.99 element on this page.
            text = await page.locator(props[0].selector).first.inner_text() if props else None
            await b.close()
            return props, text

    props, text = asyncio.run(go())
    assert len(props) == 1 and props[0].anchor_source == "json_ld"
    assert props[0].selector == '[data-testid="product-price"]' and text == "$19.99"
    assert all("col-md" not in s for s in [props[0].selector, *props[0].alternatives])


class FakeProvider:
    def __init__(self, **out):
        self.out, self.calls = out, 0

    def structured(self, system, prompt, schema, max_tokens=8000):
        self.calls += 1
        return schema(**self.out)


PAGE_TEXT = "Shop\nAcme Steel Widget 40mm\n$19.99\nIn stock (12 available)\nRelated: Acme Bolt"


def test_unanchored_fields_only_for_broken_selectors_without_structured_anchor():
    sel = SelectorSet(product_name="h1.old", availability=".stock-old", price=".p")
    ex = extraction(Candidate(source="selector", price=1.0), missing=["product_name", "availability"])
    assert unanchored_fields(ex, sel, find_anchors(ex, sel, None)) == ["product_name", "availability"]
    with_name = extraction(Candidate(source="json_ld", product_name="Widget"), missing=["product_name", "availability"])
    assert unanchored_fields(with_name, sel, find_anchors(with_name, sel, None)) == ["availability"]
    assert unanchored_fields(extraction(), sel, []) == []


def test_llm_anchors_pass_evidence_checks():
    llm = FakeProvider(product_name="Acme Steel Widget 40mm", availability_text="In stock (12 available)")
    out = llm_anchors(llm, PAGE_TEXT, ["product_name", "availability"], product_name="Acme widget 40mm",
                      extraction=extraction(), rules=None)
    assert [(a.field, a.source, a.strength) for a in out] == [
        ("product_name", "llm", "medium"), ("availability", "llm", "medium")]


@pytest.mark.parametrize("out, reason", [
    ({"product_name": "Acme Widget Pro"}, "name not on page"),
    ({"product_name": "Related: Acme Bolt"}, "on page but not related to the configured product"),
    ({"availability_text": "Ships tomorrow"}, "not on page"),
    ({"availability_text": "Shop"}, "on page but not availability wording"),
])
def test_llm_anchors_rejected(out, reason):
    name = "Steel widget 40mm" if "product_name" in out else "Acme Steel Widget 40mm"
    fields = ["product_name"] if "product_name" in out else ["availability"]
    assert llm_anchors(FakeProvider(**out), PAGE_TEXT, fields, product_name=name.replace("Acme ", ""),
                       extraction=extraction(), rules=None) == [], reason


def test_llm_availability_must_agree_with_structured_data():
    ex = extraction(Candidate(source="json_ld", availability="out_of_stock"))
    assert llm_anchors(FakeProvider(availability_text="In stock (12 available)"), PAGE_TEXT, ["availability"],
                       product_name="x", extraction=ex, rules=None) == []


def test_rank_text_prefers_tightest_element():
    raw = [{"text": "Price $19.99 In stock (12 available) Add to basket", "font_size": 14, "struck": False,
            "selectors": [{"kind": "class", "selector": "div.buybox", "matches": 1, "selects_element": True}]},
           {"text": "In stock (12 available)", "font_size": 14, "struck": False,
            "selectors": [{"kind": "class", "selector": "p.availability", "matches": 1, "selects_element": True}]}]
    p = rank(raw, Anchor("availability", "In stock (12 available)", "llm", "medium"), ".stock-old", None)
    assert p.selector == "p.availability" and p.alternatives == ["div.buybox"]
    strong = rank(raw, Anchor("availability", "In stock (12 available)", "structured", "strong"), None, None)
    assert p.confidence < strong.confidence


@pytest.mark.browser
def test_llm_anchor_heals_availability_on_real_dom():
    html = """<html><body><div class="product_main"><h1>Acme Steel Widget 40mm</h1>
      <p class="price_color">$19.99</p>
      <p class="instock availability"><i class="icon-ok"></i> In stock (12 available)</p></div>
      <aside><p>Related: Acme Bolt</p></aside></body></html>"""

    async def go():
        from playwright.async_api import async_playwright

        from app.browser.extractor import DataExtractor
        from app.config.schema import AvailabilityRules

        async with async_playwright() as p:
            try:
                b = await p.chromium.launch()
            except Exception as e:
                pytest.skip(f"chromium unavailable: {e}")
            page = await b.new_page()
            await page.set_content(html)
            sel = SelectorSet(price="p.price_color", availability="#stock-status")  # redesign removed the id
            ex = await DataExtractor().extract(page, requested_url="about:blank", http_status=200, selectors=sel,
                                               rules=AvailabilityRules(), expected_currency="USD", product_name="Widget")
            llm = FakeProvider(availability_text="In stock (12 available)")
            budget = LLMHealBudget(remaining=2)
            props = await propose(page, ex, sel, None, "USD", provider=llm, budget=budget,
                                  product_name="Acme Steel Widget 40mm", rules=AvailabilityRules())
            again = await propose(page, ex, sel, None, "USD", provider=llm, budget=budget,
                                  product_name="Acme Steel Widget 40mm", rules=AvailabilityRules())
            text = await page.locator(props[0].selector).first.inner_text() if props else None
            await b.close()
            return props, again, text, llm.calls, budget

    props, again, text, calls, budget = asyncio.run(go())
    assert len(props) == 1 and props[0].field == "availability" and props[0].anchor_source == "llm"
    assert text.strip() == "In stock (12 available)"
    assert again == [] and calls == 1 and budget.satisfied == {"availability"}  # no second LLM call for the field
