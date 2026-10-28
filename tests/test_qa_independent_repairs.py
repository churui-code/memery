"""Independent correction and adapter regressions; no real model calls."""
import json
from uuid import uuid4
import pytest

from app.context import ContextComposer
from app.corrections import sanitize_episode
from app.recall import present_episode


def episode_with_detail(text, detail_quote, *, meaning='', kind='fact'):
    source, event = str(uuid4()), str(uuid4())
    start = text.index(detail_quote)
    base = {'experience_id': source, 'role': 'user', 'source_revision': 1}
    return {
        'id': event, 'revision': 1, 'title': '合成事件', 'gist': text,
        'context_type': 'reported', 'source_experience_ids': [source],
        'source_revisions': {source: 1},
        'evidence': [{**base, 'quote': text, 'start': 0, 'end': len(text)}],
        'details': [{**base, 'id': str(uuid4()), 'quote': detail_quote,
                     'start': start, 'end': start + len(detail_quote),
                     'kind': kind, 'meaning': meaning, 'importance': 0.8,
                     'importance_reason': meaning, 'emotional_reason': meaning}],
        'cues': [],
    }


def corrected(item, old_quote):
    text = item['evidence'][0]['quote']
    start = text.index(old_quote)
    return sanitize_episode(item, [{'experience_id': item['source_experience_ids'][0],
                                   'start': start, 'end': start + len(old_quote)}])


def test_independent_correction_removes_old_claim_from_retained_detail_metadata():
    text = '修琴花了29元。宁秋说：“声音亮起来了。”'
    speech = '宁秋说：“声音亮起来了。”'
    item = episode_with_detail(text, speech, kind='speech',
                               meaning='宁秋肯定这次29元修琴的效果')
    safe = corrected(item, '修琴花了29元。')
    presented = present_episode(safe, {'activation_score': 0.9})
    payload = ContextComposer().compose(current_input='宁秋当时的原话是什么？',
                                        episodic=[presented])['payload']
    encoded = json.dumps(payload, ensure_ascii=False)
    assert speech in encoded
    assert '29元' not in encoded


def test_independent_correction_excludes_inseparable_parallel_claim_without_rewriting_source():
    text = '我和宁秋分别花29元和83元买了琴弦与琴谱。'
    item = episode_with_detail(text, text, kind='amount')
    original = json.loads(json.dumps(item))
    safe = corrected(item, '29元')
    assert '29元' not in json.dumps(safe, ensure_ascii=False)
    # The subject/amount pairing cannot survive as an independent exact quote.
    assert safe['details'] == [] and safe['evidence'] == []
    assert '83元' not in json.dumps(safe, ensure_ascii=False)
    assert item == original


def test_independent_correction_preserves_following_english_sentence():
    text = 'I spent 29 yuan. Ningqiu spent 83 yuan.'
    safe = corrected(episode_with_detail(text, text, kind='amount'), 'I spent 29 yuan.')
    assert '29 yuan' not in json.dumps(safe, ensure_ascii=False)
    assert 'Ningqiu spent 83 yuan.' in json.dumps(safe, ensure_ascii=False)


def test_independent_english_sentence_boundary_does_not_split_decimal_amounts():
    text = 'I spent 29.25 yuan. Ningqiu spent 83.50 yuan.'
    safe = corrected(episode_with_detail(text, text, kind='amount'), 'I spent 29.25 yuan.')
    assert '29.25' not in json.dumps(safe, ensure_ascii=False)
    assert any(row['quote'].strip() == 'Ningqiu spent 83.50 yuan.' for row in safe['evidence'])
    for row in safe['details'] + safe['evidence']:
        assert text[row['start']:row['end']] == row['quote']


def test_independent_comma_separated_amount_retains_the_other_clause():
    text = '宁秋花83元买了琴谱，我花29元买了琴弦。'
    safe = corrected(episode_with_detail(text, text, kind='amount'), '我花29元买了琴弦。')
    assert '29元' not in json.dumps(safe, ensure_ascii=False)
    assert '宁秋花83元买了琴谱，' in json.dumps(safe, ensure_ascii=False)


