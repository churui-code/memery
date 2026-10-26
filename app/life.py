"""Evidence-backed experience descriptions and evolving internal interpretations."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


def tool_list(value):
    # M3 occasionally encodes a tool array as {"item": [...]} or a single
    # {"item": value}. Only unwrap this exact representation; validators still
    # check every element, UUID, quote and allowed source independently.
    if isinstance(value,dict) and set(value)=={'item'}:
        return value['item'] if isinstance(value['item'],list) else [value['item']]
    return value


class Anchor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    quote: str = Field(min_length=1, max_length=8000)
    meaning: str = Field(min_length=1, max_length=300)
    occurrence: int = Field(default=0, ge=0, le=10000, strict=True)


class ExperienceDescription(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    context_type: Literal['conversation', 'reported', 'hypothetical', 'fiction'] = 'conversation'
    participants: list[str] = Field(default_factory=list, max_length=30)
    anchors: list[Anchor] = Field(default_factory=list, max_length=32)
    significance: float = Field(default=0.2, ge=0, le=1)
    occurred_at: AwareDatetime | None = None
    time_quote: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator('participants','anchors',mode='before')
    @classmethod
    def normalize_lists(cls,value):
        return tool_list(value)

    @model_validator(mode='after')
    def event_time_has_source(self):
        if self.occurred_at is not None and not self.time_quote:
            self.occurred_at = None
        return self


class RecallQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(default='', max_length=1000)
    after: AwareDatetime | None = None
    before: AwareDatetime | None = None
    time_basis: Literal['recorded_at', 'occurred_at'] = 'recorded_at'

    @model_validator(mode='after')
    def valid_window(self):
        if self.after and self.before and self.after >= self.before:
            raise ValueError('invalid recall window')
        return self


class SelfUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    domain: Literal['emotion', 'relationship', 'identity', 'belief', 'goal']
    key: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=1500)
    reason: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=8000)
    confidence: float = Field(ge=0, le=1)
    source_experience_ids: list[UUID] = Field(default_factory=list, max_length=12,
        description='仅引用实际显示原文的来源：supported_source_experience_ids或history.experience_id。一般优先引用事件ID；不能把溯源source_experience_ids列表当作已看过原文。')
    source_episode_ids: list[UUID] = Field(default_factory=list, max_length=12,
        description='引用本轮提供的experience_evidence中完整episode_id（真实事件，不是legacy_projection）。可以引用提示或梗概支持的认识，不能据此编造未展示细节。')
    intensity: float | None = Field(default=None, ge=0, le=1)
    half_life_hours: float | None = Field(default=None, ge=0.25, le=720)

    @field_validator('source_experience_ids','source_episode_ids',mode='before')
    @classmethod
    def normalize_sources(cls,value):
        return tool_list(value)

    @model_validator(mode='after')
    def emotional_dynamics(self):
        if not self.key.strip() or not self.text.strip() or not self.reason.strip():
            raise ValueError('blank internal state')
        if self.domain == 'emotion':
            if self.intensity is None or self.half_life_hours is None:
                raise ValueError('emotion requires intensity and half life')
        elif self.intensity is not None or self.half_life_hours is not None:
            raise ValueError('only emotions decay numerically')
        return self


def observation_view(row, at=None):
    """A fading present mood never rewrites its historical intensity or cause."""
    result = dict(row)
    if row['domain'] == 'emotion':
        elapsed = max(0, ((at or datetime.now(timezone.utc)) - row['created_at']).total_seconds())
        result['current_intensity'] = round(row['intensity'] * 2 ** (-elapsed / (3600 * row['half_life_hours'])), 6)
    result['epistemic_status'] = 'internal_interpretation'
    return result
