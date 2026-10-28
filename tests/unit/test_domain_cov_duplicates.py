"""Coverage of duplicate-pair scoring edges (app/domain/duplicates.py)."""
from app.domain.duplicates import MAX_NAME_BLOCK, Candidate, PairMatch, find_pairs, name_match


def test_pair_without_signals_scores_zero_and_is_medium():
    match = PairMatch("a", "b")
    assert match.score == 0 and match.reasons == [] and match.confidence == "MEDIUM"


def test_identical_first_and_last_name_keeps_the_straight_match_over_the_swap():
    assert name_match("Lee", "Lee", "Lee", "Lee") == (100, "same name")


def test_same_phone_twice_on_one_contact_is_not_a_pair_with_itself():
    people = [Candidate("p1", phone="+1 555 123 4567", mobile_phone="555-123-4567")]
    assert find_pairs(people) == []


def test_different_people_at_the_same_company_are_not_suggested():
    people = [Candidate("p1", "Anna", "Smith", account_id="acc"), Candidate("p2", "Zoe", "Smith", account_id="acc")]
    assert find_pairs(people) == []


def test_oversized_company_name_block_is_skipped():
    people = [Candidate(f"p{i:03}", "John", "Smith", account_id="acc") for i in range(MAX_NAME_BLOCK + 1)]
    assert find_pairs(people) == []
    small = find_pairs(people[:2])
    assert len(small) == 1 and small[0].score == 100
