"""Strict source-aware semantic enrichment artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Self

from draftomen.carddb import CardFace, CardInfo
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    CardRelationship,
    CardSourcePin,
    GuideClaim,
    GuideEvidence,
    GuideSourcePin,
    ModelRun,
    OracleEvidence,
    OracleFact,
    RejectedFinding,
    SemanticEnrichmentError,
    FindingStatus,
    require_utf8_text,
)


SEMANTIC_ENRICHMENT_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise SemanticEnrichmentError(f"missing required field {key!r}.")
    return value[key]


def _keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        details: list[str] = []
        if unknown:
            details.append("unknown fields: " + ", ".join(sorted(repr(item) for item in unknown)))
        if missing:
            details.append("missing fields: " + ", ".join(sorted(repr(item) for item in missing)))
        raise SemanticEnrichmentError(f"{field_name} has invalid keys ({'; '.join(details)}).")


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticEnrichmentError(f"{field_name} must be a nonblank string.")
    return require_utf8_text(value, field_name).strip()


def _exact_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticEnrichmentError(f"{field_name} must be nonblank text.")
    return require_utf8_text(value, field_name)


def _integer(value: Any, field_name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticEnrichmentError(f"{field_name} must be an integer.")
    if value < (1 if positive else 0):
        bound = "positive" if positive else "non-negative"
        raise SemanticEnrichmentError(f"{field_name} must be a {bound} integer.")
    return value


def _hash(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise SemanticEnrichmentError(f"{field_name} must be a SHA-256 digest.")
    return value.lower()


def _timestamp(value: Any, field_name: str) -> tuple[str, datetime]:
    timestamp = _identifier(value, field_name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise SemanticEnrichmentError(f"{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise SemanticEnrichmentError(f"{field_name} must include a timezone.")
    normalized = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return normalized, parsed


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return (encoded + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise SemanticEnrichmentError("value cannot be encoded as canonical JSON.") from error


def _canonical(
    values: tuple[Any, ...],
    *,
    field_name: str,
    key: Callable[[Any], object],
) -> tuple[Any, ...]:
    identities = [key(item) for item in values]
    if len(set(identities)) != len(identities):
        raise SemanticEnrichmentError(f"{field_name} contains duplicate entries.")
    return tuple(sorted(values, key=key))


def _tuple(value: Any, field_name: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise SemanticEnrichmentError(f"{field_name} must be a tuple.")
    return value


def _json_array(value: Any, field_name: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise SemanticEnrichmentError(f"{field_name} must be a JSON array.")
    return tuple(value)


def _optional_identity(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SemanticEnrichmentError(f"{field_name} must be a string or null.")
    return require_utf8_text(value, field_name).strip()


def _optional_rules_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SemanticEnrichmentError(f"{field_name} must be a string or null.")
    return require_utf8_text(value, field_name)


@dataclass(frozen=True, slots=True)
class GuideSource:
    """One preserved guide source used to validate guide evidence."""

    guide_id: str
    url: str
    text: str
    retrieved_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "guide_id", _identifier(self.guide_id, "guide_id"))
        object.__setattr__(self, "url", _identifier(self.url, "url"))
        text = require_utf8_text(self.text, "text")
        if not text.strip():
            raise SemanticEnrichmentError("text must be nonblank text.")
        normalized, _ = _timestamp(self.retrieved_at, "retrieved_at")
        object.__setattr__(self, "retrieved_at", normalized)

    @property
    def text_sha256(self) -> str:
        """Return the digest of the preserved guide text."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EnrichmentSources:
    """Frozen source context required to validate an enrichment artifact."""

    set_code: str
    cards: tuple[CardInfo, ...]
    guides: tuple[GuideSource, ...]

    def __post_init__(self) -> None:
        normalized_set_code = _identifier(self.set_code, "set_code").casefold()
        object.__setattr__(self, "set_code", normalized_set_code)

        cards = _tuple(self.cards, "cards")
        if not cards:
            raise SemanticEnrichmentError("cards must not be empty.")
        if any(not isinstance(card, CardInfo) for card in cards):
            raise SemanticEnrichmentError("cards must contain CardInfo records.")
        for card in cards:
            grp_id = _integer(card.grp_id, "card.grp_id", positive=True)
            if type(card.unknown) is not bool:
                raise SemanticEnrichmentError("card.unknown must be a boolean.")
            if card.unknown:
                raise SemanticEnrichmentError("unknown cards cannot be enrichment sources.")
            if not isinstance(card.set_code, str):
                raise SemanticEnrichmentError("card.set_code must be a string.")
            if card.set_code.strip().casefold() != normalized_set_code:
                raise SemanticEnrichmentError("card.set_code does not match sources.set_code.")
            if not isinstance(card.name, str) or not card.name.strip():
                raise SemanticEnrichmentError("card.name must be nonblank text.")
            faces = _tuple(card.faces, "card.faces")
            if any(not isinstance(face, CardFace) for face in faces):
                raise SemanticEnrichmentError("card.faces must contain CardFace records.")
            if card.arena_id is not None:
                arena_id = _integer(card.arena_id, "card.arena_id", positive=True)
                if arena_id != grp_id:
                    raise SemanticEnrichmentError("card.arena_id must equal card.grp_id.")
            card_source_projection(card)
        object.__setattr__(self, "cards", _canonical(cards, field_name="cards", key=lambda card: card.grp_id))

        guides = _tuple(self.guides, "guides")
        if any(not isinstance(guide, GuideSource) for guide in guides):
            raise SemanticEnrichmentError("guides must contain GuideSource records.")
        object.__setattr__(self, "guides", _canonical(guides, field_name="guides", key=lambda guide: guide.guide_id))


