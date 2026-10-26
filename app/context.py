"""Deterministic, inspectable context packing without cutting evidence text."""
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import json
from math import floor
from uuid import UUID

from app.errors import MemoryError


ESTIMATOR_VERSION = 'utf8-half-v1'
_SELF_TOPIC_STOP = frozenset('''我们 你们 他们 自己 我的 这个 那个 这样 那样 这些 那些
一种 一些 一个 这次 那次 当前 之前 先前 后来 现在 今天 昨天 明天 已经 形成
认为 觉得 以为 事情 内容 问题 方面 情况 相关 需要 可以 应该 还是 以及 因为 所以
但是 如果 只是 不同 根据 依据 理解 认识 观点 看法 意义 原因 理由 说明 讨论 故事
合成 表达 经历 记忆 感觉 状态 重要 the and what how this that'''.split())


def _self_topic_terms(text):
    # Use the same Chinese search segmentation as persistence. Generic words
    # about having an opinion do not establish which opinion the user means.
    from app.text import tokens
    return {word for word in tokens(text) if len(word) >= 2 and word not in _SELF_TOPIC_STOP}


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f'unsupported context value: {type(value).__name__}')


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=json_default)


def content_hash(value):
    return sha256(canonical_json(value).encode('utf-8')).hexdigest()


def estimate_tokens(value):
    """A repeatable local estimate, deliberately distinct from provider usage.

    UTF-8 bytes / 2 budgets Chinese at roughly 1.5 estimated tokens per
    character. This is a policy estimate, not a claim about M3's tokenizer.
    Always include serialized structure when estimating a complete request.
    """
    text = value if isinstance(value, str) else canonical_json(value)
    return (len(text.encode('utf-8')) + 1) // 2


def _contains_detail(parent, child):
    """Only verified coordinates within the same source version imply overlap."""
    keys = ('experience_id', 'role', 'source_revision')
    if any(parent.get(key) is None or child.get(key) is None for key in keys):
        return False
    if any(str(parent[key]) != str(child[key]) for key in keys):
        return False
    for item in (parent, child):
        if not isinstance(item.get('start'), int) or not isinstance(item.get('end'), int):
            return False
        if not isinstance(item.get('quote'), str) or item['end'] - item['start'] != len(item['quote']):
            return False
    if not parent['start'] <= child['start'] <= child['end'] <= parent['end']:
        return False
    offset = child['start'] - parent['start']
    return parent['quote'][offset:offset + len(child['quote'])] == child['quote']


def _merge_contained_details(item):
    """Merge within one event only; retain whole parent quotes and child IDs."""
    result = deepcopy(item)
    kept, covered_by, merges = [], {}, []
    indexed = list(enumerate(result.get('details', [])))
    for index, detail in sorted(indexed, key=lambda row: (-len(row[1].get('quote', '')), row[0])):
        parent = next((entry for _, entry in kept if _contains_detail(entry, detail)), None)
        if parent is None:
            kept.append((index, detail))
            continue
        parent_id, child_id = str(parent.get('id')), str(detail.get('id'))
        covered_ids = {str(value) for value in parent.get('covered_detail_ids', [])}
        covered_ids.update(str(value) for value in detail.get('covered_detail_ids', []))
        if child_id != parent_id:
            covered_ids.add(child_id)
        parent['covered_detail_ids'] = sorted(covered_ids)
        for value in covered_ids | {child_id}:
            covered_by[value] = parent_id
        merges.append({'field': 'experience_evidence', 'id': str(item.get('id')),
            'detail_id': child_id, 'retained_detail_id': parent_id, 'reason': 'verified_contained_span'})
    result['details'] = [entry for _, entry in sorted(kept)]
    result['required_detail_ids'] = list(dict.fromkeys(covered_by.get(str(value), str(value))
        for value in result.get('required_detail_ids', [])))
    return result, merges


def _detail_sources(details):
    return [{'kind': 'experience', 'id': str(detail['experience_id']),
        'revision': detail.get('source_revision', 1),
        **{key: detail[key] for key in ('role', 'start', 'end') if key in detail}}
        for detail in details if detail.get('experience_id')]