@pytest.mark.parametrize('fault', ['missing_old_quote', 'duplicate_quote', 'missing_top_quote_refs'])
def test_independent_live_adapter_enforces_cloud_quote_protocol_before_commit(repo, model, monkeypatch, fault):
    from app.errors import MemoryError
    from app.model import MiniMax
    from app.service import MemoryService
    from conftest import FakeEmbeddings

    original = '红榆巷纸品店那次，我花31元买了笔记本，顾禾花72元买了信封。'
    model.decision = {'episodes': [{'title': '红榆巷纸品店', 'gist': original, 'quote': original,
                                   'details': [{'quote': original, 'kind': 'amount'}]}]}
    first = MemoryService(repo, FakeEmbeddings(), model).chat(original)
    current = '红榆巷纸品店那次笔记本金额说错了，实际是13元。'
    adapter = MiniMax(repo.settings)
    observed = []

    def provider(system, messages, tool=None):
        if tool is None:
            return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '已按当前原话回答。'}]}
        observed.append(tool)
        if tool['name'] == 'reassess_memories':
            proposal = {'self_updates': [], 'episodes': [], 'rehearsals': []}
        elif tool['name'] == 'propose_memories':
            proposal = {'actions': [], 'retain_turn': True, 'self_updates': []}
            if fault == 'missing_old_quote':
                payload = json.loads(messages[0]['content'])
                event = next(row for row in payload['experience_evidence']
                             if str(row['id']) == str(first['episode_ids'][0]))
                detail = next(row for row in event['details'] if '31元' in row['quote'] and '72元' in row['quote'])
                proposal['corrections'] = [{
                    'target_id': str(event['id']), 'expected_revision': event['revision'],
                    'experience_id': str(detail['experience_id']), 'start': detail['start'],
                    'end': detail['end'], 'quote_refs': ['u0001'],
                }]
                required = tool['input_schema']['properties']['corrections']['items']['required']
                assert 'old_quote' in required
            else:
                episode = {'title': '核对纸品店收据', 'gist': current, 'quote': current}
                if fault == 'duplicate_quote':
                    episode['quote_refs'] = ['u0001']
                proposal['episodes'] = [episode]
                schema = tool['input_schema']['properties']['episodes']['items']
                assert 'quote_refs' in schema['required'] and 'quote' not in schema['properties']
        else:
            raise AssertionError('Unexpected tool: ' + tool['name'])
        return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': tool['name'],
                                                      'input': proposal}], 'usage': {}}

    monkeypatch.setattr(adapter, '_request', provider)
    try:
        result = MemoryService(repo, FakeEmbeddings(), adapter).chat(current, first['conversation_id'])
    except MemoryError as exc:
        assert exc.code in ('memory_decision_invalid', 'memory_reassessment_invalid')
    else:
        with repo.pool.connection() as connection:
            stored = connection.execute('''SELECT start_offset,end_offset FROM episode_corrections
                WHERE experience_id=%s''', (first['experience_id'],)).fetchall()
        safe_event = repo.episode(first['episode_ids'][0])
        diagnostic = {
            'fault': fault, 'history_saved': result['history_saved'],
            'memory_status': result['memory_status'], 'corrections': result['episode_corrections'],
            'stored_ranges': stored, 'original_length': len(original),
            'unaffected_72_retained': '72元' in json.dumps(safe_event, ensure_ascii=False, default=str),
            'raw_archive_unchanged': repo.experience(first['experience_id'])['user_text'] == original,
        }
        print(json.dumps(diagnostic, ensure_ascii=False, default=str))
        assert not result['history_saved'] and not result['episode_corrections'], diagnostic
    assert observed
    assert all(tool['name'] == 'propose_memories' for tool in observed)
    with repo.pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM experiences').fetchone()['n'] == 1
        assert connection.execute('SELECT count(*) AS n FROM episode_corrections').fetchone()['n'] == 0


def test_independent_corrected_cue_does_not_inflate_live_cue_frequency(repo):
    from app.corrections import EpisodeCorrection
    from app.recall import activation_components

    saved = []
    for text in ('第一枚松纹书签花了31元。', '第二枚松纹书签花了72元。'):
        saved.append(repo.save_answer(uuid4(), text, '合成回答。', [], repo.epoch(),
            episode_proposals=[{'title': '松纹书签', 'gist': text, 'quote': text,
                'details': [{'kind': 'amount', 'quote': text}],
                'cues': [{'kind': 'object', 'text': '松纹书签', 'quote': text}]}]))
    first, second = saved
    old = '第一枚松纹书签花了31元。'
    correction = EpisodeCorrection(target_id=first['episode_ids'][0], expected_revision=1,
        experience_id=first['experience_id'], start=0, end=len(old), old_quote=old,
        quote='第一枚其实是枫叶书签，价格也记错了。')
    repo.save_answer(uuid4(), correction.quote, '合成更正。', [], repo.epoch(), corrections=[correction])
    candidate = next(row for row in repo.episode_candidates('松纹书签') if row['id'] == second['episode_ids'][0])
    cues = [row for row in candidate['matched_cues'] if row['text'] == '松纹书签']
    components = activation_components(candidate, '松纹书签', repo.clock.now())
    print(json.dumps({'effective_cue_frequency': [row['frequency'] for row in cues],
                      'distinctive_cue': components['distinctive_cue']}, ensure_ascii=False))
    assert cues and all(row['frequency'] == 1 for row in cues)
    assert components['distinctive_cue']
