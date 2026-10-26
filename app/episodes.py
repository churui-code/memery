"""Validated, evidence-bound descriptions of events within a conversation."""
from hashlib import sha256
from datetime import datetime, timedelta
import re
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from app.life import tool_list


# Details and retrieval cues are two representations of the same source. A
# price, spoken phrase or theme can legitimately be both, so they share labels.
EvidenceKind = Literal['speech', 'phrase', 'action', 'object', 'place', 'person',
    'amount', 'time', 'sensory', 'theme', 'emotion', 'fact', 'other']


class QuotedEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    quote: str = Field(min_length=1, max_length=12000,
        description='必须逐字引用当前用户输入的连续原话；不得使用摘要、助手回复或补写内容。')
    occurrence: int = Field(default=0, ge=0, le=10000)

    @field_validator('quote')
    @classmethod
    def quote_has_content(cls, value):
        if not value.strip():
            raise ValueError('blank_episode_quote')
        return value


class EpisodeDetail(QuotedEvidence):
    kind: EvidenceKind = 'fact'
    meaning: str = Field(default='', max_length=1000)
    importance: float = Field(default=0.2, ge=0, le=1)
    emotional_salience: float = Field(default=0, ge=0, le=1)
    importance_reason: str = Field(default='', max_length=1000)
    emotional_reason: str = Field(default='', max_length=1000)

    @model_validator(mode='before')
    @classmethod
    def normalize_meaning_alias(cls, value):
        # Some providers label explanatory detail text as `text`. It is only
        # metadata: exact evidence must still come from the separate quote.
        if not isinstance(value, dict) or 'text' not in value:
            return value
        result = dict(value)
        text = result.pop('text')
        if 'meaning' in result and result['meaning'] != text:
            raise ValueError('detail_meaning_alias_conflict')
        result['meaning'] = text
        return result

    @model_validator(mode='after')
    def salience_has_basis(self):
        if self.importance != 0.2 and not (self.importance_reason.strip() or self.meaning.strip()):
            raise ValueError('detail_importance_requires_reason')
        if self.emotional_salience > 0 and not (self.emotional_reason.strip() or self.meaning.strip()):
            raise ValueError('detail_emotional_salience_requires_reason')
        return self


class EpisodeCue(QuotedEvidence):
    kind: EvidenceKind = 'phrase'
    text: str = Field(min_length=1, max_length=300)

    @field_validator('text')
    @classmethod
    def text_has_content(cls, value):
        if not value.strip():
            raise ValueError('blank_episode_cue')
        return value


class EpisodeProposal(QuotedEvidence):
    quote: str = Field(min_length=1, max_length=12000,
        description='必填。逐字引用当前用户输入中覆盖这一事件的完整连续证据范围；所有details、cues、time_quote的原文位置必须位于该范围内。')
    title: str = Field(min_length=1, max_length=200)
    gist: str = Field(min_length=1, max_length=2000)
    context_type: Literal['conversation', 'reported', 'hypothetical', 'fiction'] = 'conversation'
    importance: float = Field(default=0.2, ge=0, le=1)
    importance_reason: str = Field(default='', max_length=1000)
    emotional_salience: float = Field(default=0, ge=0, le=1)
    emotional_reason: str = Field(default='', max_length=1000)
    details: list[EpisodeDetail] = Field(default_factory=list, max_length=64)
    cues: list[EpisodeCue] = Field(default_factory=list, max_length=64)
    participants: list[str] = Field(default_factory=list, max_length=30)
    occurred_at: AwareDatetime | None = None
    occurred_until: AwareDatetime | None = None
    time_precision: Literal['unknown','day','range'] = 'unknown'
    time_quote: str | None = Field(default=None, min_length=1, max_length=300)
    target_id: UUID | None = Field(default=None, description='仅用于用户明确补充同一事件的内容，须与已提供目标的context_type一致；继续讨论主题、提问或形成看法应新建本轮讨论事件，不设置target_id。')

    @field_validator('details', 'cues', 'participants', mode='before')
    @classmethod
    def normalize_lists(cls, value):
        return tool_list(value)

    @model_validator(mode='after')
    def valid_description(self):
        if not self.title.strip() or not self.gist.strip() or not self.quote.strip():
            raise ValueError('blank_episode')
        if self.occurred_at is not None and not self.time_quote:
            self.occurred_at = None
            self.occurred_until = None
            self.time_precision = 'unknown'
        if self.importance != 0.2 and not self.importance_reason.strip():
            raise ValueError('episode_importance_requires_reason')
        if self.emotional_salience > 0 and not self.emotional_reason.strip():
            raise ValueError('episode_emotional_salience_requires_reason')
        return self


