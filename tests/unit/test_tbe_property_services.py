"""C-17 service layer: ContactFieldService and the value helpers against a dict-backed fake repository and a
fake unit of work. Type changes are refused, DELETE deactivates (values kept, still exported, hidden from
forms / writes / required checks), system properties stay active, options are {value, label}."""
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.contact_field import ContactFieldDefinition
from app.schemas.contact_schema import FieldOption, FieldWrite
from app.services import contact_field_service as fields
from app.services.contact_field_service import ContactFieldService


class FakeRepo:
    def __init__(self):
        self.fields = {}
        self.staged = []

    def add(self, tenant_id, entity):
        entity.field_id = entity.field_id or f"f{len(self.fields) + 1}"
        self.fields[entity.field_id] = entity
        self.staged.append(entity)

    def flush(self):
        pass

    def field_definitions(self, tenant_id, order="none"):
        return [f for f in self.fields.values() if f.tenant_id == tenant_id]

    def required_fields(self, tenant_id):
        return [f for f in self.field_definitions(tenant_id) if f.required]

    def field_definition(self, tenant_id, field_id):
        f = self.fields.get(field_id)
        return f if f is not None and f.tenant_id == tenant_id else None

    def field_key_taken(self, tenant_id, key):
        return any(f.field_key == key for f in self.field_definitions(tenant_id))

    def count_fields(self, tenant_id):
        return len(self.field_definitions(tenant_id))


class FakeUow:
    def __init__(self, repo):
        self.contacts, self.session = repo, None
        self.entered = self.commits = self.rollbacks = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.rollbacks += 1
        return False

    def commit(self):
        self.commits += 1


def manager():
    return SimpleNamespace(user_id="u1", tenant_id="t1", role="ADMIN", custom_permissions=None)


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def svc(repo):
    return ContactFieldService(FakeUow(repo))


def _definition(repo, **kw):
    base = dict(field_id="f9", tenant_id="t1", field_key="tier", label="Tier", field_type="SELECT",
                options=["Gold", "Silver"], group_name=None, required=False, sort_order=0, object_type="CONTACT",
                is_active=True, is_system=False, max_length=None)
    base.update(kw)
    d = ContactFieldDefinition(**base)
    repo.fields[d.field_id] = d
    return d


def test_tbe_create_stores_value_label_options_and_new_types(svc, repo):
    out = svc.create(manager(), FieldWrite(label="Tier", field_type="select",
                                           options=["Gold", FieldOption(value="s", label="Silver")]))
    assert out["field_type"] == "SELECT" and out["options"] == ["Gold", "s"]
    assert out["choices"] == [{"value": "Gold", "label": "Gold"}, {"value": "s", "label": "Silver"}]
    assert out["is_active"] is True and out["is_system"] is False and out["max_length"] is None
    assert repo.staged[-1].options == out["choices"]
    flag = svc.create(manager(), FieldWrite(label="Opted in", field_type="BOOLEAN", options=["ignored"]))
    assert flag["field_type"] == "BOOLEAN" and flag["options"] == []
    notes = svc.create(manager(), FieldWrite(label="Notes", field_type="LONG_TEXT", max_length=5000))
    assert notes["max_length"] == 5000
    with pytest.raises(BadRequestError, match="Only text properties"):
        svc.create(manager(), FieldWrite(label="Seats", field_type="NUMBER", max_length=5))
    assert svc.create(manager(), FieldWrite(label="Plain"))["field_type"] == "TEXT"


def test_tbe_type_cannot_change_after_creation(svc, repo):
    _definition(repo)
    with pytest.raises(BadRequestError, match="data type cannot be changed"):
        svc.update(manager(), "f9", FieldWrite(label="Tier", field_type="TEXT", options=["x"]))
    assert repo.fields["f9"].field_type == "SELECT"
    # same type (any case) or none at all is fine
    assert svc.update(manager(), "f9", FieldWrite(label="Tier", field_type="select"))["field_type"] == "SELECT"
    assert svc.update(manager(), "f9", FieldWrite(label="Tier level"))["label"] == "Tier level"


def test_tbe_renaming_an_option_label_keeps_values_valid(svc, repo):
    _definition(repo)
    out = svc.update(manager(), "f9", FieldWrite(label="Tier", options=[
        FieldOption(value="Gold", label="Gold tier"), "Silver"]))
    assert out["choices"][0] == {"value": "Gold", "label": "Gold tier"} and out["options"] == ["Gold", "Silver"]
    definition = repo.fields["f9"]
    assert fields.clean_custom_value(definition, "Gold") == "Gold"
    assert fields.clean_custom_value(definition, "gold tier") == "Gold"  # by label
    with pytest.raises(BadRequestError, match="must be one of"):
        fields.clean_custom_value(definition, "Bronze")
    with pytest.raises(BadRequestError, match="needs at least one option"):
        svc.update(manager(), "f9", FieldWrite(label="Tier", options=[]))
    # no options in the PATCH: the current ones stay
    assert svc.update(manager(), "f9", FieldWrite(label="Tier"))["options"] == ["Gold", "Silver"]


