"""Coverage of the saved-mapping size limit (app/domain/imports.py)."""
import pytest

from app.domain.imports import ImportInputError, SavedMappingInput


def test_saved_mapping_accepts_500_columns_and_refuses_more():
    ok = SavedMappingInput.from_raw("Big", {f"c{i}": "email" for i in range(500)})
    assert len(ok.mapping) == 500 and ok.options == {}
    with pytest.raises(ImportInputError, match="too many columns"):
        SavedMappingInput.from_raw("Big", {f"c{i}": "email" for i in range(501)})
