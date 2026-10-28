"""Read-only cue retrieval, temporal activation and evidence presentation."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
import re

from app.context import ContextComposer, ESTIMATOR_VERSION, content_hash


POLICY_VERSION = 'episodic-activation-v1'


@dataclass(frozen=True)
class RecallPolicy:
    version: str = POLICY_VERSION
    candidate_limit: int = 80
    association_limit: int = 20
    selected_limit: int = 12
    natural_threshold: float = 0.36
    deep_threshold: float = 0.14


def as_datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value is not None and value.tzinfo is None:
        raise ValueError('recall time must have a timezone')
    return value


def lexical_terms(text):
    parts = re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+', (text or '').lower())
    result = set()
    for part in parts:
        if re.fullmatch(r'[\u3400-\u9fff]+', part):
            result.update(part[i:i + 2] for i in range(len(part) - 1))
            if len(part) == 1:
                result.add(part)
        else:
            result.add(part)
    return result - {'什么', '记得', '记忆', '一下', '时候', '我们', '那个', '这个', '怎么', '多少'}


def _clamp(value):
    return max(0.0, min(1.0, float(value or 0)))


def canonical_cue(cue):
    return (cue.get('kind'), cue.get('text'), cue.get('quote'))


def in_time_window(item, window):
    basis = window.get('time_basis', 'recorded_at')
    if basis not in ('recorded_at', 'occurred_at'):
        raise ValueError('invalid recall time basis')
    after, before = as_datetime(window.get('after')), as_datetime(window.get('before'))
    when = as_datetime(item.get(basis))
    until = as_datetime(item.get('occurred_until')) if basis == 'occurred_at' else None
    return when is not None and (after is None or (until > after if until is not None else when >= after)) and (before is None or when < before)


def activation_components(item, query, at):
    importance = _clamp(item.get('importance', item.get('significance', .2)))
    emotional = _clamp(item.get('emotional_salience', 0))
    recorded = as_datetime(item.get('recorded_at')) or at
    rehearsed = as_datetime(item.get('last_rehearsed_at'))
    latest = max(recorded, rehearsed) if rehearsed else recorded
    age_days = max(0.0, (at - latest).total_seconds() / 86400)
    half_life_days = 7 + 358 * importance ** 2
    base = 2 ** (-age_days / half_life_days)
    query_terms = lexical_terms(query)
    searchable = ' '.join([item.get('title', ''), item.get('gist', ''),
        *(cue.get('text', '') for cue in item.get('cues', [])),
        *(detail.get('quote', '') for detail in item.get('details', []))])
    # A generic two-character cue is weak even when it covers the whole
    # question. Several independent matching cues provide stronger evidence.
    overlap = len(query_terms & lexical_terms(searchable)) / max(4, len(query_terms))
    similarity = _clamp(item.get('similarity', 0))
    semantic = _clamp((similarity - .42) / .43)
    matched = []
    for cue in item.get('matched_cues', []):
        text = cue.get('text', '')
        # Additional search queries may discover a candidate, but its final
        # activation must still match the user's current question.
        if text and (text.lower() in query.lower() or (len(query.strip()) >= 2 and query.lower() in text.lower())):
            matched.append(cue)
    distinctive = any(int(cue.get('frequency', 0) or 0) == 1 and len(cue.get('text', '').strip()) >= 3
                      and cue['text'].lower() in query.lower() for cue in matched)
    cue_strength = max((min(1.0, len(cue.get('text', '')) / 4) / sqrt(max(1, int(cue.get('frequency', 1))))
                        for cue in matched), default=0.0)
    relevance = max(overlap, semantic * .85, cue_strength)
    if not query.strip():
        relevance = .65
    score = .50 * relevance + .30 * base + .12 * importance + .08 * emotional
    if distinctive:
        score = max(score, .82 + .1 * importance)
    reasons = []
    if distinctive:
        reasons.append('distinctive_cue')
    if base >= .5:
        reasons.append('recently_active')
    if overlap:
        reasons.append('lexical_match')
    if semantic:
        reasons.append('semantic_match')
    if importance >= .7:
        reasons.append('important_event')
    return {'age_days': round(age_days, 6), 'half_life_days': round(half_life_days, 6),
        'base_activation': round(base, 6), 'importance': importance, 'emotional_salience': emotional,
        'lexical_match': round(overlap, 6), 'semantic_match': round(semantic, 6),
        'cue_strength': round(cue_strength, 6), 'relevance': round(relevance, 6),
        'distinctive_cue': distinctive, 'activation_score': round(min(1.0, score), 6), 'reasons': reasons}


def present_episode(item, components):
    """Present retrieved source spans; the shared composer enforces the budget."""
    details = list(item.get('details', []))
    existing = {(str(value.get('experience_id')), value.get('quote')) for value in details}
    for cue in item.get('cues', []):
        if cue.get('quote') and (str(cue.get('experience_id')), cue['quote']) not in existing:
            details.append({**cue, 'kind': 'quote' if cue.get('kind') == 'phrase' else cue.get('kind', 'other'),
                'meaning': '原文中的检索线索', 'importance': item.get('importance', .2),
                'emotional_salience': 0})
    chosen = [deepcopy(detail) for detail in details if detail.get('quote')]
    for detail in chosen:
        detail.setdefault('id', content_hash(detail))
    # Do not silently replace retrieved evidence with a gist when it cannot fit.
    required_ids = [str(detail['id']) for detail in chosen]
    if chosen:
        level = 'fragment'
    elif components['activation_score'] >= .36:
        level = 'gist'
    else:
        level = 'hint'
    result = {key: deepcopy(item[key]) for key in ('id', 'revision', 'title', 'context_type', 'recorded_at',
        'occurred_at', 'occurred_until', 'time_precision', 'time_quote', 'source_reliability', 'encoding_version',
        'source_experience_ids', 'source_revisions', 'legacy_projection') if key in item}
    result.update(episode_id=str(item['id']), level=level, details=chosen,
        required_detail_ids=required_ids, activation_score=components['activation_score'],
        epistemic_status='retrieved_evidence', ambiguity=False)
    if level != 'hint':
        result['gist'] = item.get('gist', '')
    result['sources'] = [{'kind': 'experience', 'id': str(detail['experience_id']),
        'revision': detail.get('source_revision', 1),
        **{key: detail[key] for key in ('role', 'start', 'end') if key in detail}}
        for detail in chosen if detail.get('experience_id')]
    return result


class RecallEngine:
    def __init__(self, repo, embeddings, policy=None):
        self.repo = repo
        self.embeddings = embeddings
        self.policy = policy if isinstance(policy, RecallPolicy) else RecallPolicy(**(policy or {}))

    def _now(self):
        clock = getattr(self.repo, 'clock', None)
        return clock.now() if clock is not None else datetime.now(timezone.utc)

    def preview(self, query, *, at=None, mode='natural', budget_tokens=24000, cloud_only=True,
                after=None, before=None, time_basis='recorded_at', conversation_id=None,
                expand=True, extra_queries=(), system='', tools=None, schema=None,
                current_input=None, history=(), facts=(), self_state=(), required=None, exact=False,
                cost_estimator=None):
        if mode not in ('natural', 'deep'):
            raise ValueError('invalid recall mode')
        candidate_limit = min(self.policy.candidate_limit * 2, 320) if mode == 'deep' else self.policy.candidate_limit
        at = as_datetime(at) or self._now()
        after, before = as_datetime(after), as_datetime(before)
        if after is not None and before is not None and after >= before:
            raise ValueError('invalid recall time range')
        if time_basis not in ('recorded_at', 'occurred_at'):
            raise ValueError('invalid recall time basis')
        def visible(item, temporal=False):
            if item.get('deleted_at') or item.get('context_stale') or (cloud_only and item.get('allow_cloud') is False):
                return False
            if temporal and (after is not None or before is not None):
                when = as_datetime(item.get(time_basis))
                until = as_datetime(item.get('occurred_until')) if time_basis == 'occurred_at' else None
                if when is None or (after is not None and (until <= after if until is not None else when < after)) or (before is not None and when >= before):
                    return False
            return True
        vector = None
        embedding_status = 'unavailable'
        if query.strip() and self.embeddings is not None:
            try:
                vector = self.embeddings.embed([query], query=True)[0]
                embedding_status = 'ready'
            except Exception:
                pass
        base_filters = {'cloud_only': cloud_only, 'after': after, 'before': before,
            'time_basis': time_basis, 'conversation_id': conversation_id}
        items = self.repo.episode_candidates(query, vector=vector, limit=candidate_limit, exact=exact, **base_filters)
        found = {str(item['id']): deepcopy(item) for item in items if visible(item)}
        time_windows = []
        for request in list(extra_queries)[:4]:
            request = {'query': request} if isinstance(request, str) else dict(request)
            extra_query = request.get('query', '')
            filters = {**base_filters, **{key: value for key, value in request.items()
                        if key in ('after', 'before', 'time_basis', 'conversation_id')}}
            window = {key: filters[key] for key in ('after', 'before', 'time_basis')}
            bounded = window['after'] is not None and window['before'] is not None
            if bounded:
                window['after'], window['before'] = as_datetime(window['after']), as_datetime(window['before'])
                if window['after'] >= window['before']:
                    raise ValueError('invalid recall time range')
                time_windows.append(window)
            # Expansion is an index lookup, not a second embedding model call.
            for item in self.repo.episode_candidates(extra_query, vector=None, limit=candidate_limit, exact=exact, **filters):
                if not visible(item):
                    continue
                key = str(item['id'])
                if key not in found:
                    value = deepcopy(item)
                    value['similarity'] = 0
                    value['discovered_by_expansion'] = True
                    found[key] = value
                found[key].setdefault('_expansion_queries', []).append(extra_query)
                if bounded and in_time_window(item, window):
                    found[key].setdefault('_time_windows', []).append(window)
                existing = {canonical_cue(cue) for cue in found[key].get('matched_cues', [])}
                found[key].setdefault('matched_cues', []).extend(cue for cue in item.get('matched_cues', [])
                    if canonical_cue(cue) not in existing)
        explicit_time = bool(re.search(r'昨天|昨日|前天|今天|今日|上周|上个月|去年|\d{4}年|\d{4}-\d{2}-\d{2}', query))
        memory_intent = bool(re.search(r'记得|记不记得|回忆|聊过|聊了|说过|说了|做了|发生|经历|那次|那天|当时', query))
        time_recall_question = bool(re.search(r'什么|哪些|哪天|哪次|多少|怎么|是否|有没有|吗|[？?]|回忆|记得|记不记得|告诉我|帮我找|查一下|找一下', query))
        restrict_to_time_windows = bool(explicit_time and time_windows and memory_intent and time_recall_question)
        if restrict_to_time_windows:
            found = {key: item for key, item in found.items() if any(in_time_window(item, window) for window in time_windows)}
        scored = {key: activation_components(item, query, at) for key, item in found.items()}
        for key, item in found.items():
            original = scored[key]
            windows = item.get('_time_windows', [])
            if windows and (memory_intent or original['relevance'] > .08):
                strength = max(.85 if (window['before'] - window['after']).total_seconds() <= 31*86400 else .65 for window in windows)
                original['relevance'] = max(original['relevance'], strength)
                original['activation_score'] = max(original['activation_score'], round(min(1,
                    .5*strength + .3*original['base_activation'] + .12*original['importance'] + .08*original['emotional_salience']), 6))
                original['time_cue_strength'] = strength
                original['reasons'].append('bounded_time_cue')
            if original['relevance'] <= .08 and not re.search(r'那|这|上次|之前|当时|原话|刚才|刚刚|前面', query):
                continue
            expanded = [activation_components(item, expanded_query, at)
                        for expanded_query in item.get('_expansion_queries', []) if expanded_query.strip()]
            if expanded:
                best = max(expanded, key=lambda value: value['activation_score'])
                if best['activation_score'] * .85 > original['activation_score']:
                    original['activation_score'] = round(best['activation_score'] * .85, 6)
                    original['relevance'] = max(original['relevance'], round(best['relevance'] * .85, 6))
                    original['reasons'].append('planned_context_cue')
        threshold = self.policy.deep_threshold if mode == 'deep' else self.policy.natural_threshold
        eligible = lambda score: score['activation_score'] >= threshold and (score['relevance'] > .08 or not query.strip())
        seeds = [key for key, score in sorted(scored.items(), key=lambda pair: (-pair[1]['activation_score'], pair[0])) if eligible(score)]
        # Association lookup has no conversation filter in the repository
        # contract, so a conversation-scoped request must not broaden it.
        if expand and seeds and conversation_id is None and hasattr(self.repo, 'episode_associated'):
            for item in self.repo.episode_associated(seeds[:8], cloud_only=cloud_only, limit=self.policy.association_limit):
                if not visible(item, temporal=True):
                    continue
                if restrict_to_time_windows and not any(in_time_window(item, window) for window in time_windows):
                    continue
                key = str(item['id'])
                if key in found and 'correction' not in item.get('association_kinds', []):
                    continue
                value = deepcopy(found.get(key, item))
                value['similarity'] = 0
                comp = deepcopy(scored[key]) if key in scored else activation_components(value, query, at)
                if 'correction' in item.get('association_kinds', []):
                    comp['relevance'] = max(comp['relevance'], .65)
                    comp['activation_score'] = max(comp['activation_score'], .62)
                    comp['reasons'].append('explicit_correction_source')
                # One hop can help an already matching event. It cannot awaken
                # an unrelated story solely because both happened in a park.
                if comp['relevance'] > .08:
                    comp['activation_score'] = round(min(1, comp['activation_score'] + .08), 6)
                    comp['reasons'].append('one_hop_association')
                value['association_hops'] = 1
                found[key], scored[key] = value, comp
        ordered = sorted(found, key=lambda key: (-scored[key]['activation_score'], key))
        selected_ids = [key for key in ordered if eligible(scored[key])][:self.policy.selected_limit]
        evidence = [present_episode(found[key], scored[key]) for key in selected_ids]
        ambiguities = []
        # Similar cues can identify multiple episodes. Preserve each identity
        # and show uncertainty; never merge their dates, actions or amounts.
        if len(selected_ids) > 1:
            top = scored[selected_ids[0]]
            close = [key for key in selected_ids if abs(top['activation_score'] - scored[key]['activation_score']) <= .08
                     and scored[key]['relevance'] >= .25]
            if len(close) > 1 and not top['distinctive_cue']:
                ambiguities.append({'episode_ids': close, 'reason': 'multiple_similar_events'})
                for entry in evidence:
                    if str(entry['id']) in close:
                        entry['ambiguity'] = True
                        entry['ambiguity_candidates'] = close
        context = ContextComposer(budget_tokens, cost_estimator=cost_estimator).compose(system=system, tools=tools, schema=schema,
            current_input=query if current_input is None else current_input, history=history, facts=facts,
            self_state=self_state, episodic=evidence, required=required)
        packed = context['payload']['experience_evidence']
        actual = {str(item['id']): item for item in packed}
        candidates = []
        for key in ordered:
            item, comp = found[key], scored[key]
            reason = 'selected' if key in actual else ('budget' if key in selected_ids else 'below_activation_threshold')
            candidates.append({'id': key, 'title': item.get('title', ''), 'context_type': item.get('context_type', 'conversation'),
                **comp, 'selected': key in actual, 'level': actual[key]['level'] if key in actual else None,
                'selection_reason': reason, 'methods': item.get('methods', []),
                'association_hops': item.get('association_hops', 0)})
        return {'query': query, 'mode': mode, 'as_of': at.isoformat(), 'policy_version': self.policy.version,
            'candidate_limit': candidate_limit,
            'estimator_version': ESTIMATOR_VERSION, 'embedding_status': embedding_status,
            'vector_search': 'exact' if exact else 'ann_eligible',
            'candidates': candidates, 'selected': [row for row in candidates if row['selected']],
            'activated': [row for row in candidates if row['id'] in selected_ids],
            'evidence': packed, 'manifest': context['manifest'], 'manifest_hash': context['manifest_hash'],
            'budget': context['budget'], 'ambiguities': ambiguities, 'omitted': context['omitted'],
            'payload': context['payload'], 'read_only': True}
