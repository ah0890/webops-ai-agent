import asyncio
import json

import pytest
import yaml
from sqlmodel import select

from app.browser.selector_healer import Anchor, SelectorProposal, find_anchors, propose, rank
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
