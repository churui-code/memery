"""Provider field compatibility preserves the independent evidence contract."""
from copy import deepcopy

import pytest

from app.automatic import validate_decision
from app.episodes import EpisodeDetail
from app.errors import MemoryError


def test_detail_text_alias_serializes_as_meaning_without_changing_quote_or_input():
    raw = {'kind': 'speech', 'quote': '我爱你。', 'text': '这是对承诺的解释，不是原话。',
           'importance': .9, 'emotional_salience': .8}
    original = deepcopy(raw)
    detail = EpisodeDetail.model_validate(raw)
    assert detail.meaning == raw['text']
    assert detail.quote == '我爱你。'
    assert detail.importance == .9 and detail.emotional_salience == .8
    assert 'text' not in detail.model_dump()
    assert detail.model_dump()['meaning'] == raw['text']
    assert raw == original


def test_detail_text_and_meaning_must_agree_exactly():
    raw = {'quote': '我爱你。', 'meaning': '承诺的表达', 'text': '承诺的表达'}
    assert EpisodeDetail.model_validate(raw).meaning == '承诺的表达'
    raw['text'] = '不同的解释'
    with pytest.raises(ValueError, match='detail_meaning_alias_conflict'):
        EpisodeDetail.model_validate(raw)
    raw['text'] = '承诺的表达 '
    with pytest.raises(ValueError, match='detail_meaning_alias_conflict'):
        EpisodeDetail.model_validate(raw)


@pytest.mark.parametrize('text', [None, 123, ['解释'], {'meaning': '解释'}, '甲' * 1001])
def test_detail_alias_keeps_meaning_type_and_length_validation(text):
    with pytest.raises(ValueError):
        EpisodeDetail.model_validate({'quote': '我爱你。', 'text': text})


@pytest.mark.parametrize('change', [
    {'importance': 1.1}, {'emotional_salience': -.1},
    {'text': ' ', 'importance': .8}, {'text': '', 'emotional_salience': .8},
])
def test_detail_alias_keeps_salience_range_and_basis_validation(change):
    with pytest.raises(ValueError):
        EpisodeDetail.model_validate({'quote': '我爱你。', 'text': '合成解释', **change})


def test_detail_alias_resolves_refs_but_cannot_replace_or_escape_evidence():
    text = '第一次在河岸送生日礼物。第二次在公园求婚。'
    raw = {'episodes': [{'title': '生日礼物', 'gist': '合成生日赠礼', 'quote_refs': ['u0001'],
        'details': [{'kind': 'action', 'text': '对方收到礼物的解释性描述', 'quote_refs': ['u0001']}]}]}
    decision = validate_decision(raw, text, [])
    detail = decision.episodes[0].details[0]
    assert detail.meaning == '对方收到礼物的解释性描述'
    assert detail.quote == '第一次在河岸送生日礼物。'
    assert detail.occurrence == 0
    for replacement in (
        {'text': '第一次在河岸送生日礼物。'},
        {'text': '合成解释', 'quote': '没有出现的原话'},
        {'text': '合成解释', 'quote_refs': ['u0002']},
    ):
        invalid = deepcopy(raw)
        invalid['episodes'][0]['details'] = [replacement]
        with pytest.raises(MemoryError) as exc:
            validate_decision(invalid, text, [])
        assert exc.value.code == 'memory_decision_invalid'
