"""Free-mail and company-domain rules (gap #28): whole-domain free-mail list, registrable domains."""
import pytest

from app.services import crm
from app.utils.company_domain import bare_domain, is_valid_domain, registrable_domain
from app.utils.free_mail import FREE_MAIL_DOMAINS, is_free_mail_domain


@pytest.mark.parametrize("domain", [
    "gmail.com", "GMail.com", "orange.fr", "t-online.de", "free.fr", "btinternet.com", "att.net",
    "yahoo.co.jp", "hotmail.it", "outlook.com.br", "live.co.uk", "web.de", "mail.ru", "us.att.net",
])
def test_free_mail_domains(domain):
    assert is_free_mail_domain(domain)
    assert crm.is_personal_domain(domain)


@pytest.mark.parametrize("domain", [
    "mail.acme.com", "acme.com", "live.acme.com", "me.acme.io", "inbox.acme.co.uk", "acme.fr", "orange-labs.com",
])
def test_company_domains_are_not_free_mail(domain):
    assert not is_free_mail_domain(domain)
    assert not crm.is_personal_domain(domain)


def test_free_mail_list_size_and_blank():
    assert len(FREE_MAIL_DOMAINS) >= 90
    assert not is_free_mail_domain(None) and not is_free_mail_domain("")
    assert crm.is_personal_domain(None)  # no domain: never a company


@pytest.mark.parametrize("domain,expected", [
    ("us.acme.com", "acme.com"),
    ("acme.com", "acme.com"),
    ("a.b.acme.com", "acme.com"),
    ("hr.acme.co.uk", "acme.co.uk"),
    ("acme.co.uk", "acme.co.uk"),
    ("eu.acme.com.au", "acme.com.au"),
    ("x.acme.co.jp", "acme.co.jp"),
    ("shop.acme.github.io", "acme.github.io"),
    ("team1.example.com", "team1.example.com"),  # reserved test domain keeps its label
    ("a.b.acme.example", "acme.example"),
    ("co.uk", "co.uk"),
])
def test_registrable_domain(domain, expected):
    assert registrable_domain(domain) == expected


@pytest.mark.parametrize("value,expected", [
    ("https://www.Acme.com/about?x=1", "acme.com"),
    ("http://shop.acme.com:8080/", "acme.com"),
    ("WWW.HR.ACME.CO.UK", "acme.co.uk"),
    ("ceo@us.acme.com", "acme.com"),
    ("not a domain", "not a domain"),  # invalid text is returned uncollapsed for the caller to check
    ("", None),
    (None, None),
])
def test_clean_domain(value, expected):
    assert crm.clean_domain(value) == expected


@pytest.mark.parametrize("domain,ok", [
    ("acme.com", True), ("acme-corp.co.uk", True), ("xn--bcher-kva.de", True),
    ("acme", False), ("acme..com", False), ("-acme.com", False), ("acme_corp.com", False),
    ("acme.c0m", False), ("ac me.com", False), ("a" * 64 + ".com", False),
])
def test_is_valid_domain(domain, ok):
    assert is_valid_domain(domain) is ok


def test_bare_domain():
    assert bare_domain("https://user.acme.com/path#frag") == "user.acme.com"
    assert bare_domain("  ") is None


@pytest.mark.parametrize("email,expected", [
    ("jo@us.acme.com", "acme.com"),
    ("jo@mail.acme.com", "acme.com"),
    ("jo@hr.acme.co.uk", "acme.co.uk"),
    ("jo@gmail.com", None),
    ("jo@orange.fr", None),
    ("jo@yahoo.co.in", None),
    ("broken", None),
    ("jo@bad_domain.com", None),
])
def test_company_domain_from_email(email, expected):
    assert crm.company_domain_from_email(email) == expected