def evidence_manifest(evidence, source_type='episode'):
    rows = []
    for item in evidence:
        eid = item.get('episode_id', item.get('id'))
        level = item.get('level', 'fragment')
        spans = []
        for detail in item.get('details', []):
            span = {key: detail[key] for key in ('experience_id', 'role', 'start', 'end', 'source_revision') if key in detail}
            span['hash'] = content_hash(detail.get('quote', ''))
            spans.append(span)
            if detail.get('experience_id'):
                rows.append({'source_type': 'experience', 'id': str(detail['experience_id']),
                    'revision': detail.get('source_revision', 1), 'level': 'fragment',
                    'spans': [span], 'hash': span['hash']})
        semantic = {key: item[key] for key in ('id', 'episode_id', 'revision', 'title', 'gist',
            'context_type', 'recorded_at', 'occurred_at', 'occurred_until', 'time_precision', 'time_quote',
            'source_reliability', 'encoding_version', 'level', 'ambiguity', 'source_experience_ids') if key in item}
        semantic['details'] = sorted(item.get('details', []), key=canonical_json)
        if 'source_experience_ids' in semantic:
            semantic['source_experience_ids'] = sorted(str(value) for value in semantic['source_experience_ids'])
        kind = 'experience' if item.get('legacy_projection') else source_type
        rows.append({'source_type': kind, 'id': str(eid), 'revision': item.get('revision', 1),
            'level': level, 'spans': sorted(spans, key=canonical_json), 'hash': content_hash(semantic)})
        represented = {str(d.get('experience_id')) for d in item.get('details', [])}
        for source_id in item.get('source_experience_ids', []):
            if str(source_id) not in represented:
                rows.append({'source_type': 'experience', 'id': str(source_id),
                    'revision': item.get('source_revisions', {}).get(str(source_id), 1),
                    'level': level, 'spans': [], 'hash': content_hash(item.get('gist', item.get('title', '')))})
    return rows


