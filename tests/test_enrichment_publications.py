from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from draftomen.enrichment_publications import (
    ENRICHMENT_PUBLICATIONS_FILE_NAME,
    READ_ERROR,
    EnrichmentPublication,
    EnrichmentPublicationError,
    EnrichmentPublications,
    commit_enrichment_candidate,
    load_enrichment_publications,
    publish_enrichment_publications,
    resolve_enrichment_candidates,
    write_enrichment_candidate,
)


SET_CODE = "HOB"
EVENT_FORMAT = "QuickDraft"
ARTIFACT_SHA256 = "a" * 64
PROFILE_GZIP_SHA256 = "b" * 64
RUN_ID = "9574d202eef14943"
REVIEWED_AT = "2026-09-01T10:30:00+00:00"
PUBLISHED_AT = "2026-09-02T11:45:00+00:00"


def _publication(
    set_code: str = SET_CODE,
    event_format: str = EVENT_FORMAT,
    *,
    artifact_sha256: str = ARTIFACT_SHA256,
    run_id: str = RUN_ID,
    reviewed_at: str = REVIEWED_AT,
    published_at: str = PUBLISHED_AT,
    profile_gzip_sha256: str = PROFILE_GZIP_SHA256,
) -> EnrichmentPublication:
    return EnrichmentPublication(
        set_code=set_code,
        event_format=event_format,
        artifact_sha256=artifact_sha256,
        run_id=run_id,
        reviewed_at=reviewed_at,
        published_at=published_at,
        profile_gzip_sha256=profile_gzip_sha256,
    )


def _raw_publication(**overrides: object) -> dict[str, object]:
    value = _publication().to_json()
    value.update(overrides)
    return value


_OMITTED = object()


def _raw_record(
    *,
    schema_version: object = 2,
    publications: object = (),
    candidates: object = _OMITTED,
    **extra: object,
) -> bytes:
    value: dict[str, object] = {
        "publications": publications,
        "schema_version": schema_version,
        **extra,
    }
    if candidates is not _OMITTED:
        value["candidates"] = candidates
    return json.dumps(value).encode("utf-8")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("artifact_sha256", "A" * 64, "artifact_sha256 must be a lowercase SHA-256 digest."),
        ("artifact_sha256", "not-a-digest", "artifact_sha256 must be a lowercase SHA-256 digest."),
        (
            "profile_gzip_sha256",
            ARTIFACT_SHA256[:63],
            "profile_gzip_sha256 must be a lowercase SHA-256 digest.",
        ),
        ("run_id", "", "run_id must be a safe identifier."),
        ("run_id", "nested/run", "run_id must be a safe identifier."),
        (
            "reviewed_at",
            "2026-09-01T10:30:00",
            "reviewed_at must be a timezone-aware ISO-8601 timestamp.",
        ),
        (
            "published_at",
            "not-a-timestamp",
            "published_at must be a timezone-aware ISO-8601 timestamp.",
        ),
        ("set_code", "", "set_code must be a safe identifier."),
        ("event_format", "..", "event_format must be a safe identifier."),
    ],
)
def test_publication_rejects_invalid_fields(field: str, value: str, message: str) -> None:
    with pytest.raises(EnrichmentPublicationError, match=re.escape(message)):
        _publication(**{field: value})


def test_publication_casefolds_its_identity() -> None:
    publication = _publication(" Hob ", "QUICKDRAFT")

    assert publication.set_code == "hob"
    assert publication.event_format == "quickdraft"


def test_publication_stores_timestamps_verbatim() -> None:
    publication = _publication(
        reviewed_at="2026-09-01T10:30:00Z",
        published_at="2026-09-02T12:45:00+02:00",
    )

    assert publication.reviewed_at == "2026-09-01T10:30:00Z"
    assert publication.published_at == "2026-09-02T12:45:00+02:00"


def test_publication_stores_the_run_identity_verbatim() -> None:
    publication = _publication(run_id="9574D202EEF14943")

    assert publication.run_id == "9574D202EEF14943"


def test_publication_json_round_trip_requires_the_exact_key_set() -> None:
    publication = _publication()
    value = publication.to_json()

    assert set(value) == {
        "artifact_sha256",
        "event_format",
        "profile_gzip_sha256",
        "published_at",
        "reviewed_at",
        "run_id",
        "set_code",
    }
    assert EnrichmentPublication.from_json(value) == publication

    value["format"] = "quickdraft"
    with pytest.raises(EnrichmentPublicationError, match="unsupported fields"):
        EnrichmentPublication.from_json(value)

    del value["format"]
    del value["run_id"]
    with pytest.raises(EnrichmentPublicationError, match="Missing required"):
        EnrichmentPublication.from_json(value)


