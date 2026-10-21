"""Browser-layer tests on local HTML via page.set_content — real Chromium, no network."""

import asyncio
import json

import pytest

from app.browser.extractor import DataExtractor
from app.browser.observer import PageObserver
from app.config.schema import AvailabilityRules, SelectorSet

pytestmark = pytest.mark.browser

PRODUCT_HTML = """<html><head><title>Widget Pro</title>
<meta property="product:price:amount" content="1299.00"><meta property="product:price:currency" content="USD">
<script type="application/ld+json">{}</script></head>
<body><div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Widget Pro</h1>
<span class="price">$1,299.00</span><meta itemprop="price" content="1299.00"><meta itemprop="priceCurrency" content="USD">
<link itemprop="availability" href="https://schema.org/InStock"><p class="stock">In stock</p></div></body></html>
""".format(json.dumps({"@type": "Product", "name": "Widget Pro", "sku": "WP-1",
                  "offers": {"price": "1299.00", "priceCurrency": "USD", "availability": "https://schema.org/InStock"}}))


def _run(coro):
    return asyncio.run(coro)


async def _with_page(html, fn):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            b = await p.chromium.launch()
        except Exception as e:  # chromium not installed
            pytest.skip(f"chromium unavailable: {e}")
        page = await b.new_page()
        await page.set_content(html)
        try:
            return await fn(page)
        finally:
            await b.close()


def test_extractor_all_strategies_agree():
    async def fn(page):
        return await DataExtractor().extract(
            page, requested_url="about:blank", http_status=200,
            selectors=SelectorSet(price=".price", availability=".stock", product_name="h1"),
            rules=AvailabilityRules(), expected_currency="USD", product_name="Widget Pro")

    r = _run(_with_page(PRODUCT_HTML, fn))
    assert {c.source for c in r.candidates} == {"selector", "json_ld", "microdata", "meta"}
    assert all(c.price == 1299.0 for c in r.candidates)
    assert r.chosen.source == "selector" and r.chosen.sku == "WP-1" and r.chosen.availability == "in_stock"


def test_extractor_reports_missing_configured_selector_and_attribute_selector():
    async def fn(page):
        return await DataExtractor().extract(
            page, requested_url="about:blank", http_status=200,
            selectors=SelectorSet(price="meta[itemprop=price]@content", availability=".gone"),
            rules=AvailabilityRules(), expected_currency="USD", product_name="Widget Pro")

    r = _run(_with_page(PRODUCT_HTML, fn))
    assert r.selector_missing == ["availability"]
    assert r.candidates[0].source == "selector" and r.candidates[0].price == 1299.0


def test_observer_detects_challenge_and_login_wall():
    challenge = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"
    login = "<html><body><form><input type=text><input type=password></form></body></html>"
    s1 = _run(_with_page(challenge, lambda p: PageObserver().observe(p, 403)))
    s2 = _run(_with_page(login, lambda p: PageObserver().observe(p, 200)))
    assert s1.captcha and s1.blocked
    assert s2.login_wall and not s2.captcha
