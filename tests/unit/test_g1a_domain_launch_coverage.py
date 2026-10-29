"""g1a: launch needs a template on every sequence step, not just as many templates as steps."""
from app.domain.campaigns import missing_templates_message, uncovered_steps


def test_g1a_step_without_template_is_reported_even_when_counts_match():
    steps = [(1, "s1"), (2, "s2")]
    # step 1 has a generic and a CFO version (two templates for two steps), step 2 has none
    assert uncovered_steps(steps, ["s1", "s1"]) == [2]


def test_g1a_every_step_covered():
    assert uncovered_steps([(2, "s2"), (1, "s1")], ["s2", "s1", None]) == []


def test_g1a_unlinked_templates_cover_nothing_and_steps_sorted():
    assert uncovered_steps([(3, "s3"), (1, "s1"), (2, "s2")], [None, "s2"]) == [1, 3]


def test_g1a_message_names_the_steps():
    assert missing_templates_message([2]) == "Missing email templates for step 2"
    assert missing_templates_message([1, 3]) == "Missing email templates for steps 1, 3"
