"""`pa setup` tests (plan §8.2): idempotent provisioning of the required custom
fields + review tags, a verified no-op on re-run, and incompatible-field
reporting (never clobber).
"""
from paperless_assistant.provision import Provisioner, REQUIRED_FIELDS
from fakes import FakePaperless, make_custom_fields, healthy_tags


def test_fresh_paperless_creates_fields_and_tags():
    fake = FakePaperless(fields=[], tags=[])
    report = Provisioner(fake.client()).run()

    assert report.ok
    assert set(report.created_fields) == set(REQUIRED_FIELDS)  # 3 fields
    assert set(report.created_tags) == {"superseded", "ai-new-taxonomy"}  # 2 tags
    assert not report.is_noop

    # The select field carries the state-machine options.
    stage = next(f for f in fake.custom_fields if f["name"] == "ai_stage")
    labels = {o["label"] for o in stage["extra_data"]["select_options"]}
    assert labels == {"triaged", "reocr_done", "metadata_done"}
    # Exactly the two review tags exist (no duplicates).
    assert sorted(t["name"] for t in fake.tags) == ["ai-new-taxonomy", "superseded"]


def test_second_run_is_verified_noop():
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    n_fields_before = len(fake.custom_fields)
    n_tags_before = len(fake.tags)

    report = Provisioner(fake.client()).run()

    assert report.ok
    assert report.is_noop                      # <-- verified no-op
    assert report.created_fields == []
    assert report.created_tags == []
    assert set(report.existing_fields) == set(REQUIRED_FIELDS)
    assert set(report.existing_tags) == {"superseded", "ai-new-taxonomy"}
    # Nothing created (no duplicates).
    assert len(fake.custom_fields) == n_fields_before
    assert len(fake.tags) == n_tags_before


def test_idempotent_double_run_equivalent():
    fake = FakePaperless(fields=[], tags=[])
    Provisioner(fake.client()).run()
    fields_after_first = [f["name"] for f in fake.custom_fields]
    tags_after_first = [t["name"] for t in fake.tags]

    report2 = Provisioner(fake.client()).run()
    assert report2.is_noop
    assert [f["name"] for f in fake.custom_fields] == fields_after_first
    assert [t["name"] for t in fake.tags] == tags_after_first


def test_incompatible_field_reported_not_clobbered():
    # ai_stage exists but as a TEXT field, not a select -> incompatible.
    bad = make_custom_fields()
    for f in bad:
        if f["name"] == "ai_stage":
            f["data_type"] = "text"
            f.pop("extra_data", None)
    fake = FakePaperless(fields=bad, tags=healthy_tags())

    report = Provisioner(fake.client()).run()

    assert not report.ok
    assert any("ai_stage" in m and "data_type" in m for m in report.incompatible)
    # The existing (text) field is untouched - never clobbered.
    stage = next(f for f in fake.custom_fields if f["name"] == "ai_stage")
    assert stage["data_type"] == "text"
    # And we did not create a duplicate ai_stage.
    assert sum(1 for f in fake.custom_fields if f["name"] == "ai_stage") == 1


def test_missing_select_option_reported():
    # ai_stage select exists but is missing the 'metadata_done' option.
    fields = make_custom_fields(stage_options=("triaged", "reocr_done"))
    fake = FakePaperless(fields=fields, tags=healthy_tags())
    report = Provisioner(fake.client()).run()
    assert not report.ok
    assert any("metadata_done" in m for m in report.incompatible)


def test_integer_ocr_quality_tolerated():
    # An integer ocr_quality is acceptable (coerce_score handles it) -> compatible.
    fields = make_custom_fields(score_type="integer")
    fake = FakePaperless(fields=fields, tags=healthy_tags())
    report = Provisioner(fake.client()).run()
    assert report.ok
    assert "ocr_quality" in report.existing_fields
