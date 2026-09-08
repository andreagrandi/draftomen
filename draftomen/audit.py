"""Independent append-only audit logging for live draft decisions.
Records preserve scoring inputs, recommendations, and eventual choices.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import Any, TypeAlias

from draftomen import __version__
from draftomen.config import PickEngineConfig
from draftomen.events import (
    DraftCompletedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)
from draftomen.pickengine import (
    ScoredCard,
    ScoredPack,
    render_pick_rationale_concise,
    render_pick_rationale_detailed,
)
from draftomen.pool import DraftState
from draftomen.ranking import RANKING_MODES, rank_scored_cards, validate_ranking_mode
from draftomen.seventeen import SeventeenLandsData, SeventeenLandsFormatData

PathInput: TypeAlias = str | PathLike[str]
Clock: TypeAlias = Callable[[], datetime]
AuditRecord: TypeAlias = dict[str, Any]

AUDIT_DIRECTORY_NAME = "audit"
DRAFT_AUDIT_DIRECTORY_NAME = "drafts"
DRAFT_AUDIT_SCHEMA_VERSION = 1


class DraftAuditError(RuntimeError):
    """Raised when a draft audit record cannot be read or persisted.
    Live callers surface the failure instead of silently losing evidence.
    """


def draft_audit_path(
    *,
    account_id: str,
    draft_id: str,
    app_dir: PathInput,
) -> Path:
    """Return the append-only JSONL path for one account and draft.
    Path segments are validated before being used on disk.
    """

    safe_draft_id = _path_segment(value=draft_id, field_name="draft_id")
    return (
        Path(app_dir)
        / AUDIT_DIRECTORY_NAME
        / DRAFT_AUDIT_DIRECTORY_NAME
        / _path_segment(value=account_id, field_name="account_id")
        / f"{safe_draft_id}.jsonl"
    )


def load_draft_audit_records(
    *,
    account_id: str,
    draft_id: str,
    app_dir: PathInput,
) -> tuple[AuditRecord, ...]:
    """Load and validate every audit record for one draft.
    Malformed lines fail with their one-based line number.
    """

    path = draft_audit_path(
        account_id=account_id,
        draft_id=draft_id,
        app_dir=app_dir,
    )
    if not path.exists():
        return ()

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise DraftAuditError(f"Could not read draft audit log {path}: {error}.") from error

    records: list[AuditRecord] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise DraftAuditError(
                f"Malformed draft audit log {path} at line {line_number}: {error}."
            ) from error

        if not isinstance(value, dict):
            raise DraftAuditError(
                f"Malformed draft audit log {path} at line {line_number}: "
                "expected an object."
            )

        schema_version = value.get("schema_version")
        if schema_version != DRAFT_AUDIT_SCHEMA_VERSION:
            raise DraftAuditError(
                f"Unsupported draft audit schema {schema_version!r} in {path} "
                f"at line {line_number}; expected {DRAFT_AUDIT_SCHEMA_VERSION}."
            )

        record_id = value.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise DraftAuditError(
                f"Malformed draft audit log {path} at line {line_number}: "
                "missing record_id."
            )

        records.append(value)

    return tuple(records)


class DraftAuditStore:
    """Persist stable, independently analyzable records for live drafts.
    Duplicate log scans reuse record ids and leave prior evidence unchanged.
    """

    def __init__(
        self,
        *,
        app_dir: PathInput,
        clock: Clock | None = None,
        app_version: str = __version__,
    ) -> None:
        self.app_dir = app_dir
        self._clock = _system_clock if clock is None else clock
        self._app_version = app_version
        self._indexed_paths: set[Path] = set()
        self._known_record_ids: dict[Path, set[str]] = {}
        self._closed_decision_ids: dict[Path, set[str]] = {}
        self._latest_evaluations: dict[tuple[Path, str], AuditRecord] = {}

    def record_draft_started(self, *, state: DraftState) -> Path:
        """Record one draft's durable identity and starting metadata.
        Repeated course snapshots resolve to the same stable record.
        """

        payload: AuditRecord = {
            "course_id": state.course_id,
            "started_at": state.started_at,
        }
        record_id = _record_id(
            prefix="draft-started",
            value={
                "account_id": state.account_id,
                "draft_id": state.draft_id,
                **payload,
            },
        )
        return self._append(
            state=state,
            record_type="draft_started",
            record_id=record_id,
            payload=payload,
        )

    def record_decision(
        self,
        *,
        state: DraftState,
        event: PackOfferedEvent,
        scored_pack: ScoredPack,
        config: PickEngineConfig,
        ratings_data: SeventeenLandsData | None,
    ) -> Path:
        """Record one distinct scoring evaluation for an offered pack.
        Evaluations after a recorded choice are ignored as retrospective rescans.
        """

        path = self.path_for(state=state)
        self._ensure_index(path=path)
        decision_id = _decision_id(
            state=state,
            pack_number=event.pack_number,
            pick_number=event.pick_number,
        )
        if decision_id in self._closed_decision_ids[path]:
            return path

        evaluation = _decision_payload(
            state=state,
            event=event,
            scored_pack=scored_pack,
            config=config,
            ratings_data=ratings_data,
            app_version=self._app_version,
            decision_id=decision_id,
        )
        evaluation_id = _record_id(
            prefix="evaluation",
            value=_evaluation_identity_payload(evaluation=evaluation),
        )
        payload = {
            **evaluation,
            "evaluation_id": evaluation_id,
        }
        return self._append(
            state=state,
            record_type="decision_evaluated",
            record_id=evaluation_id,
            payload=payload,
        )

    def record_choice(
        self,
        *,
        state: DraftState,
        event: PickMadeEvent,
        ranking_mode: str,
    ) -> Path:
        """Record the actual Arena pick and recommendation visible at that time.
        The choice links to the most recent persisted evaluation when available.
        """

        mode = validate_ranking_mode(ranking_mode=ranking_mode)
        path = self.path_for(state=state)
        self._ensure_index(path=path)
        decision_id = _decision_id(
            state=state,
            pack_number=event.pack_number,
            pick_number=event.pick_number,
        )
        evaluation = self._latest_evaluations.get((path, decision_id))
        recommended_grp_id = _recommended_grp_id(
            evaluation=evaluation,
            ranking_mode=mode,
        )
        payload: AuditRecord = {
            "decision_id": decision_id,
            "evaluation_id": (
                None if evaluation is None else evaluation.get("evaluation_id")
            ),
            "pack_number": event.pack_number,
            "pick_number": event.pick_number,
            "chosen_grp_id": event.chosen_grp_id,
            "ranking_mode": mode,
            "recommended_grp_id": recommended_grp_id,
            "recommendation_followed": (
                None
                if recommended_grp_id is None
                else recommended_grp_id == event.chosen_grp_id
            ),
        }
        record_id = _record_id(
            prefix="choice",
            value={
                "decision_id": decision_id,
                "chosen_grp_id": event.chosen_grp_id,
            },
        )
        return self._append(
            state=state,
            record_type="choice_made",
            record_id=record_id,
            payload=payload,
        )

    def record_draft_completed(
        self,
        *,
        state: DraftState,
        event: DraftCompletedEvent,
    ) -> Path:
        """Record final pool contents and the completion signal type.
        Repeated completion payloads share one stable record.
        """

        payload: AuditRecord = {
            "pack_number": event.pack_number,
            "pick_number": event.pick_number,
            "picked_grp_ids": list(event.picked_grp_ids),
            "pick_count": len(event.picked_grp_ids),
            "inferred": event.inferred,
            "completed_at": state.completed_at,
        }
        record_id = _record_id(
            prefix="draft-completed",
            value={
                "account_id": state.account_id,
                "draft_id": state.draft_id,
                **payload,
            },
        )
        return self._append(
            state=state,
            record_type="draft_completed",
            record_id=record_id,
            payload=payload,
        )

    def path_for(self, *, state: DraftState) -> Path:
        """Return the audit path associated with one persisted draft state.
        The path is resolved without creating files or directories.
        """

        return draft_audit_path(
            account_id=state.account_id,
            draft_id=state.draft_id,
            app_dir=self.app_dir,
        )

    def _append(
        self,
        *,
        state: DraftState,
        record_type: str,
        record_id: str,
        payload: Mapping[str, Any],
    ) -> Path:
        path = self.path_for(state=state)
        self._ensure_index(path=path)
        if record_id in self._known_record_ids[path]:
            return path

        record: AuditRecord = {
            "schema_version": DRAFT_AUDIT_SCHEMA_VERSION,
            "record_id": record_id,
            "record_type": record_type,
            "recorded_at": self._now_iso(),
            "app_version": self._app_version,
            "account_id": state.account_id,
            "draft_id": state.draft_id,
            "event_name": state.event_name,
            "set_code": state.set_code,
            **dict(payload),
        }
        encoded = (
            json.dumps(
                record,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                _write_all(descriptor=descriptor, data=encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise DraftAuditError(
                f"Could not append draft audit record to {path}: {error}."
            ) from error

        self._remember_record(path=path, record=record)
        return path

    def _ensure_index(self, *, path: Path) -> None:
        if path in self._indexed_paths:
            return

        self._known_record_ids[path] = set()
        self._closed_decision_ids[path] = set()
        account_id = path.parent.name
        draft_id = path.stem
        for record in load_draft_audit_records(
            account_id=account_id,
            draft_id=draft_id,
            app_dir=self.app_dir,
        ):
            self._remember_record(path=path, record=record)

        self._indexed_paths.add(path)

    def _remember_record(self, *, path: Path, record: AuditRecord) -> None:
        record_id = record["record_id"]
        self._known_record_ids[path].add(record_id)
        record_type = record.get("record_type")
        decision_id = record.get("decision_id")
        if record_type == "decision_evaluated" and isinstance(decision_id, str):
            self._latest_evaluations[(path, decision_id)] = record
        elif record_type == "choice_made" and isinstance(decision_id, str):
            self._closed_decision_ids[path].add(decision_id)

    def _now_iso(self) -> str:
        return self._clock().astimezone(UTC).isoformat()


def _decision_payload(
    *,
    state: DraftState,
    event: PackOfferedEvent,
    scored_pack: ScoredPack,
    config: PickEngineConfig,
    ratings_data: SeventeenLandsData | None,
    app_version: str,
    decision_id: str,
) -> AuditRecord:
    ranked_cards = {
        mode: rank_scored_cards(cards=scored_pack.cards, ranking_mode=mode)
        for mode in RANKING_MODES
    }
    recommendation = ranked_cards["score"][0] if ranked_cards["score"] else None
    rankings = {
        mode: [card.card.grp_id for card in cards]
        for mode, cards in ranked_cards.items()
    }
    return {
        "decision_id": decision_id,
        "pack_number": event.pack_number,
        "pick_number": event.pick_number,
        "pick_index": scored_pack.commitment.pick_index,
        "offered_grp_ids": list(event.offered_grp_ids),
        "pool_before_pick": list(event.pool_grp_ids),
        "source_summary": scored_pack.source_summary,
        "comparison_summary": scored_pack.comparison_summary,
        "algorithm": {
            "name": "draftomen.pickengine.PickEngine",
            "app_version": app_version,
            "config": asdict(config),
            "features": {
                "splash_enabled": scored_pack.splash_state.enabled,
            },
        },
        "ratings_snapshot": _ratings_snapshot(ratings_data=ratings_data),
        "normalization": {
            "lower_rating": scored_pack.normalization.lower_rating,
            "upper_rating": scored_pack.normalization.upper_rating,
            "neutral_rating": scored_pack.normalization.neutral_rating,
        },
        "commitment": {
            "pick_index": scored_pack.commitment.pick_index,
            "pool_size": scored_pack.commitment.pool_size,
            "color_weights": dict(scored_pack.commitment.color_weights),
            "inferred_pair": scored_pack.commitment.inferred_pair,
            "level": scored_pack.commitment.level,
            "phase": scored_pack.commitment.phase,
            "locked": scored_pack.commitment.locked,
        },
        "splash_state": asdict(scored_pack.splash_state),
        "role_ledger": (
            None
            if scored_pack.role_ledger is None
            else scored_pack.role_ledger.to_json()
        ),
        "context_provenance": _context_provenance(
            scored_pack=scored_pack,
        ),
        "rankings": rankings,
        "recommended_grp_id": (
            None if recommendation is None else recommendation.card.grp_id
        ),
        "recommendation": _recommendation_payload(scored_card=recommendation),
        "candidates": [
            _candidate_payload(scored_card=card, rank=rank)
            for rank, card in enumerate(scored_pack.cards, start=1)
        ],
    }


def _context_provenance(
    *,
    scored_pack: ScoredPack,
) -> AuditRecord | None:
    context = scored_pack.scoring_context
    if context is None:
        return None

    profile = context.set_profile
    stage = context.stage
    return {
        "stage": {
            "pack_number": stage.pack_number,
            "pick_number": stage.pick_number,
            "global_pick_index": stage.global_pick_index,
            "estimated_remaining_picks": stage.estimated_remaining_picks,
        },
        "profile": {
            "set_code": profile.set_code,
            "format": profile.event_format,
            "profile_version": profile.profile_version,
            "maturity": profile.maturity.value,
            "confidence": profile.confidence,
            "fingerprint": profile.fingerprint,
            "source": profile.source.to_json(),
        },
    }


def _recommendation_payload(*, scored_card: ScoredCard | None) -> AuditRecord | None:
    if scored_card is None:
        return None
    return {
        "grp_id": scored_card.card.grp_id,
        "score": scored_card.score,
        "source_label": scored_card.source_label,
        "color_fit": scored_card.color_fit,
        "contextual_breakdown": scored_card.contextual_breakdown.to_json(),
        "contextual_evidence": list(scored_card.contextual_evidence),
        "contextual_pair": scored_card.contextual_pair,
        "contextual_theme": scored_card.contextual_theme,
        "contextual_profile_maturity": scored_card.contextual_profile_maturity,
        "contextual_profile_confidence": scored_card.contextual_profile_confidence,
        **_rationale_fields(scored_card=scored_card),
    }


def _rationale_fields(*, scored_card: ScoredCard) -> AuditRecord:
    return {
        "rationale": scored_card.rationale.to_json(),
        "concise_explanation": render_pick_rationale_concise(
            scored_card=scored_card,
        ),
        "explanation": render_pick_rationale_detailed(
            scored_card=scored_card,
        ),
    }


def _candidate_payload(*, scored_card: ScoredCard, rank: int) -> AuditRecord:
    rating = scored_card.rating
    card = scored_card.card
    return {
        "rank": rank,
        "grp_id": card.grp_id,
        "name": card.name,
        "colors": list(card.colors),
        "mana_value": card.mana_value,
        "rarity": card.rarity,
        "types": list(card.types),
        "unknown": card.unknown,
        **_rationale_fields(scored_card=scored_card),
        "metadata": {
            "oracle_text": card.oracle_text,
            "keywords": list(card.keywords),
            "type_line": card.type_line,
            "subtypes": list(card.subtypes),
            "layout": card.layout,
            "faces": [face.to_json() for face in card.faces],
            "mana_cost": card.mana_cost,
            "produced_mana": list(card.produced_mana),
            "set": card.set_code,
            "collector_number": card.collector_number,
            "arena_id": card.arena_id,
            "source_provenance": list(card.source_provenance),
        },
        "offered_index": scored_card.original_index,
        "rating": {
            "grp_id": rating.grp_id,
            "name": rating.name,
            "color": rating.color,
            "rarity": rating.rarity,
            "average_last_seen_at": rating.average_last_seen_at,
            "gih_win_rate": rating.gih_win_rate,
            "opening_hand_win_rate": rating.opening_hand_win_rate,
            "drawn_improvement_win_rate": rating.drawn_improvement_win_rate,
            "sample_counts": rating.sample_counts.to_json(),
            "letter_grade": rating.letter_grade,
            "neutral_prior_score": rating.neutral_prior_score,
            "neutral_prior": rating.neutral_prior,
            "source": {
                "requested_format": rating.metadata.requested_format,
                "source": rating.metadata.source,
                "source_format": rating.metadata.source_format,
                "fallback_reason": rating.metadata.fallback_reason,
            },
        },
        "scoring": {
            "base_rating": scored_card.base_rating,
            "base_score": scored_card.base_score,
            "color_fit": scored_card.color_fit,
            "color_factor": scored_card.color_factor,
            "adjusted_rating": scored_card.adjusted_rating,
            "contextual_breakdown": scored_card.contextual_breakdown.to_json(),
            "contextual_evidence": list(scored_card.contextual_evidence),
            "contextual_pair": scored_card.contextual_pair,
            "contextual_theme": scored_card.contextual_theme,
            "contextual_profile_maturity": scored_card.contextual_profile_maturity,
            "contextual_profile_confidence": (
                scored_card.contextual_profile_confidence
            ),
            "raw_score": scored_card.raw_score,
            "score": scored_card.score,
            "source_label": scored_card.source_label,
            "pair_tiebreaker_pair": scored_card.pair_tiebreaker_pair,
            "pair_tiebreaker_win_rate": scored_card.pair_tiebreaker_win_rate,
            "pair_tiebreaker_weight": scored_card.pair_tiebreaker_weight,
            "score_sort_index": scored_card.score_sort_index,
        },
        "splash": asdict(scored_card.splash),
    }


def _evaluation_identity_payload(*, evaluation: AuditRecord) -> AuditRecord:
    """Keep evaluation ids stable as rationale fields are added additively."""

    identity = dict(evaluation)
    identity.pop("comparison_summary", None)
    recommendation = identity.get("recommendation")
    if isinstance(recommendation, dict):
        identity["recommendation"] = _without_rationale_fields(
            payload=recommendation,
        )
    candidates = identity.get("candidates")
    if isinstance(candidates, list):
        identity["candidates"] = [
            (
                _without_rationale_fields(payload=candidate)
                if isinstance(candidate, dict)
                else candidate
            )
            for candidate in candidates
        ]
    return identity


def _without_rationale_fields(*, payload: Mapping[str, Any]) -> AuditRecord:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"rationale", "concise_explanation", "explanation"}
    }




def _ratings_format_snapshot(
    *,
    dataset: SeventeenLandsFormatData,
) -> AuditRecord:
    return {
        "event_format": dataset.event_format,
        "fetched_at": dataset.fetched_at.astimezone(UTC).isoformat(),
    }


def _ratings_snapshot(*, ratings_data: SeventeenLandsData | None) -> AuditRecord | None:
    if ratings_data is None:
        return None

    return {
        "set_code": ratings_data.set_code,
        "requested_format": ratings_data.requested_format,
        "primary": _ratings_format_snapshot(dataset=ratings_data.primary),
        "fallback": (
            None
            if ratings_data.fallback is None
            else _ratings_format_snapshot(dataset=ratings_data.fallback)
        ),
        "pair_card_ratings": {
            pair: _ratings_format_snapshot(dataset=dataset)
            for pair, dataset in sorted(ratings_data.pair_card_ratings.items())
        },
        "pair_win_rates": {
            pair: record.to_json()
            for pair, record in sorted(ratings_data.pair_win_rates.items())
        },
    }


def _decision_id(
    *,
    state: DraftState,
    pack_number: int,
    pick_number: int,
) -> str:
    return _record_id(
        prefix="decision",
        value={
            "account_id": state.account_id,
            "draft_id": state.draft_id,
            "pack_number": pack_number,
            "pick_number": pick_number,
        },
    )


def _recommended_grp_id(
    *,
    evaluation: AuditRecord | None,
    ranking_mode: str,
) -> int | None:
    if evaluation is None:
        return None

    rankings = evaluation.get("rankings")
    if not isinstance(rankings, dict):
        return None

    ranked_grp_ids = rankings.get(ranking_mode)
    if not isinstance(ranked_grp_ids, list) or not ranked_grp_ids:
        return None

    grp_id = ranked_grp_ids[0]
    return grp_id if isinstance(grp_id, int) else None


def _record_id(*, prefix: str, value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _write_all(*, descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("append returned no bytes")
        view = view[written:]


def _path_segment(*, value: str, field_name: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise DraftAuditError(f"Invalid {field_name}; cannot be used as a path segment.")

    return value


def _system_clock() -> datetime:
    return datetime.now(tz=UTC)