def card_source_projection(card: CardInfo) -> dict[str, object]:
    """Return the normalized semantic projection of one canonical card.
    Face index stays request metadata rather than source-hash data.
    """
    if not isinstance(card, CardInfo):
        raise SemanticEnrichmentError("card must be a CardInfo.")
    card_id = _integer(card.grp_id, "card.grp_id", positive=True)
    faces = _tuple(card.faces, "card.faces")
    face_projection: list[dict[str, str | None]] = []
    for index, face in enumerate(faces):
        if not isinstance(face, CardFace):
            raise SemanticEnrichmentError(f"card.faces[{index}] must be a CardFace.")
        face_projection.append(
            {
                "name": _optional_rules_text(face.name, f"card.faces[{index}].name"),
                "type_line": _optional_rules_text(face.type_line, f"card.faces[{index}].type_line"),
                "oracle_text": _optional_rules_text(
                    face.oracle_text,
                    f"card.faces[{index}].oracle_text",
                ),
            }
        )
    return {
        "card_id": card_id,
        "oracle_id": _optional_identity(card.oracle_id, "card.oracle_id"),
        "set_code": _optional_identity(card.set_code, "card.set_code").casefold()
        if card.set_code is not None
        else None,
        "collector_number": _optional_identity(card.collector_number, "card.collector_number"),
        "name": _optional_identity(card.name, "card.name"),
        "layout": _optional_identity(card.layout, "card.layout"),
        "type_line": _optional_rules_text(card.type_line, "card.type_line"),
        "oracle_text": _optional_rules_text(card.oracle_text, "card.oracle_text"),
        "faces": face_projection,
    }


def card_source_sha256(card: CardInfo) -> str:
    """Hash normalized semantic card inputs, not a raw card-data blob."""
    projection = card_source_projection(card)
    return hashlib.sha256(_json_bytes(projection)).hexdigest()


