"""Pure helpers of the campaign wizard (schedule preview, regeneration prompt, body clean-up)."""
from datetime import datetime

from app.prompts.regeneration_prompts import build_regeneration_prompt
from app.services.campaign_wizard_service import build_schedule_preview
from app.services.campaign_wizard_template_service import prepare_regenerated_body, strip_trailing_signature

BASE = datetime(2031, 3, 3, 12, 0)  # a Monday


def _prompt(**kw):
    args = dict(field="subject", current_subject="Old subject", campaign_description="", persona_type="CFO",
                product_name="Hub services", cta_link=None, custom_instruction=None, creative_email=False,
                step_number=1)
    args.update(kw)
    return build_regeneration_prompt(**args)


def test_schedule_preview_accumulates_wait_days_and_hides_step_one_wait():
    out = build_schedule_preview([(1, 2), (2, 3)], "NY", base_time=BASE)
    assert [s["step_number"] for s in out] == [1, 2]
    assert out[0]["wait_days"] == 0 and out[1]["wait_days"] == 3
    first, second = (datetime.fromisoformat(s["send_date"][:-1]) for s in out)
    assert all(s["send_date"].endswith("Z") for s in out)
    assert (second - first).days >= 3  # cumulative: step 2 is 2 + 3 days after the base
    assert out[0]["formatted"] == first.strftime("%a, %b %d")
    assert first.weekday() < 5 and second.weekday() < 5  # business days only


def test_schedule_preview_dates_are_stable_to_the_minute():
    steps = [(1, 0), (2, 2), (3, 4)]

    def minutes(out):
        return [(s["formatted"], s["send_date"][:16]) for s in out]

    assert minutes(build_schedule_preview(steps, "CA", BASE)) == minutes(build_schedule_preview(steps, "CA", BASE))


def test_cold_subject_prompt_mentions_product_and_current_subject():
    p = _prompt()
    assert p.startswith("Generate a new subject line for a <<SENDER_COMPANY>> cold outreach email.")
    assert "Product: Hub services" in p and "Current subject: Old subject" in p
    assert "CREATIVE_EMAIL_MODE" not in p and "USER_FEEDBACK" not in p


def test_conference_context_switches_to_conference_edition():
    p = _prompt(campaign_description="Meet us at the HLTH 25 conference in Las Vegas")
    assert "conference/in-person outreach email" in p and "Email step: 1" in p


def test_creative_mode_only_applies_to_subjects_and_feedback_is_appended():
    assert "CREATIVE_EMAIL_MODE" in _prompt(creative_email=True)
    body = _prompt(field="body", creative_email=True, custom_instruction="make it shorter")
    assert "CREATIVE_EMAIL_MODE" not in body
    assert body.endswith("USER_FEEDBACK: The user has requested: 'make it shorter'. "
                         "Apply this preference to this specific rewrite if reasonable.")


def test_body_prompt_cta_line_depends_on_link():
    with_link = _prompt(field="body", cta_link="https://cal.example.com/me", step_number=2)
    assert "CTA Link: https://cal.example.com/me" in with_link and "(followup_1)" in with_link
    assert "No CTA link. Do NOT include a meeting ask" in _prompt(field="body")


def test_strip_trailing_signature_removes_every_sign_off():
    assert strip_trailing_signature("Hi\n\nBest regards,\n{{your_name}}") == "Hi"
    assert strip_trailing_signature("Hi\nThanks,\nCheers,\n[your_name]\n\nRegards,\n{{your_name}}") == "Hi"
    assert strip_trailing_signature("Hi there") == "Hi there"


def test_regenerated_body_ends_with_exactly_one_signature():
    content, body = prepare_regenerated_body("Hi {{first_name}},\n\nGreat call.\n\nSincerely,\n{{your_name}}",
                                             "https://cal.example.com/x")
    assert content == "Hi {{first_name}},\n\nGreat call.\n\nRegards,\n{{your_name}}"
    assert content.count("your_name") == 1
    assert body  # finalized (HTML-formatted) template body
