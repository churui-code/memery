"""Strict proposals: reflective interpretations never create source evidence."""
from math import isclose, isfinite
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import MemoryError
from app.life import tool_list
from app.source_units import source_units


class ReflectionUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    domain: Literal['emotion', 'relationship', 'identity', 'belief', 'goal']
    key: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=1500)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=24,
        description='仅使用本次 episodes[].evidence[].id，原文由服务端还原，不输出quote。')
    target_id: UUID | None = None
    expected_revision: int = Field(default=0, ge=0, strict=True)
    intensity: float | None = Field(default=None, ge=0, le=1)
    intensity_direction: Literal['new', 'decrease', 'maintain', 'increase'] | None = Field(default=None,
        description='emotion必填：新建用new；修订方向须相对于目标当前current_intensity，不是历史intensity。其他域省略。')
    half_life_hours: float | None = Field(default=None, ge=.25, le=720)

    @field_validator('evidence_ids', mode='before')
    @classmethod
    def normalize_ids(cls, value):
        return tool_list(value)

    @model_validator(mode='after')
    def valid_update(self):
        if any(not value.strip() for value in (self.key, self.text, self.reason)):
            raise ValueError('blank_reflection_update')
        if (self.target_id is None) != (self.expected_revision == 0):
            raise ValueError('reflection_target_revision_required')
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('duplicate_reflection_evidence')
        if self.domain == 'emotion':
            if self.intensity is None or self.half_life_hours is None or self.intensity_direction is None:
                raise ValueError('reflection_emotion_requires_dynamics')
        elif self.intensity is not None or self.half_life_hours is not None or self.intensity_direction is not None:
            raise ValueError('reflection_dynamics_only_for_emotion')
        return self


class ReflectionProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    updates: list[ReflectionUpdate] = Field(max_length=8)

    @field_validator('updates', mode='before')
    @classmethod
    def normalize_updates(cls, value):
        return tool_list(value)


class ReflectionCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    unit_id: str = Field(min_length=1, max_length=100)
    support: Literal['supported', 'interpretation', 'unsupported', 'conflict']
    evidence_ids: list[UUID] = Field(min_length=1, max_length=24,
        description='只能从本proposal.evidence_ids选取本单元对应的原文，不得引入其他提案的来源。')
    note: str = Field(min_length=1, max_length=400,
        description='简短给出证据对应与判定，核对主体、限定词、金额对象、时间和条件；不输出思考过程。')

    @field_validator('evidence_ids', mode='before')
    @classmethod
    def normalize_ids(cls, value):
        return tool_list(value)

    @model_validator(mode='after')
    def valid_check(self):
        if not self.note.strip():
            raise ValueError('blank_reflection_check')
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('duplicate_reflection_check_evidence')
        return self


class ReflectionVerdict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    proposal_id: str = Field(min_length=1, max_length=64)
    accept: bool = Field(strict=True)
    checks: list[ReflectionCheck] = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator('checks', mode='before')
    @classmethod
    def normalize_checks(cls, value):
        return tool_list(value)

    @field_validator('reason')
    @classmethod
    def reason_has_content(cls, value):
        if not value.strip():
            raise ValueError('blank_reflection_verdict')
        return value


class ReflectionReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reviews: list[ReflectionVerdict] = Field(min_length=1, max_length=8)

    @field_validator('reviews', mode='before')
    @classmethod
    def normalize_reviews(cls, value):
        return tool_list(value)


def validate_reflection_proposal(raw, payload):
    try:
        proposal = ReflectionProposal.model_validate(raw)
        evidence = {str(row['id']): row for event in payload['episodes'] for row in event['evidence']}
        fresh = {str(row['id']) for event in payload['episodes'] if event['pending'] for row in event['evidence']}
        states = {(row['domain'], row.get('state_key', row.get('key'))): row for row in [*payload['self_state'], *payload.get('target_versions', [])]}
        background = {str(row['id']): row for row in payload['self_state']}
        seen_ids, seen_keys = set(), set()
        for update in proposal.updates:
            key = (update.domain, update.key)
            if update.id in seen_ids or key in seen_keys:
                raise ValueError('duplicate_reflection_proposal')
            seen_ids.add(update.id)
            seen_keys.add(key)
            selected = {str(value) for value in update.evidence_ids}
            if not selected <= evidence.keys() or not selected.intersection(fresh):
                raise ValueError('reflection_requires_displayed_new_evidence')
            previous = states.get(key)
            if previous:
                if (str(update.target_id) != str(previous['id'])
                    or update.expected_revision != previous['revision']):
                    raise ValueError('reflection_target_mismatch')
            elif update.target_id is not None:
                raise ValueError('unknown_reflection_target')
            if update.domain == 'emotion':
                if update.target_id is None:
                    if update.intensity_direction != 'new':
                        raise ValueError('reflection_new_emotion_direction')
                else:
                    current = background.get(str(update.target_id), {}).get('current_intensity')
                    if type(current) not in (int, float) or not isfinite(current) or not 0 <= current <= 1:
                        raise ValueError('reflection_emotion_current_intensity_unavailable')
                    difference = update.intensity - current
                    direction = ('maintain' if isclose(update.intensity, current, rel_tol=0, abs_tol=1e-6)
                                 else 'increase' if difference > 0 else 'decrease')
                    if update.intensity_direction != direction:
                        raise ValueError('reflection_emotion_direction_mismatch')
        return proposal
    except (ValueError, TypeError, KeyError) as exc:
        raise MemoryError('reflection_proposal_invalid', '反思提案未通过来源、目标版本或格式校验，没有写入。', 502) from exc


def reflection_review_units(proposal):
    """Server-owned complete sentence units; no model-selected claim omissions."""
    return [{'unit_id': f'{update.id}:{field}:{unit["id"]}', 'proposal_id': update.id,
             'field': field, 'start': unit['start'], 'end': unit['end'], 'text': unit['text']}
            for update in proposal.updates for field in ('text', 'reason')
            for unit in source_units(getattr(update, field))]


def validate_reflection_review(raw, proposal):
    try:
        review = ReflectionReview.model_validate(raw)
        proposals = {row.id: row for row in proposal.updates}
        actual = [row.proposal_id for row in review.reviews]
        if len(actual) != len(set(actual)) or set(actual) != proposals.keys():
            raise ValueError('reflection_review_must_cover_original_proposals')
        units = reflection_review_units(proposal)
        for verdict in review.reviews:
            expected = {row['unit_id'] for row in units if row['proposal_id'] == verdict.proposal_id}
            actual_units = [check.unit_id for check in verdict.checks]
            if len(actual_units) != len(set(actual_units)) or set(actual_units) != expected:
                raise ValueError('reflection_review_must_cover_every_unit')
            allowed = set(proposals[verdict.proposal_id].evidence_ids)
            for check in verdict.checks:
                if not set(check.evidence_ids) <= allowed:
                    raise ValueError('reflection_review_source_not_in_proposal')
            supported = all(check.support in ('supported', 'interpretation') for check in verdict.checks)
            if verdict.accept != supported:
                raise ValueError('reflection_review_accept_conflicts_with_checks')
        return review
    except (ValueError, TypeError, KeyError) as exc:
        raise MemoryError('reflection_review_invalid', '反思复核须逐句覆盖原提案且仅引用原来源，接受结论必须与证据判定一致。', 502) from exc
