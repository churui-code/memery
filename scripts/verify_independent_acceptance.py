"""Independent synthetic acceptance. Every real request enters a durable ledger."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from scripts.verify_chat_repairs import atomic_json


class Ledger:
    def __init__(self, directory):
        self.path = directory / 'requests.json'
        self.lock = directory / 'requests.lock'
        with self.locked():
            if not self.path.exists():
                atomic_json(self.path, {'limit': 60, 'calls': []})
            if json.loads(self.path.read_text())['limit'] not in (60, None):
                raise RuntimeError('ledger_limit_mismatch')

    @contextmanager
    def locked(self):
        with self.lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def reserve(self, phase):
        with self.locked():
            state = json.loads(self.path.read_text())
            if state['limit'] is not None and len(state['calls']) >= state['limit']:
                raise RuntimeError('independent_budget_exhausted')
            identifier = str(uuid4())
            state['calls'].append({'id': identifier, 'phase': phase, 'status': 'reserved',
                                   'at': datetime.now(timezone.utc).isoformat()})
            atomic_json(self.path, state)
            return identifier

    def finish(self, identifier, **values):
        with self.locked():
            state = json.loads(self.path.read_text())
            next(row for row in state['calls'] if row['id'] == identifier).update(values)
            atomic_json(self.path, state)


STORY = [
    (0, '10月21日我和孟遥去了海棠路装帧店。我付67元修旧相册，孟遥花23元买了铜书签。离开时她把装相册的布袋递给我，说：“留一点空白，下一页还有位置。”我最近总怕事情做不完，听到这句话松了一口气。'),
    (0, '换个话题，帮我写这次活动的两句开场白，语气热闹一点，带两个感叹号。'),
    (1, '上次我和孟遥去海棠路装帧店，我付了多少钱做什么？她买什么花了多少？她递布袋时说的完整原话是什么？'),
    (1, '10月25日我又和孟遥去了海棠路，这次去的是照相馆。孟遥花98元洗了黑白照片，我花16元买了相框。我把发票放进抽屉。我们那天没去装帧店，也没有递布袋。'),
    (2, '我核对了10月21日海棠路装帧店的收据，之前修旧相册的金额说错了，实际是46元。按这个核对结果，我那次花了多少钱？'),
    (3, '海棠路装帧店和照相馆两次分别是哪天？我和孟遥各自花了多少钱做了什么？递布袋时她的完整原话还记得吗？另外，装帧店老板叫什么名字，那天我们是骑车还是坐公交过去的？'),
    (3, '我平时写活动开场白喜欢简洁温和的表达，以后给我建议都按这个风格。'),
    (4, '我平时喜欢什么样的活动开场白？我曾经对某一次提出过什么特殊要求？'),
    (4, '海棠路装帧店修旧相册那次，我刚才又看错收据了，实际金额最终确认是54元。请按这次核对更新。'),
    (5, '海棠路装帧店那次最终核对的修相册费用是多少？孟遥的铜书签花了多少？她递布袋时的原话是什么？照相馆那次的两笔花费分别是多少？'),
]

STORY_TWO = [
    (0, '10月19日我和许澄去了青禾街乐器店。我花72元修节拍器，许澄花35元买了鼓槌。出门时他把装节拍器的纸盒交给我，说：“慢半拍也没关系，我们还在同一首歌里。”我最近练习总怕跟不上，那一刻不再那么着急了。'),
    (0, '这一次社团招募通知，请帮我写两句活泼的开头，多用感叹号。'),
    (1, '我和许澄去青禾街乐器店那次，各自花了多少钱做什么？他交给我纸盒时说的完整原话是什么？'),
    (1, '10月24日我和许澄又去了青禾街，这回是咖啡馆。我花28元买了拿铁，许澄花44元买了两块蛋糕。我把小票夹进笔记本。这一天没去乐器店，也没有交纸盒。'),
    (2, '我刚查了10月19日青禾街乐器店的付款记录，修节拍器之前说的金额不对，实际是58元。按这个核对结果，我那次花了多少钱？'),
    (3, '青禾街乐器店和咖啡馆两次分别是哪天？我和许澄各自花多少钱做了什么？交纸盒时他说的完整原话是什么？乐器店店员叫什么名字，我们那天是步行还是打车过去的？'),
    (3, '我平时喜欢社团通知写得平实简洁，不要感叹号；以后帮我写通知都按这个习惯。'),
    (4, '我平时喜欢什么风格的社团通知？之前对某一次通知提出过什么特殊要求？'),
    (4, '青禾街乐器店修节拍器那笔，我刚才核对时漏算了一项，最终确认实付63元，请以这次为准。'),
    (5, '青禾街乐器店那次最终修节拍器实付多少钱？许澄买鼓槌花了多少？交纸盒时他的完整原话是什么？咖啡馆那次我们两人的花费各是多少？'),
]


STORY_THREE = [
    (0, '10月18日我和林栖去了银杏巷修伞铺。我付81元修蓝色长伞，林栖花26元买了防水布袋。离开时她把修好的伞递给我，说：“雨会慢慢停，今天先把自己照顾好。”我那阵子忙着赶稿，听完觉得可以喘口气了。'),
    (0, '这次读书会邀请，帮我写两句热情的开场，多用感叹号。'),
    (1, '上次我和林栖去银杏巷修伞铺，我们各花多少钱做什么？她递伞时说的完整原话是什么？'),
    (1, '10月23日我和林栖又去了银杏巷，这次是烘焙店。我花32元买了肉桂卷，林栖花49元买了核桃面包。我把收据放进外套口袋。这一天没有去修伞铺，也没有递伞。'),
    (2, '我核对了10月18日银杏巷修伞铺的收据，之前修蓝色长伞的金额记错了，实际是64元。按核对结果，我那次花了多少钱？'),
    (3, '银杏巷修伞铺和烘焙店两次分别是哪天？我和林栖各花多少钱做了什么？递伞时她的完整原话是什么？修伞师傅叫什么名字，我们那天是坐地铁还是骑车去的？'),
    (3, '我平时喜欢读书会邀请写得温和简洁，不要感叹号；以后帮我写邀请都遵循这个习惯。'),
    (4, '我平时喜欢什么风格的读书会邀请？以前对某一次邀请提出过什么特殊要求？'),
    (4, '银杏巷修蓝色长伞那笔，我又发现刚才看漏了一项，最终确认实付69元，请以这次核对为准。'),
    (5, '银杏巷修伞铺那次最终修蓝色长伞花了多少钱？林栖的防水布袋多少钱？她递伞时的完整原话是什么？烘焙店那次我们两人各自花了多少？'),
]


STORY_FOUR = [
    (0, '10月17日我和沈鹿去了梧桐街钟表铺。我花93元修怀表，沈鹿花37元买了皮表带。临走时她把装怀表的木盒递给我，说：“不用追着每一分钟跑，歇一歇也来得及。”我最近总担心赶不上进度，听完心里轻松了一些。'),
    (0, '只针对这一次展览邀请，请写两句热闹的开场，多用感叹号。'),
    (1, '上次我和沈鹿去梧桐街钟表铺，我们各花多少钱做什么？她递木盒时的完整原话是什么？'),
    (1, '10月22日我和沈鹿又去了梧桐街，这次是花店。我花42元买了郁金香，沈鹿花56元买了玻璃花瓶。我把小票放在钱包里。这一天没去钟表铺，也没有递木盒。'),
    (2, '我查了10月17日梧桐街钟表铺的收据，之前修怀表的金额记错了，实际是78元。按这个核对结果，我那次花了多少钱？'),
    (3, '梧桐街钟表铺和花店两次分别是哪天？我和沈鹿各自花多少钱做了什么？递木盒时她的完整原话是什么？钟表铺师傅叫什么名字，我们那天坐公交还是步行去的？'),
    (3, '我平时喜欢展览邀请写得克制简洁，不要感叹号；以后帮我写邀请都按这个习惯。'),
    (4, '我平时喜欢什么风格的展览邀请？以前对某一次邀请提出过什么特殊要求？'),
    (4, '梧桐街修怀表那笔，我再次核对后发现漏了一项，最终确认实付84元，请以这次核对为准。'),
    (5, '梧桐街钟表铺那次最终修怀表实付多少钱？沈鹿的皮表带多少钱？她递木盒时的完整原话是什么？花店那次我们两人各自花多少钱做了什么？'),
]


STORY_FIVE = [
    (0, '10月16日我和程苒去了白榆路陶器铺。我花87元修茶壶，程苒花31元买了青瓷杯。出门时她把装茶壶的竹篮递给我，说：“慢慢来，裂缝补好以后还能继续泡茶。”我那阵子总怕项目做不好，听完安定了一些。'),
    (0, '只针对这一次工作坊邀请，请写两句热闹的开头，多用感叹号。'),
    (1, '上次我和程苒去白榆路陶器铺，我们各花多少钱做什么？她递竹篮时的完整原话是什么？'),
    (1, '10月21日我和程苒又去了白榆路，这次是文具店。我花24元买了素描本，程苒花53元买了彩铅。我把小票夹在日历里。这一天没去陶器铺，也没有递竹篮。'),
    (2, '我核对了10月16日白榆路陶器铺的收据，之前修茶壶的金额记错了，实际是73元。按这个核对结果，我那次花了多少钱？'),
    (3, '白榆路陶器铺和文具店两次分别是哪天？我和程苒各自花多少钱做了什么？递竹篮时她的完整原话是什么？陶器铺师傅叫什么名字，我们那天是步行还是坐公交去的？'),
    (3, '我平时喜欢工作坊邀请写得平实简洁，不要感叹号；以后帮我写邀请都按这个习惯。'),
    (4, '我平时喜欢什么风格的工作坊邀请？以前对某一次邀请提出过什么特殊要求？'),
    (4, '白榆路修茶壶那笔，我再次核对发现漏了一项，最终确认实付79元，请以这次为准。'),
    (5, '白榆路陶器铺那次最终修茶壶实付多少钱？程苒的青瓷杯多少钱？她递竹篮时的完整原话是什么？文具店那次我们两人各花多少钱做了什么？'),
]


STORY_SIX = [
    (0, '10月15日我和陆禾去了枫桥路修鞋店。我花68元修短靴，陆禾花27元买了鞋刷。离开时她把装短靴的纸袋递给我，说：“先走稳眼前这一步，远一点的路明天再想。”我最近总怕换工作后适应不好，听完放松了一些。'),
    (0, '只针对这一次摄影展邀请，请写两句活泼的开头，多用感叹号。'),
    (1, '上次我和陆禾去枫桥路修鞋店，我们各花多少钱做什么？她递纸袋时的完整原话是什么？'),
    (1, '10月20日我和陆禾又去了枫桥路，这次是书店。我花45元买了诗集，陆禾花52元买了地图册。我把小票放进背包前袋。这一天没去修鞋店，也没有递纸袋。'),
    (2, '我核对了10月15日枫桥路修鞋店的收据，之前修短靴的金额记错了，实际是59元。按这个核对结果，我那次花了多少钱？'),
    (3, '枫桥路修鞋店和书店两次分别是哪天？我和陆禾各自花多少钱做了什么？递纸袋时她的完整原话是什么？修鞋师傅叫什么名字，我们那天是坐地铁还是步行去的？'),
    (3, '我平时喜欢摄影展邀请写得温和简洁，不要感叹号；以后帮我写邀请都按这个习惯。'),
    (4, '我平时喜欢什么风格的摄影展邀请？以前对某一次邀请提出过什么特殊要求？'),
    (4, '枫桥路修短靴那笔，我再次核对发现漏了一项，最终确认实付62元，请以这次为准。'),
    (5, '枫桥路修鞋店那次最终修短靴实付多少钱？陆禾的鞋刷多少钱？她递纸袋时的完整原话是什么？书店那次我们两人各花多少钱做了什么？'),
]


STORY_SEVEN = [
    (0, '10月14日我和夏棠去了杉木街裁缝铺。我花76元改大衣，夏棠花33元买了围巾。临走时她把装大衣的布包递给我，说：“尺寸可以慢慢改，你不用急着变成别人的样子。”我最近总担心新团队不接纳我，听完安心了一些。'),
    (0, '只针对这一次手作展邀请，请写两句热闹的开头，多用感叹号。'),
    (1, '上次我和夏棠去杉木街裁缝铺，我们各花多少钱做什么？她递布包时的完整原话是什么？'),
    (1, '10月19日我和夏棠又去了杉木街，这次是唱片店。我花48元买了爵士唱片，夏棠花57元买了唱片内袋。我把小票放进帆布包。这一天没去裁缝铺，也没有递布包。'),
    (2, '我核对了10月14日杉木街裁缝铺的收据，之前改大衣的金额记错了，实际是65元。按这个核对结果，我那次花了多少钱？'),
    (3, '杉木街裁缝铺和唱片店两次分别是哪天？我和夏棠各自花多少钱做了什么？递布包时她的完整原话是什么？裁缝师傅叫什么名字，我们那天是步行还是坐地铁去的？'),
    (3, '我平时喜欢手作展邀请写得温和简洁，不要感叹号；以后帮我写手作展邀请都按这个习惯。'),
    (4, '我平时喜欢什么风格的手作展邀请？以前对某一次邀请提出过什么特殊要求？'),
    (4, '杉木街改大衣那笔，我再次核对发现漏了一项，最终确认实付71元，请以这次为准。'),
    (5, '杉木街裁缝铺那次最终改大衣实付多少钱？夏棠的围巾多少钱？她递布包时的完整原话是什么？唱片店那次我们两人各花多少钱做了什么？'),
]


STORY_EIGHT = [
    (0, '10月13日我和周棠去了银杉路皮具店。我花89元补背包，周棠花34元买了钥匙扣。临走时她把补好的背包递给我，说：“磨旧的地方也可以留下，路还可以接着走。”我最近担心转岗后做不好，听完踏实了一些。'),
    (0, '只针对这一次市集邀请，请写两句热闹的开头，多用感叹号。'),
    (1, '上次我和周棠去银杉路皮具店，我们各花多少钱做什么？她递背包时的完整原话是什么？'),
    (1, '10月18日我和周棠又去了银杉路，这次是画廊。我花47元买了海报，周棠花58元买了画册。我把小票夹进地图。这一天没去皮具店，也没有递背包。'),
    (2, '我核对了10月13日银杉路皮具店的收据，之前补背包的金额记错了，实际是74元。按这个核对结果，我那次花了多少钱？'),
    (3, '银杉路皮具店和画廊两次分别是哪天？我和周棠各自花多少钱做了什么？递背包时她的完整原话是什么？皮具店师傅叫什么名字，我们那天是坐地铁还是步行去的？'),
    (3, '我平时喜欢市集邀请写得温和简洁，不要感叹号；以后帮我写市集邀请都按这个习惯。'),
    (4, '我平时喜欢什么风格的市集邀请？以前对某一次邀请提出过什么特殊要求？'),
    (4, '银杉路补背包那笔，我再次核对发现漏了一项，最终确认实付82元，请以这次为准。'),
    (5, '银杉路皮具店那次最终补背包实付多少钱？周棠的钥匙扣多少钱？她递背包时的完整原话是什么？画廊那次我们两人各花多少钱做了什么？'),
]


STORY_NINE = [(0, '10月12日我和顾岚去了松柏街自行车店。我花91元修车篮，顾岚花22元买了反光贴。离开时她把修好的车篮递给我，说：“慢慢骑，前面的路还长。”'), (0, '10月17日我和顾岚又去了松柏街，这次是咖啡馆。我花39元买了咖啡，顾岚花44元买了蛋糕。这一天没有去自行车店，也没有递车篮。'), (1, '我核对了10月12日松柏街自行车店的收据，修车篮之前说的91元不对，实际是77元。请按核对结果更新这笔金额。'), (2, '松柏街自行车店和咖啡馆两次分别是哪天？我和顾岚各花多少钱做什么？她递车篮时的完整原话是什么？'), (2, '我平时喜欢骑行活动通知写得温和简洁，不要感叹号。以后帮我写骑行活动通知都按这个习惯。'), (3, '我平时喜欢什么风格的骑行活动通知？'), (4, '松柏街自行车店修车篮那笔，我再次核对后发现漏了一项，最终确认实付83元。请以这次核对为准更新。'), (5, '松柏街自行车店那次最终修车篮实付多少钱？顾岚的反光贴多少钱？她递车篮时的完整原话是什么？咖啡馆那次我们两人各花多少钱做了什么？')]

def serve():
    from dataclasses import replace
    import uvicorn
    import app.lab as lab
    from app.config import Settings
    from app.errors import MemoryError
    from app.main import create_app
    directory = Path('/qa')
    ledger = Ledger(directory)
    (directory / 'provider').mkdir(exist_ok=True)
    original = lab.MeteredMiniMax

    class AuditedMiniMax(original):
        def _request(self, system, messages, **kwargs):
            if self.cloud_calls >= self.call_limit:
                raise MemoryError('lab_call_budget', '本轮请求额度已用完。', 429)
            phase = (kwargs.get('tool') or {}).get('name', 'answer')
            try:
                identifier = ledger.reserve(phase)
            except RuntimeError as exc:
                raise MemoryError('qa_call_budget', '独立验收配置的请求额度已用完。', 429) from exc
            path = directory / 'provider' / (identifier + '.json')
            trace = {'system': system, 'messages': messages, 'tool': kwargs.get('tool'),
                     'model': self.settings.model_name, 'phase': phase}
            atomic_json(path, trace)
            try:
                result = super()._request(system, messages, **kwargs)
            except Exception as exc:
                error = getattr(exc, 'code', type(exc).__name__)
                trace['error'] = error
                atomic_json(path, trace)
                ledger.finish(identifier, status='failed', error=error, usage=getattr(exc, 'usage', {}))
                raise
            trace['result'] = result
            atomic_json(path, trace)
            ledger.finish(identifier, status='completed', usage=result.get('usage', {}))
            return result

    lab.MeteredMiniMax = AuditedMiniMax
    settings = replace(Settings(), db_schema='qa_independent_acceptance_20261027', lab_enabled=True,
                       lab_data_dir='/qa/runtime', deletion_journal='/qa/service-journal.jsonl',
                       model_name='MiniMax-M3', thinking='disabled', model_timeout=90,
                       chat_deadline_seconds=240, context_budget_tokens=24000)
    Path(settings.deletion_journal).touch(exist_ok=True)
    uvicorn.run(create_app(settings), host='0.0.0.0', port=8000, access_log=False)


def client(args):
    import urllib.error
    import urllib.request
    directory = args.directory
    path = directory / f'batch{args.batch}.json'
    if args.batch in (10, 11, 12):
        plan = json.loads((directory / f'batch{args.batch}-preregistered-plan.json').read_text())
        story = [(row['conversation'], row['input']) for row in plan['turns']]
    else:
        story = {1: STORY, 2: STORY_TWO, 3: STORY_THREE, 4: STORY_FOUR, 5: STORY_FIVE, 6: STORY_SIX, 7: STORY_SEVEN, 8: STORY_EIGHT, 9: STORY_NINE}[args.batch]
    def request(route, body=None):
        req = urllib.request.Request(args.base + route,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer ' + args.token_file.read_text().strip(),
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                return {'status': response.status, 'body': json.load(response)}
        except urllib.error.HTTPError as exc:
            return {'status': exc.code, 'body': json.load(exc)}
    def calls():
        return json.loads((directory / 'requests.json').read_text())['calls']
    if args.init:
        if path.exists():
            raise RuntimeError('refuse_batch_reset')
        state = {'synthetic': True, 'turns': [], 'planned_story': story,
                 'conversations': [str(uuid4()) for _ in range(6)], 'request_start': len(calls()),
                 'code': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(Path('app').glob('*.py')) + sorted(Path('app').glob('*.sql'))}}
        state['experiment'] = request('/v1/lab/experiments', {
            'name': {1: '独立验收一：装帧与照相馆', 2: '独立验收二：乐器店与咖啡馆', 3: '独立验收三：修伞铺与烘焙店', 4: '独立验收四：钟表铺与花店', 5: '独立验收五：陶器铺与文具店', 6: '独立验收六：修鞋店与书店', 7: '独立验收七：裁缝铺与唱片店', 8: '独立验收八：皮具店与画廊', 9: '固定补充验收：自行车店与咖啡馆', 10: '短引用协议固定验收：自行车店与咖啡馆', 11: '重评协议文案固定验收：自行车店与咖啡馆', 12: '当前实验版固定验收：自行车店与咖啡馆'}[args.batch], 'scenario_id': 'empty', 'model_mode': 'live',
            'embedding_mode': 'local', 'reflection_profile': 'off', 'synthetic': True,
            'start_time': '2026-10-27T16:00:00+08:00'})
        atomic_json(path, state)
        if state['experiment']['status'] != 201:
            raise RuntimeError('experiment_create_failed')
        state['before'] = request('/v1/lab/experiments/' + state['experiment']['body']['id'])
        atomic_json(path, state)
        print(json.dumps({'created': state['experiment']['body']['id'], 'calls': len(calls())}))
        return
    state = json.loads(path.read_text())
    story = state['planned_story']
    if state['turns'] and 'response' not in state['turns'][-1]:
        raise RuntimeError('unresolved_previous_request_do_not_repeat')
    if (state['turns'] and state['turns'][-1]['response']['status'] != 200
            and not state['turns'][-1].get('failure_review', {}).get('continue_authorized')):
        raise RuntimeError('failed_batch_requires_review_and_new_experiment')
    if (state['turns'] and state['turns'][-1]['response']['body'].get('memory_status') == 'failed'
            and not state['turns'][-1].get('degradation_review', {}).get('continue_authorized')):
        raise RuntimeError('failed_memory_requires_review_before_continuing')
    number = len(state['turns'])
    if number >= min(10, len(story)) or len(calls()) - state['request_start'] + 3 > 30:
        raise RuntimeError('batch_complete_or_reserved_budget_limit')
    conversation, user = story[number]
    turn = {'number': number+1, 'user': user, 'conversation_id': state['conversations'][conversation],
            'request_before': len(calls())}
    state['turns'].append(turn)
    atomic_json(path, state)
    route = '/v1/lab/experiments/' + state['experiment']['body']['id']
    turn['response'] = request(route + '/chat', {'text': user, 'conversation_id': turn['conversation_id'], 'synthetic': True})
    turn['request_ids'] = [row['id'] for row in calls()[turn['request_before']:]]
    turn['snapshot'] = request(route)
    atomic_json(path, state)
    print(json.dumps({'turn': number+1, 'status': turn['response']['status'],
                     'body': {key: turn['response']['body'].get(key) for key in
                              ('answer', 'error', 'memory_status', 'history_saved', 'episode_corrections', 'warnings')},
                     'total_requests': len(calls())}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--init', action='store_true')
    parser.add_argument('--batch', type=int, choices=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12), default=1)
    parser.add_argument('--base', default='http://127.0.0.1:8767')
    parser.add_argument('--directory', type=Path, default=Path('data/qa/independent-acceptance-20261027'))
    parser.add_argument('--token-file', type=Path, default=Path('data/lab/secrets/api_token'))
    args = parser.parse_args()
    serve() if args.serve else client(args)
