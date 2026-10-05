"""Unit tests for the fail-closed caller email guard."""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("ENV", "development")
os.environ.setdefault("REQUIRE_AUTH", "false")
for _k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ALLOWED_EMAIL_DOMAINS", "ALLOWED_EMAILS", "ALLOW_ANY_EMAIL"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402

GUARD = main._require_allowed_email
EMAIL = "alice@serotonin.co"


@pytest.fixture
def caller(monkeypatch):
    monkeypatch.delenv("ALLOW_ANY_EMAIL", raising=False)
    monkeypatch.setattr(main, "_google_auth_active", lambda: True)
    monkeypatch.setattr(main.settings, "allowed_email_domains", "")
    monkeypatch.setattr(main.settings, "allowed_emails", "")

    def set_claims(**claims):
        monkeypatch.setattr(main, "_caller_claims", lambda: dict(claims))

    set_claims(email=EMAIL, email_verified=True)
    return set_claims


def _denied(result):
    assert result is not None and result["status"] == "error"
    assert EMAIL not in result["message"] and "alice" not in result["message"]
    return result["message"]


def test_skipped_without_google_auth(monkeypatch):
    monkeypatch.setattr(main, "_google_auth_active", lambda: False)
    assert GUARD() is None


def test_empty_lists_deny(caller):
    msg = _denied(GUARD())
    assert msg == "Access denied: ALLOWED_EMAIL_DOMAINS is not configured on this server."


def test_allow_any_email_allows(caller, monkeypatch):
    monkeypatch.setenv("ALLOW_ANY_EMAIL", "true")
    assert GUARD() is None


def test_missing_email_denies(caller, monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_email_domains", "serotonin.co")
    caller(email_verified=True)
    _denied(GUARD())


@pytest.mark.parametrize("verified", [False, "false", "False", None, "", "0", "yes"])
def test_unverified_denies(caller, monkeypatch, verified):
    monkeypatch.setattr(main.settings, "allowed_email_domains", "serotonin.co")
    caller(email=EMAIL, email_verified=verified)
    _denied(GUARD())


@pytest.mark.parametrize("verified", [True, "true", "TRUE"])
def test_verified_variants_allow(caller, monkeypatch, verified):
    monkeypatch.setattr(main.settings, "allowed_email_domains", "serotonin.co")
    caller(email=EMAIL, email_verified=verified)
    assert GUARD() is None


def test_wrong_domain_denies(caller, monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_email_domains", "example.com")
    _denied(GUARD())


def test_subdomain_is_not_exact_domain(caller, monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_email_domains", "co")
    _denied(GUARD())


def test_right_domain_allows(caller, monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_email_domains", "example.com, Serotonin.co")
    assert GUARD() is None


def test_allowed_email_allows_even_when_domain_list_excludes_it(caller, monkeypatch):
    # OR semantics: email list match is enough when both lists are set.
    monkeypatch.setattr(main.settings, "allowed_email_domains", "example.com")
    monkeypatch.setattr(main.settings, "allowed_emails", "alice@serotonin.co")
    assert GUARD() is None


def test_email_not_in_either_list_denies(caller, monkeypatch):
    monkeypatch.setattr(main.settings, "allowed_email_domains", "example.com")
    monkeypatch.setattr(main.settings, "allowed_emails", "bob@serotonin.co")
    _denied(GUARD())
