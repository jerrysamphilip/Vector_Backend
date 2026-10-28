"""a1b: pure duplicate-matching rules (app/domain/duplicates.py) and list rules (app/domain/lists.py)."""

import pytest

from app.domain import duplicates as d
from app.domain import lists as L
from app.utils.free_mail import is_free_mail_domain


def cand(pid, first=None, last=None, email=None, company=None, account=None, phone=None, mobile=None, linkedin=None):
    return d.Candidate(pid, first, last, email, company, account, phone, mobile, linkedin)


def pairs(*cands):
    return {(m.contact_a, m.contact_b): m for m in d.find_pairs(list(cands), is_free_mail_domain)}


# ── normalisation ──────────────────────────────────────────────────

def test_fold_removes_accents_case_and_punctuation():
    assert d.fold("  José-María  O'Neil ") == "jose maria o neil"
    assert d.fold(None) == ""


def test_email_key_gmail_dots_tags_and_case():
    assert d.email_key("Jane.Doe+news@Gmail.com", is_free_mail_domain) == "janedoe@gmail.com"
    assert d.email_key("jane.doe@googlemail.com") == "janedoe@gmail.com"
    assert d.email_key("jane.doe@acme.com", is_free_mail_domain) == "jane.doe@acme.com"  # dots matter at companies
    assert d.email_key("no-at-sign") is None


def test_corporate_domain_ignores_free_mail():
    assert d.corporate_domain("a@acme.io", is_free_mail_domain) == "acme.io"
    assert d.corporate_domain("a@gmail.com", is_free_mail_domain) is None
    assert d.corporate_domain("a@orange.fr", is_free_mail_domain) is None


def test_phone_and_linkedin_keys():
    assert d.phone_key("+1 (415) 555-0100") == "4155550100"
    assert d.phone_key("123") is None
    assert d.linkedin_key("https://www.LinkedIn.com/in/jane-doe-123/?trk=x") == "jane-doe-123"
    assert d.linkedin_key("https://linkedin.com/company/acme") is None


# ── names ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("a, b, why", [
    (("Bob", "Smith"), ("Robert", "Smith"), "nickname of the same first name"),
    (("J.", "Smith"), ("John", "Smith"), "first initial matches"),
    (("Jonathan", "Smith"), ("Johnathan", "Smith"), "similar spelling"),
    (("José", "García"), ("Jose", "Garcia"), "same name"),
    (("Smith", "John"), ("John", "Smith"), "first and last name swapped"),
])
def test_name_match_variants(a, b, why):
    score, reason = d.name_match(*a, *b)
    assert score >= d.MIN_SCORE and reason == why


def test_name_match_rejects_different_people_and_blanks():
    assert d.name_match("Anna", "Lee", "Bob", "Lee")[0] == 0
    assert d.name_match("Anna", None, "Anna", "Lee")[0] == 0
    assert d.name_match("Jon", "Smith", "John", "Smyth")[0] > 0  # typo in the last name


# ── pair finding ───────────────────────────────────────────────────

def test_same_mailbox_is_high():
    found = pairs(cand("1", email="Jane.Doe+a@gmail.com"), cand("2", email="janedoe@googlemail.com"))
    m = found[("1", "2")]
    assert m.score == 100 and m.confidence == "HIGH" and m.reasons == [d.REASON_EMAIL]


def test_names_only_count_inside_the_same_company_or_domain():
    found = pairs(cand("1", "Bob", "Smith", "bob@acme.com", "Acme"),
                  cand("2", "Robert", "Smith", "rsmith@acme.com", "ACME"),
                  cand("3", "Robert", "Smith", "robert@other.org", "Other"),
                  cand("4", "Robert", "Smith", "r@gmail.com"))
    assert set(found) == {("1", "2")}
    assert found[("1", "2")].reasons[0].startswith("Similar name at the same company (nickname")


def test_same_account_id_is_a_company_block_even_with_different_spellings():
    found = pairs(cand("a", "Ann", "Lee", "ann@x.com", "X Inc", "acc1"), cand("b", "Ann", "Lee", "al@y.com", "X", "acc1"))
    assert found[("a", "b")].reasons == ["Same name at the same company"]