def set_source_sha256(sources: EnrichmentSources) -> str:
    """Hash normalized semantic set inputs, not a raw card-data blob."""
    if not isinstance(sources, EnrichmentSources):
        raise SemanticEnrichmentError("sources must be an EnrichmentSources record.")
    projection = {
        "set_code": sources.set_code,
        "cards": [card_source_projection(card) for card in sources.cards],
    }
    return hashlib.sha256(_json_bytes(projection)).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticEnrichmentArtifact:
    """Strict, source-bound semantic enrichment artifact."""

    schema_version: int = field(default=SEMANTIC_ENRICHMENT_SCHEMA_VERSION, kw_only=True)
    set_code: str
    set_source_id: str
    set_source_sha256: str
    created_at: str
    cards: tuple[CardSourcePin, ...]
    guides: tuple[GuideSourcePin, ...]
    runs: tuple[ModelRun, ...]
    oracle_facts: tuple[OracleFact, ...]
    guide_claims: tuple[GuideClaim, ...]
    relationships: tuple[CardRelationship, ...]
    rejected_findings: tuple[RejectedFinding, ...]
    review: ArtifactReview
    confirmed_relationship_ids: tuple[str, ...]
    sources: InitVar[EnrichmentSources] = field(kw_only=True)

    def __post_init__(self, sources: EnrichmentSources) -> None:
        if not isinstance(sources, EnrichmentSources):
            raise SemanticEnrichmentError("sources must be an EnrichmentSources record.")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise SemanticEnrichmentError("schema_version must be an integer.")
        if self.schema_version != SEMANTIC_ENRICHMENT_SCHEMA_VERSION:
            raise SemanticEnrichmentError("unsupported schema_version.")
        normalized_set_code = _identifier(self.set_code, "set_code").casefold()
        if normalized_set_code != sources.set_code:
            raise SemanticEnrichmentError("set_code does not match sources.set_code.")
        object.__setattr__(self, "set_code", normalized_set_code)
        # set_source_id has no counterpart in EnrichmentSources; validate it as an identifier only.
        object.__setattr__(self, "set_source_id", _identifier(self.set_source_id, "set_source_id"))
        expected_set_hash = set_source_sha256(sources)
        source_hash = _hash(self.set_source_sha256, "set_source_sha256")
        if source_hash != expected_set_hash:
            raise SemanticEnrichmentError("set_source_sha256 does not match sources.")
        object.__setattr__(self, "set_source_sha256", source_hash)
        created_at, created_parsed = _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "created_at", created_at)

        cards = _tuple(self.cards, "cards")
        if any(not isinstance(pin, CardSourcePin) for pin in cards):
            raise SemanticEnrichmentError("cards must contain CardSourcePin records.")
        cards = _canonical(cards, field_name="cards", key=lambda pin: pin.card_id)
        source_cards = {card.grp_id: card for card in sources.cards}
        if len(cards) != len(source_cards) or {pin.card_id for pin in cards} != set(source_cards):
            raise SemanticEnrichmentError("cards must cover every source card exactly.")
        for pin in cards:
            card = source_cards[pin.card_id]
            if _optional_identity(card.oracle_id, "card.oracle_id") != pin.oracle_id:
                raise SemanticEnrichmentError("card oracle_id does not match its source pin.")
            if _optional_identity(card.collector_number, "card.collector_number") != pin.collector_number:
                raise SemanticEnrichmentError("card collector_number does not match its source pin.")
            if pin.sha256 != card_source_sha256(card):
                raise SemanticEnrichmentError("card source hash does not match its source pin.")
        object.__setattr__(self, "cards", cards)

        guides = _tuple(self.guides, "guides")
        if any(not isinstance(pin, GuideSourcePin) for pin in guides):
            raise SemanticEnrichmentError("guides must contain GuideSourcePin records.")
        guides = _canonical(guides, field_name="guides", key=lambda pin: pin.guide_id)
        source_guides = {guide.guide_id: guide for guide in sources.guides}
        if len(guides) != len(source_guides) or {pin.guide_id for pin in guides} != set(source_guides):
            raise SemanticEnrichmentError("guides must cover every guide source exactly.")
        for pin in guides:
            guide = source_guides[pin.guide_id]
            if pin.url != guide.url:
                raise SemanticEnrichmentError("guide URL does not match its source pin.")
            if pin.retrieved_at != guide.retrieved_at:
                raise SemanticEnrichmentError("guide retrieved_at does not match its source pin.")
            if pin.sha256 != guide.text_sha256:
                raise SemanticEnrichmentError("guide source hash does not match its source pin.")
        object.__setattr__(self, "guides", guides)

        runs = _tuple(self.runs, "runs")
        if not runs:
            raise SemanticEnrichmentError("runs must not be empty.")
        if any(not isinstance(run, ModelRun) for run in runs):
            raise SemanticEnrichmentError("runs must contain ModelRun records.")
        runs = _canonical(runs, field_name="runs", key=lambda run: run.run_id)
        object.__setattr__(self, "runs", runs)
        run_ids = {run.run_id for run in runs}
        for run in runs:
            _, completed_at = _timestamp(run.completed_at, "completed_at")
            if created_parsed < completed_at:
                raise SemanticEnrichmentError("created_at must not precede run completion.")

        oracle_facts = _tuple(self.oracle_facts, "oracle_facts")
        guide_claims = _tuple(self.guide_claims, "guide_claims")
        relationships = _tuple(self.relationships, "relationships")
        rejected_findings = _tuple(self.rejected_findings, "rejected_findings")
        if any(not isinstance(item, OracleFact) for item in oracle_facts):
            raise SemanticEnrichmentError("oracle_facts must contain OracleFact records.")
        if any(not isinstance(item, GuideClaim) for item in guide_claims):
            raise SemanticEnrichmentError("guide_claims must contain GuideClaim records.")
        if any(not isinstance(item, CardRelationship) for item in relationships):
            raise SemanticEnrichmentError("relationships must contain CardRelationship records.")
        if any(not isinstance(item, RejectedFinding) for item in rejected_findings):
            raise SemanticEnrichmentError("rejected_findings must contain RejectedFinding records.")
        oracle_facts = _canonical(oracle_facts, field_name="oracle_facts", key=lambda item: item.finding_id)
        guide_claims = _canonical(guide_claims, field_name="guide_claims", key=lambda item: item.finding_id)
        relationships = _canonical(relationships, field_name="relationships", key=lambda item: item.identity)
        rejected_findings = _canonical(
            rejected_findings,
            field_name="rejected_findings",
            key=lambda item: item.finding_id,
        )
        findings = [
            item.finding_id
            for collection in (oracle_facts, guide_claims, relationships, rejected_findings)
            for item in collection
        ]
        if len(set(findings)) != len(findings):
            raise SemanticEnrichmentError("finding IDs must be globally unique.")
        object.__setattr__(self, "oracle_facts", oracle_facts)
        object.__setattr__(self, "guide_claims", guide_claims)
        object.__setattr__(self, "relationships", relationships)
        object.__setattr__(self, "rejected_findings", rejected_findings)

        card_by_id = source_cards
        guide_by_id = source_guides
        for fact in oracle_facts:
            if fact.card_id not in card_by_id:
                raise SemanticEnrichmentError("Oracle fact references an unknown card.")
            if fact.run_id not in run_ids:
                raise SemanticEnrichmentError("Oracle fact references an unknown run.")
            for evidence in fact.evidence:
                self._validate_oracle_evidence(evidence=evidence, card_by_id=card_by_id)
        for claim in guide_claims:
            if claim.run_id not in run_ids:
                raise SemanticEnrichmentError("guide claim references an unknown run.")
            for card_id in claim.card_ids:
                if card_id not in card_by_id:
                    raise SemanticEnrichmentError("guide claim references an unknown card.")
            for evidence in claim.evidence:
                self._validate_guide_evidence(evidence=evidence, guide_by_id=guide_by_id)
        relationship_identities: set[tuple[str, tuple[int, ...]]] = set()
        for relationship in relationships:
            if relationship.run_id not in run_ids:
                raise SemanticEnrichmentError("relationship references an unknown run.")
            for card_id in relationship.participants:
                if card_id not in card_by_id:
                    raise SemanticEnrichmentError("relationship references an unknown card.")
            evidence_card_ids = {evidence.card_id for evidence in relationship.oracle_evidence}
            if evidence_card_ids != set(relationship.participants):
                raise SemanticEnrichmentError("relationship Oracle evidence must cover participants exactly.")
            for evidence in relationship.oracle_evidence:
                self._validate_oracle_evidence(evidence=evidence, card_by_id=card_by_id)
            for evidence in relationship.guide_evidence:
                self._validate_guide_evidence(evidence=evidence, guide_by_id=guide_by_id)
            if relationship.identity in relationship_identities:
                raise SemanticEnrichmentError("relationships contain duplicate identities.")
            relationship_identities.add(relationship.identity)
        for rejected in rejected_findings:
            if rejected.run_id not in run_ids:
                raise SemanticEnrichmentError("rejected finding references an unknown run.")

        if not isinstance(self.review, ArtifactReview):
            raise SemanticEnrichmentError("review must be an ArtifactReview.")
        confirmed_ids = _tuple(self.confirmed_relationship_ids, "confirmed_relationship_ids")
        confirmed_ids = tuple(_identifier(item, "confirmed_relationship_id") for item in confirmed_ids)
        confirmed_ids = _canonical(
            confirmed_ids,
            field_name="confirmed_relationship_ids",
            key=lambda item: item,
        )
        if self.review.state != "confirmed" and confirmed_ids:
            raise SemanticEnrichmentError("only confirmed artifacts may select relationships.")
        relationships_by_id = {relationship.finding_id: relationship for relationship in relationships}
        for finding_id in confirmed_ids:
            relationship = relationships_by_id.get(finding_id)
            if relationship is None or relationship.review.status is not FindingStatus.ACCEPTED:
                raise SemanticEnrichmentError("confirmed relationship IDs must select accepted relationships.")
        object.__setattr__(self, "confirmed_relationship_ids", confirmed_ids)

        if self.review.reviewed_at is not None:
            _, reviewed_at = _timestamp(self.review.reviewed_at, "reviewed_at")
            if reviewed_at < created_parsed:
                raise SemanticEnrichmentError("reviewed_at must not precede created_at.")

    @staticmethod
    def _validate_oracle_evidence(
        *,
        evidence: OracleEvidence,
        card_by_id: Mapping[int, CardInfo],
    ) -> None:
        if not isinstance(evidence, OracleEvidence):
            raise SemanticEnrichmentError("Oracle evidence must be an OracleEvidence record.")
        card = card_by_id.get(evidence.card_id)
        if card is None:
            raise SemanticEnrichmentError("Oracle evidence references an unknown card.")
        if not card.faces:
            if evidence.face_index is not None:
                raise SemanticEnrichmentError("face_index must be null for a card without faces.")
            selected_text = card.oracle_text
        else:
            if evidence.face_index is None:
                raise SemanticEnrichmentError("face_index is required for a card with faces.")
            if isinstance(evidence.face_index, bool) or not isinstance(evidence.face_index, int):
                raise SemanticEnrichmentError("face_index must be an integer.")
            if evidence.face_index >= len(card.faces):
                raise SemanticEnrichmentError("face_index is outside the card's faces.")
            selected_text = card.faces[evidence.face_index].oracle_text
        if not isinstance(selected_text, str):
            raise SemanticEnrichmentError("Oracle evidence requires source oracle text.")
        if evidence.quote not in selected_text:
            raise SemanticEnrichmentError("Oracle evidence quote is not an exact source substring.")

    @staticmethod
    def _validate_guide_evidence(
        *,
        evidence: GuideEvidence,
        guide_by_id: Mapping[str, GuideSource],
    ) -> None:
        if not isinstance(evidence, GuideEvidence):
            raise SemanticEnrichmentError("guide evidence must be a GuideEvidence record.")
        guide = guide_by_id.get(evidence.guide_id)
        if guide is None:
            raise SemanticEnrichmentError("guide evidence references an unknown guide.")
        if evidence.quote not in guide.text:
            raise SemanticEnrichmentError("guide evidence quote is not an exact source substring.")

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible artifact object."""
        return {
            "schema_version": int(self.schema_version),
            "set_code": self.set_code,
            "set_source_id": self.set_source_id,
            "set_source_sha256": self.set_source_sha256,
            "created_at": self.created_at,
            "cards": [pin.to_json() for pin in self.cards],
            "guides": [pin.to_json() for pin in self.guides],
            "runs": [run.to_json() for run in self.runs],
            "oracle_facts": [fact.to_json() for fact in self.oracle_facts],
            "guide_claims": [claim.to_json() for claim in self.guide_claims],
            "relationships": [relationship.to_json() for relationship in self.relationships],
            "rejected_findings": [finding.to_json() for finding in self.rejected_findings],
            "review": self.review.to_json(),
            "confirmed_relationship_ids": list(self.confirmed_relationship_ids),
        }

    def to_bytes(self) -> bytes:
        """Return canonical compact UTF-8 artifact bytes."""
        return _json_bytes(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any], *, sources: EnrichmentSources) -> Self:
        """Decode and source-validate one artifact object."""
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("artifact must be an object.")
        _keys(
            value,
            {
                "schema_version",
                "set_code",
                "set_source_id",
                "set_source_sha256",
                "created_at",
                "cards",
                "guides",
                "runs",
                "oracle_facts",
                "guide_claims",
                "relationships",
                "rejected_findings",
                "review",
                "confirmed_relationship_ids",
            },
            "artifact",
        )
        return cls(
            schema_version=_required(value, "schema_version"),
            set_code=_required(value, "set_code"),
            set_source_id=_required(value, "set_source_id"),
            set_source_sha256=_required(value, "set_source_sha256"),
            created_at=_required(value, "created_at"),
            cards=tuple(CardSourcePin.from_json(item) for item in _json_array(value["cards"], "cards")),
            guides=tuple(GuideSourcePin.from_json(item) for item in _json_array(value["guides"], "guides")),
            runs=tuple(ModelRun.from_json(item) for item in _json_array(value["runs"], "runs")),
            oracle_facts=tuple(
                OracleFact.from_json(item) for item in _json_array(value["oracle_facts"], "oracle_facts")
            ),
            guide_claims=tuple(
                GuideClaim.from_json(item) for item in _json_array(value["guide_claims"], "guide_claims")
            ),
            relationships=tuple(
                CardRelationship.from_json(item)
                for item in _json_array(value["relationships"], "relationships")
            ),
            rejected_findings=tuple(
                RejectedFinding.from_json(item)
                for item in _json_array(value["rejected_findings"], "rejected_findings")
            ),
            review=ArtifactReview.from_json(_required(value, "review")),
            confirmed_relationship_ids=_json_array(
                value["confirmed_relationship_ids"],
                "confirmed_relationship_ids",
            ),
            sources=sources,
        )

    @classmethod
    def from_bytes(cls, payload: bytes, *, sources: EnrichmentSources) -> Self:
        """Decode strict UTF-8 canonical artifact bytes."""
        if not isinstance(payload, (bytes, bytearray)):
            raise SemanticEnrichmentError("payload must be bytes or bytearray.")
        try:
            text = bytes(payload).decode("utf-8")
            value = json.loads(
                text,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except SemanticEnrichmentError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise SemanticEnrichmentError("payload is not valid UTF-8 JSON.") from error
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("artifact must be a JSON object.")
        return cls.from_json(value, sources=sources)

    @property
    def confirmed_relationships(self) -> tuple[CardRelationship, ...]:
        """Return selected accepted relationships in canonical relationship order."""
        selected = set(self.confirmed_relationship_ids)
        return tuple(relationship for relationship in self.relationships if relationship.finding_id in selected)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticEnrichmentError("duplicate JSON object key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise SemanticEnrichmentError(f"non-finite JSON constant {value!r} is not allowed.")


__all__ = [
    "SEMANTIC_ENRICHMENT_SCHEMA_VERSION",
    "GuideSource",
    "EnrichmentSources",
    "SemanticEnrichmentArtifact",
    "card_source_projection",
    "card_source_sha256",
    "set_source_sha256",
]
