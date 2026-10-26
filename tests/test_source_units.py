from copy import deepcopy
import pytest
from pydantic import BaseModel
from app.source_units import source_units, bind_source_references


@pytest.mark.parametrize('text', [
    '', '没有标点的完整长句'+('very_long_ascii_word_'*500),
    '  第一行。\r\n\n“我爱你！”\n 然后，戴上戒指。  ',
    '小吃花了37.5元。 English sentence. Next?! ”\t末尾',
    '🙂亲吻手背。\u3000颜色 e\u0301、符号<>$\\全部保留。',
])
def test_units_preserve_every_character_and_are_deterministic(text):
    units=source_units(text)
    assert units==source_units(text)
    assert ''.join(unit['text'] for unit in units)==text
    previous=0
    for index,unit in enumerate(units,1):
        assert unit['id']==f'u{index:04d}'
        assert unit['start']==previous and unit['end']>unit['start']
        assert text[unit['start']:unit['end']]==unit['text']
        previous=unit['end']
    assert previous==len(text)


def test_many_short_sentences_are_grouped_without_loss_or_sentence_splits():
    sentences=[f'第{index}句。' for index in range(600)]
    text=''.join(sentences)
    units=source_units(text)
    assert units==source_units(text)
    assert len(units)<=64
    assert ''.join(unit['text'] for unit in units)==text
    assert [unit['text'] for unit in units]==[
        ''.join(sentences[index:index+10]) for index in range(0,600,10)]
    previous=0
    for index,unit in enumerate(units,1):
        assert unit['id']==f'u{index:04d}'
        assert unit['start']==previous
        assert unit['text']==text[unit['start']:unit['end']]
        previous=unit['end']
    assert previous==len(text)
    resolved=bind_source_references({'episodes':[{'quote_refs':[units[-1]['id']]}]},text)
    assert resolved['episodes'][0]['quote']==''.join(sentences[-10:])


@pytest.mark.parametrize('count', [1,63,64])
def test_up_to_sixty_four_sentences_keep_individual_units(count):
    sentences=[f'第{index}句。' for index in range(count)]
    assert [unit['text'] for unit in source_units(''.join(sentences))]==sentences


def test_repeated_utterance_refs_bind_correct_occurrence_for_episodes_and_anchors():
    text='我爱你。我们散步。我爱你。'
    raw={'episodes':[{'quote_refs':['u0003'],
                     'details':[{'quote_refs':{'item':['u0003']}}],
                     'cues':[{'quote_refs':['u0003'],'text':'爱'}]}],
         'actions':[{'quote_refs':['u0003']}],
         'experience':{'anchors':[{'quote_refs':['u0003']}]},
         'self_updates':[{'quote_refs':['u0003']}]}
    before=deepcopy(raw)
    bound=bind_source_references(raw,text)
    event=bound['episodes'][0]
    assert event['quote']=='我爱你。' and event['occurrence']==1
    assert event['details'][0]['occurrence']==1
    assert event['cues'][0]['occurrence']==1 and event['cues'][0]['text']=='爱'
    assert bound['experience']['anchors'][0]=={'quote':'我爱你。','occurrence':1}
    for row in (bound['actions'][0],bound['self_updates'][0]):
        assert row=={'quote':'我爱你。'}
    assert raw==before


def test_contiguous_multiple_refs_include_intervening_symbols_and_whitespace():
    text='第一句。\n  第二句。'
    result=bind_source_references({'episodes':[{'quote_refs':['u0001','u0002','u0003']}]},text)
    assert result['episodes'][0]=={'quote':text,'occurrence':0}


@pytest.mark.parametrize('refs', [[],['u9999'],['old-experience-id'],['u0002','u0001'],
    ['u0001','u0001'],'u0001',[{'id':'u0001','start':0}],
    {'item':['u0001'],'source':'old'}, {'item':{'id':'u0001'}}, {'item':17}, {'item':None}])
def test_missing_external_disordered_or_duplicate_refs_are_rejected(refs):
    with pytest.raises(ValueError):
        bind_source_references({'actions':[{'quote_refs':refs}]},'一句。二句。三句。')


def test_ordered_sparse_refs_include_all_intervening_original_text():
    text='开头。\n中间原话不可省略。结尾。'
    raw={'experience':{'anchors':[{'quote_refs':['u0001','u0004'],'meaning':'完整范围'}]}}
    before=deepcopy(raw)
    result=bind_source_references(raw,text)
    assert result['experience']['anchors'][0]=={'quote':text,'occurrence':0,'meaning':'完整范围'}
    assert raw==before


