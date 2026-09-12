"""Compile one confirmed semantic-enrichment artifact into a profile enhancement block.

The compiler is the only schema-three writer: it validates one already-decoded
artifact against the exact generation inputs and maps it onto the profile-side
enhancement vocabulary without inventing content.
"""

from __future__ import annotations

import hashlib
import re

from draftomen.carddb import CardDatabase
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    SemanticEnrichmentArtifact,
    set_source_sha256,
)
from draftomen.semantic_enrichment_records import FindingStatus, SemanticEnrichmentError
from draftomen.set_profile import (
    EnhancementCardData,
    SetProfileEnhancement,
    SetProfileSchemaError,
)


ARTIFACT_TYPE_ERROR = "enrichment must be a SemanticEnrichmentArtifact."
SET_CODE_ERROR = "set_code must be a non-empty string."
UNCONFIRMED_ERROR = "The enrichment artifact has not been confirmed."
CANCELLED_ERROR = "The enrichment artifact review was cancelled."
SET_MISMATCH_ERROR = "The enrichment artifact set code does not match the generated set."
CARD_DATA_MISMATCH_ERROR = (
    "The enrichment artifact card data does not match the generation card database."
)
CARD_DATA_IDENTITY_ERROR = (
    "The enrichment artifact card data identity cannot be recorded in a profile."
)
NO_FINDINGS_ERROR = (
    "The enrichment artifact contains no confirmed relationship or accepted mechanic finding."
)
COMPILE_ERROR = "The enrichment artifact cannot be compiled into a profile."
PUBLISHED_IDENTITY_ERROR = (
    "The enrichment artifact carries a local filesystem path where a published identity is required."
)


class ProfileEnhancementError(ValueError):
    """Raised when an enrichment artifact cannot be compiled into a profile."""


_LOCAL_IDENTITY_PREFIXES = ("/", "\\", "~", "./", ".\\", "../", "..\\")
_LOCAL_IDENTITY_DRIVE = re.compile(r"[A-Za-z]:[\\/]")


def published_identity_is_safe(value: str) -> bool:
    """Return whether one enrichment identity can be published without a local path."""

    if value.startswith(_LOCAL_IDENTITY_PREFIXES):
        return False
    return _LOCAL_IDENTITY_DRIVE.match(value) is None


def compile_profile_enhancement(
    *,
    artifact: SemanticEnrichmentArtifact,
    set_code: str,
    card_database: CardDatabase,
) -> SetProfileEnhancement:
    """Compile one confirmed artifact against the generation inputs."""

    if not isinstance(artifact, SemanticEnrichmentArtifact):
        raise ProfileEnhancementError(ARTIFACT_TYPE_ERROR)
    if not isinstance(set_code, str) or not set_code.strip():
        raise ProfileEnhancementError(SET_CODE_ERROR)
    normalized_set = set_code.strip().casefold()
    if artifact.review.state == "cancelled":
        raise ProfileEnhancementError(CANCELLED_ERROR)
    if artifact.review.state != "confirmed":
        raise ProfileEnhancementError(UNCONFIRMED_ERROR)
    if artifact.set_code != normalized_set:
        raise ProfileEnhancementError(SET_MISMATCH_ERROR)
    try:
        if not isinstance(card_database, CardDatabase):
            raise TypeError("card_database must be a CardDatabase.")
        sources = EnrichmentSources(
            set_code=normalized_set,
            cards=tuple(card_database.cards.values()),
            guides=(),
        )
        expected_set_source = set_source_sha256(sources)
    except (SemanticEnrichmentError, TypeError, ValueError) as error:
        raise ProfileEnhancementError(CARD_DATA_MISMATCH_ERROR) from error
    if expected_set_source != artifact.set_source_sha256:
        raise ProfileEnhancementError(CARD_DATA_MISMATCH_ERROR)
    mechanics = tuple(
        claim
        for claim in artifact.guide_claims
        if claim.category == "mechanic" and claim.review.status is FindingStatus.ACCEPTED
    )
    relationships = artifact.confirmed_relationships
    if not mechanics and not relationships:
        raise ProfileEnhancementError(NO_FINDINGS_ERROR)
    referenced_runs = {item.run_id for item in (*mechanics, *relationships)}
    runs = tuple(run for run in artifact.runs if run.run_id in referenced_runs)
    published_identities = (
        *(pin.guide_id for pin in artifact.guides),
        *(value for run in runs for value in (run.run_id, run.provider, run.model)),
    )
    if any(not published_identity_is_safe(value) for value in published_identities):
        raise ProfileEnhancementError(PUBLISHED_IDENTITY_ERROR)
    try:
        card_data = EnhancementCardData(
            source=artifact.set_source_id,
            sha256=artifact.set_source_sha256,
            card_count=len(artifact.cards),
        )
    except SetProfileSchemaError as error:
        raise ProfileEnhancementError(CARD_DATA_IDENTITY_ERROR) from error
    typed = (*artifact.oracle_facts, *artifact.guide_claims, *artifact.relationships)
    confidence = (
        sum(1 for item in typed if item.review.status is FindingStatus.ACCEPTED) / len(typed)
    )
    try:
        return SetProfileEnhancement(
            artifact_schema_version=artifact.schema_version,
            artifact_sha256=hashlib.sha256(artifact.to_bytes()).hexdigest(),
            set_code=artifact.set_code,
            set_source_id=artifact.set_source_id,
            set_source_sha256=artifact.set_source_sha256,
            created_at=artifact.created_at,
            card_data=card_data,
            cards=artifact.cards,
            guides=artifact.guides,
            runs=runs,
            mechanics=mechanics,
            relationships=relationships,
            review=artifact.review,
            confidence=confidence,
        )
    except SetProfileSchemaError as error:
        raise ProfileEnhancementError(COMPILE_ERROR) from error


__all__ = ["ProfileEnhancementError", "compile_profile_enhancement", "published_identity_is_safe"]