class ContextComposer:
    """Pack complete semantic units under one estimated input budget.

    ``required`` contains extra mandatory request fields such as planned
    changes. System, tools, schema and the current question are mandatory.
    Optional history turns stay intact; facts and self observations are never
    sliced. Episodes can lose optional details, but a requested exact detail
    is kept whole or the entire episode is omitted explicitly.
    """

    def __init__(self, budget_tokens=24000, safety_margin=0.2, cost_estimator=None):
        if not isinstance(budget_tokens, int) or budget_tokens < 1 or not 0 <= safety_margin < 1:
            raise ValueError('invalid context budget')
        self.budget_tokens = budget_tokens
        self.safety_margin = safety_margin
        self.effective_budget = floor(budget_tokens * (1 - safety_margin))
        if cost_estimator is not None and not callable(cost_estimator):
            raise TypeError('cost_estimator must be callable')
        self.cost_estimator = cost_estimator or estimate_tokens
        self.estimation_scope = 'request_payload' if cost_estimator is not None else 'context_payload'

    def estimate(self, payload):
        value = self.cost_estimator(payload)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError('cost_estimator must return a nonnegative integer')
        return value

    @staticmethod
    def _variants(item):
        full = deepcopy(item)
        yield full
        details = full.get('details', [])
        required_ids = {str(i) for i in full.get('required_detail_ids', [])}
        if details:
            compact = deepcopy(full)
            compact['details'] = [d for d in details if str(d.get('id')) in required_ids]
            compact['sources'] = _detail_sources(compact['details'])
            compact['supported_source_experience_ids'] = sorted({str(d['experience_id'])
                for d in compact['details'] if d.get('experience_id')})
            if not compact['details']:
                compact['level'] = 'gist'
            if compact != full:
                yield compact
            if required_ids:
                return
        if full.get('gist'):
            hint = {key: deepcopy(value) for key, value in full.items()
                    if key not in ('gist', 'details', 'ambiguity_candidates')}
            hint['details'] = []
            hint['sources'] = []
            hint['supported_source_experience_ids'] = []
            hint['level'] = 'hint'
            yield hint

    def compose(self, *, system='', tools=None, schema=None, current_input='', history=(),
                facts=(), self_state=(), episodic=(), required=None):
        payload = {'system': system, 'current_question': current_input, 'history': [],
            'memory_evidence': [], 'self_state': [], 'experience_evidence': []}
        if tools is not None:
            payload['tools'] = tools
        if schema is not None:
            payload['schema'] = schema
        if required:
            if set(required).intersection(payload):
                raise ValueError('required fields cannot replace context fields')
            payload.update(deepcopy(required))
        mandatory_cost = self.estimate(payload)
        if mandatory_cost > self.effective_budget:
            raise MemoryError('context_budget_exceeded', '当前输入与必要指令超过上下文预算，请拆分本次内容。', 413)

        units, omitted, deduplicated = [], [], []
        topic_terms = _self_topic_terms(current_input) if self_state else set()
        groups = []
        for row in history:
            if row.get('role') == 'user' or not groups:
                groups.append([])
            groups[-1].append(deepcopy(row))
        for index, group in enumerate(groups):
            units.append(('history', index, group, 110 - min(30, (len(groups) - index - 1) * 5)))
        for field, items, priority in [('memory_evidence', facts, 95), ('self_state', self_state, 85),
                                        ('experience_evidence', episodic, 90)]:
            seen = set()
            for index, item in enumerate(items):
                identity = str(item.get('episode_id', item.get('id', content_hash(item))))
                key = (identity, item.get('revision'))
                if key in seen:
                    deduplicated.append({'field': field, 'id': identity, 'reason': 'duplicate_source'})
                    continue
                seen.add(key)
                score = float(item.get('activation_score', item.get('score', 0)) or 0)
                unit_priority = 130 if field == 'experience_evidence' and item.get('required_detail_ids') else priority
                if field == 'self_state':
                    state_text = ' '.join(str(item.get(key) or '') for key in ('state_key', 'key', 'text'))
                    if topic_terms.intersection(_self_topic_terms(state_text)):
                        unit_priority = 120
                    score = 0
                units.append((field, index, deepcopy(item), unit_priority + min(10, max(0, score) * 10)))

        selected = {field: [] for field in ('history', 'memory_evidence', 'self_state', 'experience_evidence')}
        for field, index, item, priority in sorted(units, key=lambda row: (-row[3], row[0], row[1])):
            candidate = deepcopy(item)
            if field == 'experience_evidence':
                candidate, merges = _merge_contained_details(candidate)
                deduplicated.extend(merges)
                candidate['sources'] = _detail_sources(candidate['details'])
                candidate['supported_source_experience_ids'] = sorted({str(detail['experience_id'])
                    for detail in candidate['details'] if detail.get('experience_id')})
                variants = list(self._variants(candidate))
            else:
                variants = [candidate]
            accepted = None
            for variant in variants:
                prior = payload[field]
                payload[field] = prior + (variant if field == 'history' else [variant])
                if self.estimate(payload) <= self.effective_budget:
                    accepted = variant
                    break
                payload[field] = prior
            if accepted is None:
                omitted.append({'field': field, 'id': str(item.get('id', index)) if isinstance(item, dict) else str(index),
                                'reason': 'budget', 'estimated_tokens': estimate_tokens(item)})
                continue
            selected[field].append((index, accepted))

        for field, values in selected.items():
            values.sort(key=lambda value: value[0])
            payload[field] = [row for _, group in values for row in group] if field == 'history' else [row for _, row in values]
        for item in payload['experience_evidence']:
            retained, covered = [], set()
            for detail in item.get('details', []):
                history_has_quote = any(str(row.get('experience_id')) == str(detail.get('experience_id'))
                    and row.get('role') == detail.get('role') and detail.get('quote', '') in row.get('content', '')
                    for row in payload['history'])
                if history_has_quote:
                    covered.add(str(detail['experience_id']))
                    deduplicated.append({'field': 'experience_evidence', 'id': str(item.get('id')),
                        'detail_id': str(detail.get('id')), 'reason': 'quote_already_in_selected_history'})
                else:
                    retained.append(detail)
            item['details'] = retained
            item['sources'] = _detail_sources(retained)
            item['supported_source_experience_ids'] = sorted(covered | {
                str(detail['experience_id']) for detail in retained if detail.get('experience_id')})
            if not retained and item.get('level') in ('anchor', 'fragment'):
                item['level'] = 'gist' if item.get('gist') else 'hint'
        manifest = evidence_manifest(payload['experience_evidence'])
        for field, kind in [('memory_evidence', 'memory'), ('self_state', 'self')]:
            for row in payload[field]:
                manifest.append({'source_type': kind, 'id': str(row.get('id', content_hash(row))),
                    'revision': row.get('revision', 1), 'level': 'full', 'spans': [], 'hash': content_hash(row)})
        for row in payload['history']:
            if row.get('experience_id'):
                manifest.append({'source_type': 'experience', 'id': str(row['experience_id']),
                    'revision': row.get('source_revision', 1), 'level': 'history', 'spans': [], 'hash': content_hash(row)})
            for source_id in row.get('source_ids', []):
                manifest.append({'source_type': 'memory', 'id': str(source_id), 'revision': None,
                    'level': 'history_dependency', 'spans': [], 'hash': content_hash(row)})
        manifest = sorted({canonical_json(row): row for row in manifest}.values(), key=canonical_json)
        used = self.estimate(payload)
        return {'payload': payload, 'manifest': manifest, 'manifest_hash': content_hash(manifest),
            'omitted': omitted, 'deduplicated': deduplicated,
            'budget': {'configured_tokens': self.budget_tokens, 'safety_margin': self.safety_margin,
                'effective_tokens': self.effective_budget, 'estimated_tokens': used,
                'mandatory_tokens': mandatory_cost, 'remaining_tokens': self.effective_budget - used,
                'estimator_version': ESTIMATOR_VERSION, 'provider_usage': None,
                'estimation_scope': self.estimation_scope, 'section_estimates_scope': 'content_only',
                'section_estimates': {key: estimate_tokens(value) for key, value in payload.items()}}}