@pytest.mark.parametrize('refs', [['u0003','u0001'],['u0001','u0003','u0003']])
def test_ordered_span_refs_still_reject_reversed_or_duplicate_ids(refs):
    with pytest.raises(ValueError,match='quote_refs_must_be_ordered_and_unique'):
        bind_source_references({'episodes':[{'quote_refs':refs}]},'一句。二句。三句。')


def test_sparse_refs_cannot_claim_a_quote_that_omits_the_middle():
    with pytest.raises(ValueError,match='quote_refs_quote_conflict'):
        bind_source_references({'episodes':[{'quote_refs':['u0001','u0003'],'quote':'一句。三句。'}]},
                               '一句。二句。三句。')


@pytest.mark.parametrize('changes', [{'quote':'改写的原话'}, {'quote':' '}, {'quote':17},
    {'occurrence':0}, {'occurrence':True}])
def test_conflicting_quote_or_occurrence_is_rejected(changes):
    raw={'episodes':[{'quote_refs':['u0002'],**changes}]}
    with pytest.raises(ValueError):bind_source_references(raw,'原话。原话。')


def test_matching_explicit_quote_and_occurrence_are_preserved():
    assert bind_source_references({'episodes':[{'quote_refs':['u0002'],'quote':'原话。','occurrence':1}]},
        '原话。原话。')=={'episodes':[{'quote':'原话。','occurrence':1}]}


def test_provider_list_wrappers_keep_episode_occurrence_paths():
    raw={'episodes':{'item':[{'quote_refs':{'item':['u0002']},
        'details':{'item':[{'quote_refs':['u0002']}]}}]}}
    result=bind_source_references(raw,'原话。原话。')
    assert result['episodes']['item'][0]['occurrence']==1
    assert result['episodes']['item'][0]['details']['item'][0]['occurrence']==1


def test_provider_single_item_wrappers_keep_episode_occurrence_paths():
    raw={'episodes':{'item':{'quote_refs':{'item':'u0002'},
        'details':{'item':{'quote_refs':{'item':'u0002'}}},
        'cues':{'item':{'quote_refs':{'item':'u0002'},'text':'原话'}}}},
        'actions':{'item':{'quote_refs':{'item':'u0002'}}},
        'experience':{'anchors':{'item':{'quote_refs':{'item':'u0002'}}}},
        'self_updates':{'item':{'quote_refs':{'item':'u0002'}}}}
    before=deepcopy(raw)
    result=bind_source_references(raw,'原话。原话。')
    event=result['episodes']['item']
    assert event['quote']=='原话。' and event['occurrence']==1
    assert event['details']['item']=={'quote':'原话。','occurrence':1}
    assert event['cues']['item']=={'quote':'原话。','occurrence':1,'text':'原话'}
    assert result['experience']['anchors']['item']=={'quote':'原话。','occurrence':1}
    for row in (result['actions']['item'],result['self_updates']['item']):
        assert row=={'quote':'原话。'}
    assert raw==before


@pytest.mark.parametrize('changes', [{'quote':'旧输入的原话。'}, {'occurrence':0}])
def test_provider_single_item_wrappers_do_not_relax_conflict_validation(changes):
    raw={'episodes':{'item':{'quote_refs':{'item':'u0002'},**changes}}}
    with pytest.raises(ValueError):
        bind_source_references(raw,'原话。原话。')


@pytest.mark.parametrize('refs', [{'item':'u0003'}, {'item':'previous:u0001'},
    {'item':'u0001','source':'previous'}, {'item':{'item':'u0001'}}])
def test_provider_single_item_wrappers_cannot_extend_current_source_scope(refs):
    with pytest.raises(ValueError):
        bind_source_references({'actions':{'item':{'quote_refs':refs}}},'当前原话。')


def test_plain_quotes_and_unrelated_fields_are_not_rewritten():
    raw={'actions':[{'quote':'原始短语','text':'用户事实'}],'source_id':'some-id',
         'extra':{'start':7,'end':9},'episodes':[{'quote':'原始短语','occurrence':2}]}
    assert bind_source_references(raw,'原始短语')==raw
    bound=bind_source_references({'actions':[{'quote_refs':['u0001'],'operation':'create','text':'解释'}]},'原话。')
    assert bound['actions'][0]=={'quote':'原话。','operation':'create','text':'解释'}


def test_basemodel_is_converted_without_mutation():
    class Container(BaseModel):
        episodes:list[dict]
    raw=Container(episodes=[{'quote_refs':['u0002']}])
    result=bind_source_references(raw,'原话。原话。')
    assert result=={'episodes':[{'quote':'原话。','occurrence':1}]}
    assert raw.episodes==[{'quote_refs':['u0002']}]
