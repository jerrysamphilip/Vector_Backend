"""Coverage of the campaign list paging rule (app/domain/campaigns.py)."""
from app.domain.campaigns import total_pages


def test_total_pages_rounds_up_and_is_zero_when_empty():
    assert total_pages(0, 20) == 0
    assert total_pages(1, 20) == 1
    assert total_pages(20, 20) == 1
    assert total_pages(21, 20) == 2