def locate_quote(text, quote, occurrence=0):
    """Return Unicode code-point offsets; repeated quotations require an index."""
    start = -1
    for _ in range(occurrence + 1):
        start = text.find(quote, start + 1)
        if start < 0:
            raise ValueError('episode_quote_not_in_source')
    return start, start + len(quote)


def source_hash(user_text, assistant_text):
    return sha256((user_text + '\x1f' + assistant_text).encode()).hexdigest()


def source_reliability(context_type, role='user'):
    claim={'reported':'reported_unverified','fiction':'fiction','hypothetical':'hypothetical'}.get(context_type,'conversation_only')
    return {'version':'source-reliability-v1','quote_verified':True,'speaker':role,'claim':claim}


def normalize_event_time(time_quote, recorded_at):
    """Only source-supported calendar precision; model timestamps are not proof."""
    unknown={'occurred_at':None,'occurred_until':None,'time_precision':'unknown'}
    def period(first,last,precision):
        try:
            return {'occurred_at':first,'occurred_until':last+timedelta(days=1),'time_precision':precision}
        except OverflowError:
            return unknown
    if not time_quote:
        return unknown
    zone=ZoneInfo('Asia/Shanghai')
    reference=recorded_at.astimezone(zone)
    matches=list(re.finditer(r'(\d{4})(?:年|[-/])(\d{1,2})(?:月|[-/])(\d{1,2})(?:日|号)?',time_quote))
    dates=[]
    try:
        dates=[datetime(*(int(value) for value in match.groups()),tzinfo=zone) for match in matches]
    except ValueError:
        return unknown
    if len(dates)==2:
        between=time_quote[matches[0].end():matches[1].start()]
        if dates[1]>=dates[0] and re.search(r'至|到|~|～|—|–|-',between):
            return period(dates[0],dates[1],'range')
        return unknown
    if len(dates)>2:
        return unknown
    if dates:
        # A date embedded in an open/approximate range does not establish its
        # boundaries. Retain the original phrase without inventing a duration.
        if re.search(r'左右|前后|以来|之前|之后|以前|以后|大约|大概|至|到|~|～',time_quote):
            return unknown
        day=dates[0]
    else:
        relative=[(word,offset) for word,offset in (('前天',2),('昨天',1),('昨晚',1),('今天',0),('今日',0)) if word in time_quote]
        if len(relative)!=1 or re.search(r'到|至|以前|以后|之前|之后|前后|左右',time_quote):
            return unknown
        day=(reference-timedelta(days=relative[0][1])).replace(hour=0,minute=0,second=0,microsecond=0)
    return period(day,day,'day')


def validate_episode_proposals(proposals, current_text, allowed_existing=()):
    allowed = {str(item.get('id') if isinstance(item, dict) else item) for item in allowed_existing}
    existing = {str(item.get('id')): item for item in allowed_existing if isinstance(item, dict)}
    result = [item if isinstance(item, EpisodeProposal) else EpisodeProposal.model_validate(item) for item in proposals]
    if len(result) > 32:
        raise ValueError('too_many_episodes')
    targets = set()
    for proposal in result:
        scope_start,scope_end=locate_quote(current_text, proposal.quote, proposal.occurrence)
        if proposal.target_id is not None:
            target = str(proposal.target_id)
            if target not in allowed or target in targets:
                raise ValueError('episode_target_not_available')
            target_type = existing.get(target, {}).get('context_type')
            if target_type is not None and target_type != proposal.context_type:
                raise ValueError('episode_target_context_type_mismatch')
            targets.add(target)
        if proposal.time_quote and proposal.time_quote not in proposal.quote:
            raise ValueError('episode_time_outside_event_scope')
        for evidence in [*proposal.details, *proposal.cues]:
            start,end=locate_quote(current_text, evidence.quote, evidence.occurrence)
            if start<scope_start or end>scope_end:
                raise ValueError('episode_detail_outside_event_scope')
    return result
