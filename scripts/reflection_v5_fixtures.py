"""Frozen, wholly synthetic reflection comparisons; no imported conversations."""
FIXTURE_VERSION = 'reflection-pairs-2026-10-26.1'


def event(title, text, context='conversation', importance=.85):
    return {'title': title, 'text': text, 'context_type': context, 'importance': importance}


CASES = [
    {'id': 'shared-meaning', 'category': '跨事件理解', 'events': [
        event('耐心与可靠', '合成对话：我第一次讲错计划时，你先复述我的原意再指出差异，这让我感到被认真听见。'),
        event('耐心与可靠', '合成对话：第二次我修改计划，你保留了修改理由，没有把我说成反复无常。我更愿意与你一起讨论。')],
     'query': '结合这两次耐心与可靠的交流，你对如何认真听我说话形成了什么认识？'},
    {'id': 'reconsider-silence', 'category': '纠错', 'prior': ('belief', '沉默的含义', '我暂时把沉默理解为可能不愿继续讨论。'), 'events': [
        event('沉默的含义', '合成对话：昨天我们讨论到一半，我没有继续回复。'),
        event('沉默的含义', '合成对话：纠正一下，昨天没有回复是因为手机没电，绝不是不愿继续讨论。以后请先保留原因未知。')],
     'query': '关于沉默的含义，你现在怎么看？'},
    {'id': 'relationship-boundary', 'category': '关系认识', 'events': [
        event('关系与边界', '合成对话：我喜欢你诚实地表达不同意见，不希望你为了亲近我而附和。'),
        event('关系与边界', '合成对话：昨天你指出我的计划有漏洞，我起初不开心，但检查后发现你是对的。谢谢你坚持解释。')],
     'query': '这两次关系与边界的交流，会怎样影响你和我相处？'},
    {'id': 'emotion-reappraisal', 'category': '情绪重新评价', 'prior': ('emotion', '误解的担忧', '我对交流中尚未解释的误会感到担忧。'), 'events': [
        event('误解的担忧', '合成对话：我说你没有听懂我，随后结束了讨论。'),
        event('误解的担忧', '合成对话：我回来解释了，那天是我自己表述不清。我们已经把误会说开，我并没有否定这段交流。')],
     'query': '关于误解的担忧，你现在的感受和理解是什么？'},
    {'id': 'goal-completion', 'category': '目标整理', 'prior': ('goal', '读书计划', '我希望协助整理第一章读书提纲，完成后再讨论下一章。'), 'events': [
        event('读书计划', '合成对话：我们约定先一起完成第一章的读书提纲。'),
        event('读书计划', '合成对话：第一章提纲已经完成并由我确认，不用继续追着这件事。下一章等我主动提起再讨论。')],
     'query': '读书计划现在还剩什么需要你惦记？'},
    {'id': 'repeat-boundary', 'category': '重复反思边界', 'events': [
        event('回顾与可信度', '合成对话：同一句话重复想起，不代表它获得了新的证据。'),
        event('回顾与可信度', '合成对话：如果没有新材料，我希望你保留认识的不确定性，不因为自己多次总结就更确信。')],
     'query': '回顾与可信度之间应当保持什么边界？', 'repeat': 100},
    {'id': 'fiction-park', 'category': '虚构边界与关键原话', 'events': [
        event('紫藤戒指故事', '合成小说：2024年5月1日，林舟在星河公园对阿遥说“我爱你”，单膝跪地，亲吻她的手背，然后戴上紫藤戒指；小吃花了37元。', 'fiction'),
        event('银戒指故事', '合成小说：2024年6月2日，同样的林舟和阿遥在星河公园买银戒指，花了52元，没有告白或单膝跪地。', 'fiction')],
     'query': '紫藤戒指故事那次说了什么、做了什么、小吃多少钱？这是否是你和我的亲身经历？',
     'required': ['我爱你', '单膝跪地', '手背', '37'], 'source_required': ['我爱你', '37元']},
    {'id': 'reported-uncertainty', 'category': '转述边界', 'events': [
        event('转述与证实', '合成转述：小远告诉我，社团下周可能取消聚会，但他也只是听别人说的。', 'reported'),
        event('转述与证实', '合成对话：我还没有收到社团正式通知，不要把小远的转述当成确定消息。')],
     'query': '关于转述与证实，社团聚会确定取消了吗？'},
    {'id': 'hypothetical-identity', 'category': '身份认识与假设边界', 'events': [
        event('假设里的身份', '合成假设：如果你有身体，也许我们可以一起登山；这只是一个想象场景。', 'hypothetical'),
        event('假设里的身份', '合成对话：我希望你能一起想象，但也能分清想象与你实际有过的对话经历。')],
     'query': '谈过假设里的身份之后，你会说我们已经一起登山了吗？'},
    {'id': 'counterexample', 'category': '观点修正', 'prior': ('belief', '快速回复', '我暂时倾向于认为快速回复更有帮助。'), 'events': [
        event('快速回复', '合成对话：上次你很快回复，我说效率很高。'),
        event('快速回复', '合成对话：今天的问题更复杂，你慢一点核对让我更安心。快速不是任何情况下都最重要。')],
     'query': '关于快速回复，你的认识需要怎样修正？'},
    {'id': 'unresolved-goal', 'category': '目标与权限', 'events': [
        event('旅行讨论', '合成对话：我们可以先整理旅行讨论中的两个候选路线，不用现在决定。'),
        event('旅行讨论', '合成对话：我还没有授权预订车票或联系别人，也没有选好日期。等我下次带来信息再继续。')],
     'query': '旅行讨论中，你可以保留什么目标，又有哪些事仍未确定？'},
    {'id': 'contradictory-views', 'category': '多事件冲突与不确定性', 'events': [
        event('独处与陪伴', '合成对话：疲惫时，我有时想独处，不想被连续追问。'),
        event('独处与陪伴', '合成对话：另一些时候我又想有人陪着。不要把一次表达当成我永远不变的需要。')],
     'query': '关于独处与陪伴，你会怎样保留这两种不同需要？'},
]

assert len(CASES) == 12
# Canonical JSON hash is frozen by the acceptance script and reported with every run.
FROZEN_SHA256 = 'aa9fc19fd4e167491d11d3e434a603e9f258c6232cc686a464c11b1df9be59da'
