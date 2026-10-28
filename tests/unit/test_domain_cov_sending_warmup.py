"""Coverage of blending external reputation (app/domain/sending_warmup.py)."""
from types import SimpleNamespace

from app.domain.sending_warmup import blend_external


def test_blend_external_without_provider_score_only_applies_rates():
    ext = SimpleNamespace(complaint_rate=0.01, bounce_rate=0.05, reputation_score=None)
    assert blend_external(80.0, ext) == 80.0 - 2.0 - 6.0


def test_blend_external_with_provider_score_weights_it_30_percent():
    ext = SimpleNamespace(complaint_rate=0, bounce_rate=0, reputation_score=50)
    assert blend_external(100.0, ext) == 100.0 * 0.7 + 50 * 0.3
    assert blend_external(77.0, None) == 77.0