def test_record_round_trip_is_canonical_and_sorted() -> None:
    record = EnrichmentPublications(
        publications=(
            _publication(
                "ZZZ",
                "PremierDraft",
                artifact_sha256="c" * 64,
                profile_gzip_sha256="d" * 64,
            ),
            _publication(),
        )
    )

    assert [(item.set_code, item.event_format) for item in record.publications] == [
        ("hob", "quickdraft"),
        ("zzz", "premierdraft"),
    ]
    assert record.candidates == ()

    payload = record.to_bytes()
    assert payload == record.to_bytes()
    assert payload.count(b"\n") == 1
    assert payload.endswith(b"\n")
    assert payload == json.dumps(
        json.loads(payload.decode("utf-8")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    assert payload.decode("utf-8").startswith('{"candidates":[]')
    assert set(json.loads(payload.decode("utf-8"))) == {
        "candidates",
        "publications",
        "schema_version",
    }
    assert EnrichmentPublications.from_bytes(payload) == record


def test_record_rejects_duplicate_identities() -> None:
    with pytest.raises(EnrichmentPublicationError, match="duplicate identity"):
        EnrichmentPublications(publications=(_publication(), _publication(" hob ", "QUICKDRAFT")))


def test_record_rejects_duplicate_candidate_identities() -> None:
    with pytest.raises(EnrichmentPublicationError, match="duplicate candidate identity"):
        EnrichmentPublications(
            candidates=(_publication(profile_gzip_sha256="c" * 64), _publication())
        )


def test_merged_replaces_one_identity_and_keeps_the_sort_order() -> None:
    hob = _publication()
    lci = _publication(
        "LCI",
        "QuickDraft",
        artifact_sha256="c" * 64,
        profile_gzip_sha256="d" * 64,
    )
    record = EnrichmentPublications(publications=(lci, hob))

    replacement = _publication(
        "lci",
        "QUICKDRAFT",
        artifact_sha256="e" * 64,
        profile_gzip_sha256="f" * 64,
        published_at="2026-09-03T09:00:00+00:00",
    )
    merged = record.merged(replacement)

    assert [(item.set_code, item.event_format) for item in merged.publications] == [
        ("hob", "quickdraft"),
        ("lci", "quickdraft"),
    ]
    assert merged.select(set_code="LCI", event_format="quickdraft") == replacement
    assert merged.select(set_code="hob", event_format="QUICKDRAFT") == hob
    assert merged.select(set_code="hob", event_format="PremierDraft") is None
    assert record.select(set_code="lci", event_format="QuickDraft") == lci


def test_load_returns_an_empty_record_when_the_file_is_absent(tmp_path: Path) -> None:
    assert load_enrichment_publications(profiles_dir=tmp_path) == EnrichmentPublications()
    assert (
        load_enrichment_publications(profiles_dir=tmp_path / "profiles")
        == EnrichmentPublications()
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[1, 2, 3]",
        _raw_record(schema_version=3),
        _raw_record(schema_version="1"),
        _raw_record(),
        _raw_record(schema_version=1, candidates=[]),
        _raw_record(candidates={}),
        _raw_record(candidates=[_raw_publication(), _raw_publication()]),
        _raw_record(unexpected=True),
        _raw_record(publications=[{"set_code": "hob"}]),
        _raw_record(publications={"hob": {}}),
        _raw_record(publications=[_raw_publication(artifact_sha256="nope")]),
        _raw_record(publications=[_raw_publication(set_code="hob/quickdraft")]),
    ],
)
def test_load_fails_closed_on_unusable_record_bytes(tmp_path: Path, payload: bytes) -> None:
    (tmp_path / ENRICHMENT_PUBLICATIONS_FILE_NAME).write_bytes(payload)

    with pytest.raises(EnrichmentPublicationError, match=re.escape(READ_ERROR)):
        load_enrichment_publications(profiles_dir=tmp_path)


def test_load_accepts_a_version_1_record_without_candidates(tmp_path: Path) -> None:
    publication = _publication()
    (tmp_path / ENRICHMENT_PUBLICATIONS_FILE_NAME).write_bytes(
        _raw_record(schema_version=1, publications=[publication.to_json()])
    )

    record = load_enrichment_publications(profiles_dir=tmp_path)

    assert record == EnrichmentPublications(publications=(publication,))
    assert record.candidates == ()


def test_write_enrichment_candidate_records_only_a_candidate(tmp_path: Path) -> None:
    publication = _publication()

    path = write_enrichment_candidate(profiles_dir=tmp_path, publication=publication)

    assert path == tmp_path / ENRICHMENT_PUBLICATIONS_FILE_NAME
    assert load_enrichment_publications(profiles_dir=tmp_path) == EnrichmentPublications(
        candidates=(publication,)
    )
    assert json.loads(path.read_bytes().decode("utf-8")) == {
        "candidates": [publication.to_json()],
        "publications": [],
        "schema_version": 2,
    }
    first_payload = path.read_bytes()
    first_modified = path.stat().st_mtime_ns

    assert write_enrichment_candidate(profiles_dir=tmp_path, publication=publication) == path

    assert path.read_bytes() == first_payload
    assert path.stat().st_mtime_ns == first_modified
    assert [item.name for item in tmp_path.iterdir()] == [ENRICHMENT_PUBLICATIONS_FILE_NAME]


def test_commit_enrichment_candidate_promotes_and_drops_the_candidate(tmp_path: Path) -> None:
    committed = _publication()
    lci = _publication(
        "lci",
        "PremierDraft",
        artifact_sha256="c" * 64,
        profile_gzip_sha256="d" * 64,
    )
    candidate = _publication(artifact_sha256="e" * 64, profile_gzip_sha256="f" * 64)
    publish_enrichment_publications(
        profiles_dir=tmp_path,
        record=EnrichmentPublications(publications=(committed, lci), candidates=(candidate,)),
    )

    path = commit_enrichment_candidate(
        profiles_dir=tmp_path,
        set_code="HOB",
        event_format="QuickDraft",
    )

    assert load_enrichment_publications(profiles_dir=tmp_path) == EnrichmentPublications(
        publications=(candidate, lci)
    )
    assert json.loads(path.read_bytes().decode("utf-8")) == {
        "candidates": [],
        "publications": [candidate.to_json(), lci.to_json()],
        "schema_version": 2,
    }
    committed_payload = path.read_bytes()
    committed_modified = path.stat().st_mtime_ns

    assert (
        commit_enrichment_candidate(
            profiles_dir=tmp_path,
            set_code="hob",
            event_format="quickdraft",
        )
        == path
    )

    assert path.read_bytes() == committed_payload
    assert path.stat().st_mtime_ns == committed_modified


def test_resolve_enrichment_candidates_promotes_and_drops(tmp_path: Path) -> None:
    committed = _publication(profile_gzip_sha256="a" * 64)
    lci = _publication(
        "lci",
        "PremierDraft",
        artifact_sha256="c" * 64,
        profile_gzip_sha256="d" * 64,
    )
    promoted = _publication(artifact_sha256="e" * 64, profile_gzip_sha256="f" * 64)
    dropped_digest_mismatch = _publication(
        "zzz",
        "PremierDraft",
        artifact_sha256="1" * 64,
        profile_gzip_sha256="2" * 64,
    )
    dropped_unselected_identity = _publication(
        "lci",
        "QuickDraft",
        artifact_sha256="3" * 64,
        profile_gzip_sha256="4" * 64,
    )
    record = EnrichmentPublications(
        publications=(committed, lci),
        candidates=(promoted, dropped_digest_mismatch, dropped_unselected_identity),
    )
    path = publish_enrichment_publications(
        profiles_dir=tmp_path,
        record=EnrichmentPublications(publications=(committed, lci)),
    )
    untouched = path.stat().st_mtime_ns

    assert (
        resolve_enrichment_candidates(
            profiles_dir=tmp_path,
            selected={("hob", "quickdraft"): committed.profile_gzip_sha256},
        )
        is None
    )
    assert path.stat().st_mtime_ns == untouched

    publish_enrichment_publications(profiles_dir=tmp_path, record=record)

    assert (
        resolve_enrichment_candidates(
            profiles_dir=tmp_path,
            selected={("HOB", "QuickDraft"): promoted.profile_gzip_sha256},
        )
        == path
    )

    assert load_enrichment_publications(profiles_dir=tmp_path) == EnrichmentPublications(
        publications=(promoted, lci)
    )
    assert json.loads(path.read_bytes().decode("utf-8")) == {
        "candidates": [],
        "publications": [promoted.to_json(), lci.to_json()],
        "schema_version": 2,
    }


def test_record_protects_a_retained_digest_through_either_entry() -> None:
    record = EnrichmentPublications(
        publications=(_publication(profile_gzip_sha256="a" * 64),),
        candidates=(_publication(profile_gzip_sha256="b" * 64),),
    )

    def protects(digest: str, *, set_code: str = "hob", event_format: str = "quickdraft") -> bool:
        return record.protects(
            set_code=set_code,
            event_format=event_format,
            profile_gzip_sha256=digest,
        )

    assert protects("a" * 64) is True
    assert protects("b" * 64) is True
    assert protects("c" * 64) is False
    assert protects("a" * 64, set_code="LCI") is False
    assert protects("not-a-digest") is False

