import logging

import pytest
from pydantic import ValidationError

from app.config.schema import LoginConfig, WebsiteConfig
from app.models.actions import parse_action, parse_actions
from app.security.secrets import MissingCredential, SecretMaskingFilter, mask, register_secret, resolve_env_refs
from app.security.url_guard import UrlGuard, UrlNotAllowed


@pytest.mark.parametrize("data", [
    {"action": "goto", "url": "https://shop.test/p/1"},
    {"action": "click", "selector": "#buy", "optional": True},
    {"action": "fill", "selector": "#q", "value": "abc"},
    {"action": "select", "selector": "select#size", "value": "M"},
    {"action": "scroll", "to": "bottom"},
    {"action": "wait", "milliseconds": 500},
    {"action": "extract", "fields": ["price", "availability"]},
    {"action": "screenshot", "name": "before"},
    {"action": "download", "selector": "a.invoice"},
    {"action": "navigate_back"},
    {"action": "new_tab", "url": "https://shop.test/x"},
    {"action": "switch_tab", "index": 1},
    {"action": "press", "key": "Escape"},
])
def test_valid_actions(data):
    assert parse_action(data).action == data["action"]


@pytest.mark.parametrize("data", [
    {"action": "goto", "url": "javascript:alert(1)"},
    {"action": "goto", "url": "file:///etc/passwd"},
    {"action": "click", "selector": "   "},
    {"action": "click", "selector": "javascript:void(0)"},
    {"action": "extract", "fields": ["password"]},
    {"action": "screenshot", "name": "../../evil"},
    {"action": "wait", "milliseconds": 999999},
    {"action": "eval", "script": "1"},
    {"action": "goto", "url": "https://x.test", "unexpected": 1},
])
def test_invalid_actions_rejected(data):
    with pytest.raises(ValidationError):
        parse_action(data)


def test_parse_action_list():
    assert len(parse_actions([{"action": "scroll"}, {"action": "press", "key": "End"}])) == 2


def test_url_guard():
    g = UrlGuard(["shop.test"])
    assert g.check("https://shop.test/a") and g.check("https://www.shop.test/a")
    for bad in ["https://evil.test/", "https://shop.test.evil.com/", "ftp://shop.test/", "https://user:pw@shop.test/",
                "http://127.0.0.1/", "http://localhost:8000/", "http://169.254.169.254/latest"]:
        with pytest.raises(UrlNotAllowed):
            g.check(bad)


def test_url_guard_explicit_local_host_allowed():
    assert UrlGuard(["localhost"]).is_allowed("http://localhost:8080/p")


def test_website_config_rejects_product_outside_allowlist():
    with pytest.raises(ValidationError):
        WebsiteConfig(name="A", base_url="https://a.test", products=[{"name": "x", "url": "https://b.test/x"}])


def test_login_secret_must_reference_env():
    with pytest.raises(ValidationError):
        LoginConfig(url="https://a.test/login",
                    steps=[{"action": "fill", "selector": "#pw", "value": "hunter2", "secret": True}])
    LoginConfig(url="https://a.test/login", steps=[{"action": "fill", "selector": "#pw", "value": "${A_PW}", "secret": True}])


def test_env_refs_and_masking(monkeypatch):
    monkeypatch.setenv("SUPPLIER_PW", "s3cret-value")
    assert resolve_env_refs("pw=${SUPPLIER_PW}") == "pw=s3cret-value"
    assert "s3cret-value" not in mask("login with s3cret-value failed")
    with pytest.raises(MissingCredential):
        resolve_env_refs("${DOES_NOT_EXIST_123}")


def test_api_key_patterns_masked():
    assert "sk-ant-abcdefgh123" not in mask("key sk-ant-abcdefgh123 leaked")
    assert mask("password=hunter22") == "password=****"


def test_logging_filter_masks():
    register_secret("topsecret99")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "value %s", ("topsecret99",), None)
    SecretMaskingFilter().filter(rec)
    assert "topsecret99" not in rec.getMessage()
