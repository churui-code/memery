"""Conservative, source-local scope checks; this is not a semantic verifier.

Only user evidence quotes are proof. The checks cover a deliberately narrow
lexical subset: restriction/prerequisite clauses must occur in bound evidence;
reused predicates with limited frequency must keep a local qualifier; and
dates, quantities or distinctive copied fragments from other displayed sources
must bind those sources. Paraphrases can be refused, and unrelated paraphrases,
implicit conditions, subject swaps and amount/object swaps can still escape.
Passing this gate does not replace independent semantic review. In particular,
negating a restrictive operator and mentioning a predicate under a different
clause's qualifier may still be refused: this gate does not resolve negation,
anaphora or whether two clauses describe the same situation.

Call after schema validation and again in the commit transaction using all
frozen evidence quotes reconstructed from storage, never model-supplied quotes.
No source text is changed, inferred, added to a citation, or silently repaired.
"""
from collections.abc import Mapping
import re
import unicodedata

from app.errors import MemoryError


SCOPE_VERSION = 'reflection-scope-v1'
# These are language operators, not names/topics from acceptance fixtures.
_RESTRICTION = re.compile(
    r'只有|仅当|仅在|唯有|必须|一律|总是|永远|始终|任何情况下都|从未|从来没有|未曾|(?:此前|过去)没有任何|'
    r'所有|全部|每次|每当|必然|一定会|才(?:能|可|会|继续|开始|推进)|'
    r'(?:待|等|先|在)[^。！？!?；;\r\n]{0,80}(?:齐备|齐全|补齐|全部|所有)[^。！？!?；;\r\n]{0,40}(?:再|后)')
_LIMITED = re.compile(r'有的时候|某些时候|一些时候|有时|偶尔|偶然|不总是|未必|不一定|可能|也许')
_CLAUSE_END = re.compile(r'[，,、：:。！？!?；;\r\n]+')
_DATE = re.compile(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]|(\d{4})-(\d{1,2})-(\d{1,2})')
_QUANTITY = re.compile(r'\d+(?:\.\d+)?\s*(?:万元|美元|欧元|英镑|公里|千米|小时|分钟|个月|元|天|年|人|次|件)|[¥￥$€£]\s*\d+(?:\.\d+)?')
_FRAGMENT_SIZE = 4


def _mapping(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='python')
    if not isinstance(value, Mapping):
        raise TypeError('reflection_scope_requires_validated_mapping')
    return value


def _normalized(text):
    chars, offsets = [], []
    for index, char in enumerate(text):
        if unicodedata.category(char)[0] in ('L', 'N'):
            chars.append(char.lower())
            offsets.append(index)
    return ''.join(chars), offsets


def _clauses(text):
    start = 0
    for match in _CLAUSE_END.finditer(text):
        if start < match.start():
            yield start, match.start(), text[start:match.start()]
        start = match.end()
    if start < len(text):
        yield start, len(text), text[start:]


def _anchors(text):
    for match in _DATE.finditer(text):
        year, month, day = (match.group(1), match.group(2), match.group(3)) if match.group(2) else match.group(4, 5, 6)
        yield ('date-md', int(month), int(day)), match.start(), match.end()
        if year:
            yield ('date', int(year), int(month), int(day)), match.start(), match.end()
    for match in _QUANTITY.finditer(text):
        yield ('quantity', re.sub(r'\s', '', match.group())), match.start(), match.end()


def _issue(update, field, start, end, code, sources=()):
    return {'proposal_id': str(update['id']), 'field': field, 'start': start,
            'end': end, 'code': code, 'evidence_ids': [row['id'] for row in sources]}


def _source_location(row, start, end):
    return {'evidence_id': row['id'], 'quote_start': start, 'quote_end': end,
            'source_start': row['start'] + start, 'source_end': row['start'] + end}


