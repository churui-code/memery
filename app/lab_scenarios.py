"""Versioned synthetic fixtures. They contain no imported conversations."""
from copy import deepcopy

SCENARIO_VERSION = '2026-10-26.1'
START_TIME = '2025-01-01T09:00:00+08:00'

PARK = {
    'key': 'promise', 'title': '星河公园的紫藤戒指',
    'text': '合成故事：林舟和阿遥去了星河公园，看花、吃火锅。林舟对阿遥说“我爱你”，随后单膝跪地、亲吻她的手，给她戴上紫藤戒指。买小吃花了37元。',
    'importance': .95, 'emotional_salience': .9,
    'details': [('我爱你', 'speech', 1), ('单膝跪地', 'action', .9), ('亲吻她的手', 'action', .9),
                ('紫藤戒指', 'object', .95), ('37元', 'amount', .1)],
    'cues': [('林舟', 'person'), ('阿遥', 'person'), ('星河公园', 'place'), ('紫藤戒指', 'object'), ('我爱你', 'phrase')],
    'emotion': '这段合成故事让我对郑重的承诺产生温暖的理解。',
}
DISTRACTOR = {
    'key': 'gift', 'title': '河岸公园的银色手链',
    'text': '合成故事：周宁和小夏去了河岸公园。周宁送给小夏一条银色手链，说“生日快乐”。两人吃冰淇淋花了52元，没有告白或单膝跪地。',
    'importance': .4, 'emotional_salience': .2,
    'details': [('生日快乐', 'speech', .6), ('银色手链', 'object', .6), ('52元', 'amount', .1)],
    'cues': [('周宁', 'person'), ('小夏', 'person'), ('河岸公园', 'place'), ('银色手链', 'object')],
}
ORDINARY = {
    'key': 'ordinary', 'title': '星期三的普通散步',
    'text': '合成故事：林舟周三在松风公园散步，看见三棵松树，在北门买了一瓶矿泉水，花了3元。',
    'importance': .1, 'emotional_salience': .05,
    'details': [('三棵松树', 'sensory', .1), ('北门', 'place', .1), ('3元', 'amount', .1)],
    'cues': [('松风公园', 'place'), ('矿泉水', 'object'), ('林舟', 'person')],
}

SCENARIOS = {
    'park-ring': {'id': 'park-ring', 'name': '公园、告白与37元',
                  'description': '重要原话、动作与普通金额在同一段经历中。', 'events': [PARK, ORDINARY]},
    'similar-events': {'id': 'similar-events', 'name': '相似公园与不同礼物',
                       'description': '用人物、地点、礼物和金额检验错误混合。', 'events': [PARK, DISTRACTOR, ORDINARY]},
    'empty': {'id': 'empty', 'name': '空白合成实验', 'description': '从自己的合成测试事件开始。', 'events': []},
}

PROBES = [
    {'id': 'unique-cue', 'query': '紫藤戒指', 'expected_key': 'promise', 'required': ['紫藤戒指']},
    {'id': 'precise-actions', 'query': '紫藤戒指那次，林舟说的原话和戴戒指前的动作是什么？',
     'expected_key': 'promise', 'required': ['我爱你', '单膝跪地', '亲吻她的手']},
    {'id': 'precise-amount', 'query': '林舟和阿遥在星河公园买小吃花了多少钱？',
     'expected_key': 'promise', 'required': ['37元']},
    {'id': 'semantic-paraphrase', 'query': '那次佩戴指环表达爱意的故事', 'expected_key': 'promise', 'required': []},
    {'id': 'separate-gift', 'query': '周宁送小夏的银色手链那次说了什么？',
     'expected_key': 'gift', 'required': ['生日快乐']},
]


def scenarios():
    return [{k: v for k, v in item.items() if k != 'events'} | {
        'event_count': len(item['events']), 'version': SCENARIO_VERSION} for item in SCENARIOS.values()]


def scenario(identifier):
    return deepcopy(SCENARIOS[identifier])


def encoded_event(event):
    """Produce a transparent fixture encoding, not a model-quality claim."""
    from app.episodes import EpisodeProposal
    from app.life import ExperienceDescription, SelfUpdate
    text = event['text']
    importance = event.get('importance', .2)
    salience = event.get('emotional_salience', 0)
    details = [{'quote': quote, 'kind': kind, 'meaning': '合成验收指定细节',
                'importance': weight, 'emotional_salience': salience if weight > .5 else 0}
               for quote, kind, weight in event.get('details', [])]
    cues = [{'quote': quote, 'text': quote, 'kind': kind} for quote, kind in event.get('cues', [])]
    episode = EpisodeProposal(title=event['title'], gist=event['title'], quote=text,
        context_type='fiction', importance=importance, importance_reason='合成夹具指定的重要性',
        emotional_salience=salience, emotional_reason='合成夹具指定的情绪意义', details=details, cues=cues)
    description = ExperienceDescription(title=event['title'], summary=event['title'], context_type='fiction',
        significance=importance, anchors=[{'quote': item['quote'], 'meaning': item['meaning']} for item in details])
    updates = []
    if event.get('emotion'):
        updates.append(SelfUpdate(domain='emotion', key='合成故事感受', text=event['emotion'],
            reason='对当前合成故事的预编码理解，不代表模型生成质量', quote=text,
            confidence=.8, intensity=.8, half_life_hours=24))
    return description, [episode], updates