def test_tbe_delete_deactivates_and_patch_reactivates(svc, repo):
    _definition(repo)
    assert svc.delete(manager(), "f9") == {"status": "deactivated", "field_id": "f9"}
    assert repo.fields["f9"].is_active is False and "f9" in repo.fields
    assert svc.list(manager()) == []
    listed = svc.list(manager(), include_inactive=True)
    assert [f["field_id"] for f in listed] == ["f9"] and listed[0]["is_active"] is False
    with pytest.raises(ConflictError, match="reactivate it instead"):
        svc.create(manager(), FieldWrite(label="tier", field_type="TEXT"))
    assert svc.update(manager(), "f9", FieldWrite(label="Tier", is_active=True))["is_active"] is True
    with pytest.raises(NotFoundError):
        svc.delete(manager(), "missing")


def test_tbe_system_properties_cannot_be_deactivated(svc, repo):
    _definition(repo, is_system=True)
    with pytest.raises(BadRequestError, match="System properties cannot be deactivated"):
        svc.delete(manager(), "f9")
    with pytest.raises(BadRequestError, match="System properties cannot be deactivated"):
        svc.update(manager(), "f9", FieldWrite(label="Tier", is_active=False))
    assert repo.fields["f9"].is_active is True


def test_tbe_rename_onto_another_property_is_a_conflict(svc, repo):
    _definition(repo)
    _definition(repo, field_id="f8", field_key="size", label="Size", field_type="NUMBER", options=None)
    with pytest.raises(ConflictError, match="A property called tier already exists"):
        svc.update(manager(), "f8", FieldWrite(label="tier"))


def test_tbe_inactive_values_are_kept_hidden_from_writes_and_required_but_exported(repo):
    _definition(repo, required=True, is_active=False)
    _definition(repo, field_id="f8", field_key="size", label="Size", field_type="NUMBER", options=None,
                required=True)
    # a write naming the inactive property is ignored (the stored value is kept by the merge)
    assert fields.clean_custom_fields(repo, "t1", {"tier": "Nope", "size": "3"}) == {"size": 3}
    with pytest.raises(BadRequestError, match="Unknown custom property"):
        fields.clean_custom_fields(repo, "t1", {"nope": 1})
    record = SimpleNamespace(custom_fields={})
    assert fields.required_missing(repo, "t1", record) == ["Size"]
    assert fields.label_map(repo, "t1") == {"tier": "Tier", "size": "Size"}
    assert [d.field_key for d in fields.definitions_in_order(repo, "t1")] == ["tier", "size"]


def _def(field_type, **kw):
    return SimpleNamespace(label="Prop", field_type=field_type, options=kw.pop("options", None), **kw)


def test_tbe_values_of_the_new_types_and_limits():
    assert fields.clean_custom_value(_def("BOOLEAN"), "yes") is True
    assert fields.clean_custom_value(_def("BOOLEAN"), False) is False
    with pytest.raises(BadRequestError, match="must be true or false"):
        fields.clean_custom_value(_def("BOOLEAN"), "perhaps")
    assert fields.clean_custom_value(_def("LONG_TEXT"), " a\nb ") == "a\nb"
    assert fields.clean_custom_value(_def("LONG_TEXT"), "x" * 12000) == "x" * 10000
    assert fields.clean_custom_value(_def("TEXT", max_length=3), "abc") == "abc"
    with pytest.raises(BadRequestError, match="at most 3 characters"):
        fields.clean_custom_value(_def("TEXT", max_length=3), "abcd")
    # stand-ins without max_length (import's pending properties) still work
    assert fields.clean_custom_value(_def("TEXT"), "x" * 1200) == "x" * 1000


def test_tbe_multi_choice_accepts_values_or_labels_in_option_order():
    d = _def("MULTI_CHECKBOX", options=[{"value": "r", "label": "Red"}, {"value": "b", "label": "Blue"}])
    assert fields.clean_custom_value(d, "blue; r") == ["r", "b"]
    assert fields.clean_custom_value(d, ["Red"]) == ["r"]
    with pytest.raises(BadRequestError, match="unknown choices"):
        fields.clean_custom_value(d, ["Pink"])


def test_tbe_create_field_definition_normalises_import_options(repo):
    d = fields.create_field_definition(repo, "t1", "Colour", "SELECT", ["Red", "red", "Blue"])
    assert d.options == [{"value": "Red", "label": "Red"}, {"value": "Blue", "label": "Blue"}]
    assert d.is_active is True and d.is_system is False
    assert fields.create_field_definition(repo, "t1", "Free", "TEXT", None).options is None
    with pytest.raises(BadRequestError, match="Duplicate option value"):
        fields.create_field_definition(repo, "t1", "Bad", "SELECT", ["a", {"value": "a", "label": "b"}])


def test_tbe_field_dict_reads_old_rows():
    old = SimpleNamespace(field_id="f1", field_key="k", label="K", field_type="RADIO", options=["Yes", "No"],
                          sort_order=None, group_name=None, required=None, object_type=None)
    out = fields.field_dict(old)
    assert out["options"] == ["Yes", "No"] and out["choices"][1] == {"value": "No", "label": "No"}
    assert out["is_active"] is True and out["is_system"] is False and out["max_length"] is None
    assert out["object_type"] == "CONTACT"