def reflection_scope_issues(proposal, payload):
    """Return deterministic diagnostics without altering either input.

    A diagnostic's start/end indexes refer to the exact proposal field. Source
    locations use both quote-relative and original-source offsets. Missing
    frozen quotes fail closed; episode summaries/self state are not substitutes.
    Both arguments can be Pydantic models or ordinary validated dictionaries.
    """
    updates = [_mapping(row) for row in _mapping(proposal)['updates']]
    if not updates:
        return []
    sources, malformed = {}, []
    for event in _mapping(payload)['episodes']:
        for raw in _mapping(event)['evidence']:
            row = _mapping(raw)
            eid, quote = str(row['id']), row.get('quote')
            if not isinstance(quote, str) or not quote.strip() or row.get('role', 'user') != 'user':
                malformed.append(eid)
                continue
            if eid in sources and sources[eid]['quote'] != quote:
                malformed.append(eid)
                continue
            normalized, offsets = _normalized(quote)
            sources[eid] = {'id': eid, 'quote': quote, 'start': row.get('start', row.get('start_offset', 0)),
                            'normalized': normalized, 'offsets': offsets,
                            'anchors': {key for key, _, _ in _anchors(quote)}}
    if malformed:
        return [{'proposal_id': str(updates[0]['id']), 'field': 'evidence_ids',
                 'start': 0, 'end': 0, 'code': 'source_quote_unavailable',
                 'evidence_ids': sorted(set(malformed))}]
    issues = []
    for update in updates:
        selected = {str(eid) for eid in update['evidence_ids']}
        missing = selected - sources.keys()
        if missing:
            issues.append({'proposal_id': str(update['id']), 'field': 'evidence_ids',
                           'start': 0, 'end': 0, 'code': 'source_quote_unavailable',
                           'evidence_ids': sorted(missing)})
            continue
        bound = [sources[eid] for eid in sorted(selected)]
        unbound = [row for eid, row in sorted(sources.items()) if eid not in selected]
        bound_text = [row['normalized'] for row in bound]
        bound_anchors = set().union(*(row['anchors'] for row in bound))
        # A limited predicate is matched locally, not by a qualifier somewhere
        # else in the paragraph (e.g. "有时" on a different need).
        limited = []
        for row in bound:
            for start, end, clause in _clauses(row['quote']):
                for marker in _LIMITED.finditer(clause):
                    tail, _ = _normalized(clause[marker.end():])
                    if len(tail) >= 2:
                        limited.append((row, start + marker.start(), end,
                                        {tail[i:i+2] for i in range(len(tail)-1)}))
        for field in ('text', 'reason'):
            text = update[field]
            # Keep comma-separated conditions together when checking exact
            # support: a familiar operator in another clause is no evidence.
            for match in re.finditer(r'[^。！？!?；;\r\n]+', text):
                clause = match.group()
                normalized, _ = _normalized(clause)
                if _RESTRICTION.search(clause) and not any(normalized in quote for quote in bound_text):
                    issues.append(_issue(update, field, match.start(), match.end(),
                                         'unlocated_restriction', bound))
            for start, end, clause in _clauses(text):
                normalized, _ = _normalized(clause)
                if _LIMITED.search(clause):
                    continue
                for row, source_start, source_end, predicates in limited:
                    if any(predicate in normalized for predicate in predicates):
                        item = _issue(update, field, start, end, 'lost_local_qualifier', [row])
                        item['source_locations'] = [_source_location(row, source_start, source_end)]
                        issues.append(item)
                        break
            for anchor, start, end in _anchors(text):
                if anchor in bound_anchors:
                    continue
                foreign = [row for row in unbound if anchor in row['anchors']]
                if foreign:
                    item = _issue(update, field, start, end, 'unbound_source_anchor', foreign)
                    item['source_locations'] = [_source_location(row, left, right)
                        for row in foreign for key, left, right in _anchors(row['quote']) if key == anchor]
                    issues.append(item)
            normalized, offsets = _normalized(text)
            # An exact foreign fragment is only a provenance alarm, never proof
            # of meaning. Four characters can coincide; conservative refusals
            # are intentional, and arbitrary synonymous borrowing is not found.
            for row in unbound:
                for index in range(len(normalized) - _FRAGMENT_SIZE + 1):
                    fragment = normalized[index:index+_FRAGMENT_SIZE]
                    source_index = row['normalized'].find(fragment)
                    if source_index < 0 or any(fragment in quote for quote in bound_text):
                        continue
                    item = _issue(update, field, offsets[index], offsets[index+_FRAGMENT_SIZE-1]+1,
                                  'unbound_source_fragment', [row])
                    item['source_locations'] = [_source_location(row, row['offsets'][source_index],
                        row['offsets'][source_index+_FRAGMENT_SIZE-1]+1)]
                    issues.append(item)
                    break
    return issues


def validate_reflection_scope(proposal, payload):
    """Fail closed on the checked subset; return the unchanged proposal."""
    issues = reflection_scope_issues(proposal, payload)
    if issues:
        error = MemoryError('reflection_scope_unsupported',
            '反思提案的限定范围或证据绑定缺少可定位的原文支持，本轮没有写入。', 502)
        error.scope_issues = issues
        raise error
    return proposal