def test_linkedin_and_phone_with_switchboard_cap():
    many = [cand(str(i), f"P{i}", "Q", phone="+1 212 555 0000") for i in range(d.SWITCHBOARD_CAP + 1)]
    assert not pairs(*many)  # a switchboard number, not a person
    found = pairs(cand("x", "Ann", "Lee", linkedin="https://linkedin.com/in/annlee"),
                  cand("y", "A.", "Lee", linkedin="linkedin.com/in/annlee/"),
                  cand("z", "Zed", "Q", phone="02079460000"), cand("w", "Zed", "Q", mobile="+44 20 7946 0000"),
                  cand("v", "Other", "Person", phone="02079460000"))
    assert found[("x", "y")].reasons == [d.REASON_LINKEDIN] and found[("x", "y")].confidence == "HIGH"
    assert found[("w", "z")].reasons == [d.REASON_PHONE_NAME]
    assert found[("v", "z")].reasons == [d.REASON_PHONE] and found[("v", "z")].confidence == "MEDIUM"


def test_linkedin_groups_over_the_cap_are_ignored():
    many = [cand(str(i), linkedin="linkedin.com/in/shared") for i in range(d.SWITCHBOARD_CAP + 1)]
    assert not pairs(*many)


def test_several_signals_raise_the_score_and_pairs_are_ordered():
    found = d.find_pairs([cand("b", "Ann", "Lee", "ann@acme.com", "Acme", phone="4155550100"),
                          cand("a", "Ann", "Lee", "ann.lee@acme.com", "Acme", phone="415-555-0100")])
    assert len(found) == 1
    m = found[0]
    assert (m.contact_a, m.contact_b) == ("a", "b") and m.score == 100 and len(m.reasons) == 2


def test_pair_match_keeps_strongest_reason_per_kind():
    m = d.PairMatch("a", "b")
    m.add("phone", 60, d.REASON_PHONE)
    m.add("phone", 90, d.REASON_PHONE_NAME)
    assert m.reasons == [d.REASON_PHONE_NAME] and m.score == 90
    m.add("email", 100, d.REASON_EMAIL)
    assert m.score == 100 and m.confidence == "HIGH"
    assert d.confidence_for(84) == "MEDIUM"


def test_merge_preview_helpers():
    assert d.default_choice([("s", None), ("x", "a@b.c")]) == "x"
    assert d.default_choice([("s", "keep"), ("x", "other")]) == "s"
    assert d.default_choice([("s", ""), ("x", None)]) is None
    assert d.values_differ(["A", "a ", None]) is False
    assert d.values_differ(["A", "B"]) is True


# ── lists ──────────────────────────────────────────────────────────

def test_list_rules():
    assert L.require_name("  Q4 ") == "Q4"
    with pytest.raises(L.ListRuleError, match=L.NAME_REQUIRED):
        L.require_name("   ")
    assert L.list_type_of(None) == "STATIC" and L.list_type_of("active") == "ACTIVE"
    with pytest.raises(L.ListRuleError, match="STATIC or ACTIVE"):
        L.list_type_of("smart")
    with pytest.raises(L.ListRuleError, match="at least one filter"):
        L.require_conditions({"op": "AND", "conditions": []})
    with pytest.raises(L.ListRuleError, match="update automatically"):
        L.require_static("ACTIVE")
    assert L.new_members(["a", "b", "a", "c", "z"], {"a", "b", "c"}, {"b"}) == ["a", "c"]
    assert L.ListFilter(q="  ").search is None and L.ListFilter(q=" x ").search == "x"


def test_prospect_list_sort_and_revalidation_helpers():
    sort = L.prospect_list_sort("list_name", "asc")
    assert sort.field == "list_name" and not sort.descending
    assert L.prospect_list_sort(None, None).descending
    with pytest.raises(L.ListRuleError):
        L.prospect_list_sort("nope", "asc")
    assert L.duplicate_emails(["A@x.com", "a@x.com", None, "b@x.com"]) == {"a@x.com"}
    outcome = L.RevalidationOutcome()
    outcome.warn("w")
    outcome.fail("f")
    outcome.warn("w2")
    assert outcome.status == "invalid" and outcome.issues == ["w", "f", "w2"]
    assert L.unsubscribed_issue(None) == "Globally unsubscribed"
    assert L.unsubscribed_issue("bounce") == "Globally unsubscribed (bounce)"
