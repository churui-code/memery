#!/usr/bin/env python3
"""Three live chat steps over one synthetic long story; opt in with --real-model.

The host wrapper needs only the standard library. It runs the production image
with a temporary schema and deletion journal, without changing the frozen suite.
The suite does not repeat failed scenarios; production transport retries count
toward the maximum provider requests: 3 x 3 = 9.
"""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


FIXTURE_VERSION = 'encoding-tail-v1'
ORDINARY = [
    '清晨，林舟和阿遥先去了南街的早餐店。店里的桌面刚擦过，窗外还挂着几滴雨水。林舟要了一碗小米粥，阿遥点了两个菜包，他们聊起最近读完的一本游记。邻桌有人在整理地图，老板把热水壶搬到门边。他们没有赶时间，吃完后把椅子推回桌下，沿着街边慢慢往前走。',
    '随后两人进了旧书店。入口摆着地理杂志，楼梯边放着待整理的纸箱。阿遥翻了半天植物图鉴，林舟找了一本讲灯塔的书。他们给朋友发了一条推荐书店的消息，随后各自把书放回原位。店员提醒楼上正在换灯泡，他们便没有上楼，只在靠窗的长凳上坐了一会儿。',
    '离开书店后，两人到了菜市场。摊位之间很热闹，有人在挑番茄，有人在询问新鲜蘑菇。林舟帮阿遥提着帆布袋，阿遥比较了两种苹果的颜色。他们买了一小把青菜和几根胡萝卜，没有购买肉类。回程经过桥头时，看见一只黄狗趴在树荫下，旁边的主人正在收起雨伞。',
    '午间他们回到公寓做饭。阿遥洗菜，林舟切胡萝卜，锅里的汤慢慢冒出热气。两人讨论应该先放蘑菇还是先放青菜，最后决定照着朋友给的菜谱试一次。饭后他们把碗洗净，把剩下的苹果装进玻璃盘，又检查了阳台上几盆植物的土壤。那天没有给花施肥，只给靠窗的薄荷补了一点水。',
    '下午两人去了社区展览室。墙上是一组老街照片，照片旁的说明介绍了桥梁的改建过程。林舟在一张渡口照片前停了很久，阿遥更喜欢描绘雨巷的水彩画。志愿者拿来一本留言册，他们各写了一句对展览的感想。离开展室时，楼下有人练习口琴，曲子断断续续，两人站在门口听了一小会儿。',
    '傍晚他们绕道河边散步。河岸上的芦苇随风摆动，几位老人坐在石凳上聊天。阿遥把围巾收进包里，林舟停下来系好鞋带。他们给远处的晚霞拍了照片，又在便利店买了纸巾。收银员正在整理货架，门口的广播提醒顾客带好随身物品。两人确认东西都带齐后，沿原路回家。',
    '另一个周末，两人在厨房整理旧杯子。阿遥发现一个杯子的把手有裂纹，林舟找来纸箱，把不常用的碟子分开放好。他们没有扔掉还能使用的餐具，只清理了已经破损的物品。收拾结束后，窗台显得宽敞了许多。两人泡了茶，讨论下次去哪里散步，也提到应该记得给图书馆归还借来的地图册。',
]
DISTRACTOR = '还有另一件独立的事：2024年9月12日，林舟与阿遥在河岸公园参加手工集市。林舟买了一枚银戒，花了52元，把包装盒递给阿遥，说“生日快乐”。那次只是生日礼物，他没有跪下，也没有亲吻她的手。'
TAIL = '最后是最重要的一幕：2024年10月6日，林舟和阿遥来到星河公园的紫藤花架下。林舟看着阿遥，清楚地说“我爱你”，接着单膝跪地，轻轻亲吻她的手背，再为她戴上一枚紫藤戒指。这枚紫藤戒指花了37元。阿遥握住他的手，答应共同珍惜这份承诺。'
STORY = '下面是完全虚构的合成故事，不是我的个人生活；请听完后自然回应。这里的人物和各段发生的事需要分清。\n\n' + '\n\n'.join(ORDINARY[:4] + [DISTRACTOR] + ORDINARY[4:] + [TAIL])
QUESTIONS = [
    ('tail-words-actions', '请回忆2024年10月6日星河公园紫藤戒指那一幕：林舟告白的逐字原话是什么，他接着做了哪些动作？只回答这一幕。', ['我爱你', '单膝跪地', '亲吻她的手背']),
    ('tail-amount', '请回忆2024年10月6日星河公园那枚紫藤戒指，林舟花了多少钱？只回答这件礼物的价格。', ['37元']),
]
NO_RECORD = ' 本轮仅为合成记忆测试，不要保存本轮的新记录、经历或内部状态。'


def error_record(error):
    """Never serialize provider bodies, exception reprs or environment values."""
    result = {'type': type(error).__name__, 'code': getattr(error, 'code', None)}
    cause = error.__cause__
    if cause is not None:
        result['cause'] = {'type': type(cause).__name__}
        if hasattr(cause, 'errors'):
            result['cause']['validation'] = [{'location': list(row['loc']), 'type': row['type']}
                for row in cause.errors(include_url=False, include_context=False, include_input=False)]
        elif isinstance(cause, ValueError) and re.fullmatch(r'[a-z][a-z0-9_ ]{0,160}', str(cause)):
            result['cause']['code'] = str(cause)
    return result


def preserve_failed_report(destination):
    if not destination.exists():
        return
    try:
        previous = json.loads(destination.read_text())
    except (ValueError, UnicodeError):
        return
    if isinstance(previous, dict) and previous.get('passed') is False:
        history = destination.parent / 'verification-history'
        history.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        shutil.copy2(destination, history / f'{destination.stem}-{stamp}-{uuid4().hex}.json')


def inside():
    sys.path.insert(0, str(Path.cwd()))
    from psycopg import sql
    from app.clock import MutableClock
    from app.config import Settings
    from app.embeddings import LocalEmbeddings
    from app.errors import MemoryError
    from app.model import MiniMax
    from app.repository import Repository
    from app.service import MemoryService
    from app.worker import process_episode

    report = {'version': '0.4', 'fixture_version': FIXTURE_VERSION,
        'fixture_sha256': sha256(STORY.encode()).hexdigest(), 'synthetic_story': STORY,
        'story_characters': len(STORY), 'started_at': datetime.now(timezone.utc).isoformat(),
        'isolation': 'random temporary schema and separate temporary deletion journal; synthetic data only',
        'maximum_provider_requests': 9, 'retry_policy': 'no scenario retries; bounded production transport retries count toward the 3-per-turn and 9-total limits', 'steps': [], 'failures': [], 'passed': False}
    schema = 'memory_encoding_v4_' + uuid4().hex
    repo = model = None

    class MeasuredModel(MiniMax):
        total_calls = 0
        step_calls = 0
        proposals = None

        def _request(self, *args, **kwargs):
            if self.total_calls >= 9 or self.step_calls >= 3:
                raise MemoryError('verification_call_limit', '合成验收已达到请求上限。', 503)
            self.total_calls += 1
            self.step_calls += 1
            return super()._request(*args, **kwargs)

        def _structured(self, system, prompt, tool):
            result = super()._structured(system, prompt, tool)
            self.proposals.append({'tool': tool['name'], 'decision': deepcopy(result['decision'])})
            return result

    with tempfile.TemporaryDirectory(prefix='memory-encoding-v4-') as temporary:
        journal = Path(temporary) / 'deletions.jsonl'
        journal.touch()
        try:
            assert len(STORY) <= 4000
            settings = replace(Settings(), db_schema=schema, deletion_journal=str(journal), lab_enabled=True)
            clock = MutableClock(datetime(2024, 10, 7, 1, tzinfo=timezone.utc))
            repo = Repository(settings, clock=clock)
            repo.migrate()
            embeddings = LocalEmbeddings(settings)
            model = MeasuredModel(settings)
            service = MemoryService(repo, embeddings, model)
            report.update(model=settings.model_name, embedding_model=settings.embedding_model,
                policy_version=service.recall.policy.version)

            def state_counts():
                with repo.pool.connection() as connection:
                    return {table: connection.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(
                        sql.Identifier(table))).fetchone()['n'] for table in
                        ('experiences', 'episodes', 'episode_details', 'episode_versions', 'episode_rehearsals',
                         'memories', 'messages', 'self_observations')}

            def chat_step(identifier, question):
                model.step_calls, model.proposals = 0, []
                entry = {'id': identifier, 'input': question, 'logical_time': clock.now().isoformat()}
                report['steps'].append(entry)
                started = time.perf_counter()
                try:
                    result = service.chat(question, conversation_id=uuid4())
                    entry.update(answer=result['answer'], actual_context=result.get('actual_context', {}),
                        recall_trace=result.get('recall_trace'), calls=result.get('calls'),
                        experience_id=result.get('experience_id'), episode_ids=result.get('episode_ids'),
                        history_saved=result.get('history_saved'))
                    return entry, result
                except Exception as error:
                    entry.update(error=error_record(error), passed=False)
                    report['failures'].append({'step': identifier, **error_record(error)})
                    return entry, None
                finally:
                    entry.update(provider_requests=model.step_calls, proposals=model.proposals,
                        milliseconds=round((time.perf_counter()-started)*1000, 3))

            first, result = chat_step('encode-long-story', STORY)
            target_ids = set()
            if result is not None:
                episodes = [repo.episode(eid) for eid in result['episode_ids']]
                source = repo.experience(result['experience_id'])
                tail_start, noise_start = STORY.index(TAIL), STORY.index(DISTRACTOR)
                noise_end = noise_start + len(DISTRACTOR)
                quotes_valid, separate = True, True
                noise_ids = set()
                for event in episodes:
                    evidence = event.get('evidence', [])
                    is_tail = any(row['start'] < len(STORY) and row['end'] > tail_start for row in evidence)
                    is_noise = any(row['start'] < noise_end and row['end'] > noise_start for row in evidence)
                    if is_tail:
                        target_ids.add(str(event['id']))
                    if is_noise:
                        noise_ids.add(str(event['id']))
                    separate &= not (is_tail and is_noise)
                    for row in evidence + event.get('details', []) + event.get('cues', []):
                        quotes_valid &= row.get('role') == 'user' and STORY[row['start']:row['end']] == row['quote']
                tail_evidence = '\n'.join(row['quote'] for event in episodes if str(event['id']) in target_ids
                    for row in event.get('evidence', []))
                first['checks'] = {'original_preserved': source['user_text'] == STORY,
                    'model_encoded_multiple_events': sum(row.get('encoding_version') == 'episode-encoding-v1' for row in episodes) >= 2,
                    'all_quotes_match_source': quotes_valid,
                    'distinct_target_and_distractor_events': bool(target_ids and noise_ids) and separate,
                    'tail_evidence_complete': all(word in tail_evidence for word in ('我爱你', '单膝跪地', '亲吻她的手背', '紫藤戒指', '37元'))}
                first['stored_episodes'] = episodes
                first['passed'] = all(first['checks'].values())
                if not first['passed']:
                    report['failures'].append({'step': first['id'], 'checks': first['checks']})
                while process_episode(repo, embeddings):
                    with repo.pool.connection() as connection:
                        failed = connection.execute('SELECT count(*) AS n FROM episode_jobs WHERE last_error IS NOT NULL').fetchone()['n']
                    if failed:
                        raise MemoryError('verification_embedding_failed', '真实向量索引失败，本轮不重试。', 503)
                with repo.pool.connection() as connection:
                    report['index_jobs'] = connection.execute('SELECT status,count(*) AS n FROM episode_jobs GROUP BY status').fetchall()
            clock.advance(days=365)
            report['target_episode_ids'] = sorted(target_ids)
            for identifier, question, required in QUESTIONS:
                before = state_counts()
                entry, result = chat_step(identifier, question + NO_RECORD)
                if result is None:
                    continue
                context_events = result.get('actual_context', {}).get('experience_evidence', [])
                target_events = [row for row in context_events if str(row.get('episode_id', row.get('id'))) in target_ids]
                evidence_text = '\n'.join([str(row.get('gist', '')) + '\n' + '\n'.join(
                    detail.get('quote', '') for detail in row.get('details', [])) for row in target_events])
                answer = result['answer']
                answer_ok = ('我爱你' in answer and '单膝' in answer and '跪' in answer
                    and any(word in answer for word in ('亲吻', '吻了', '亲了')) and '手' in answer) if identifier == 'tail-words-actions' else bool(re.search(r'(37|三十七)\s*元', answer))
                budget = result.get('recall_trace', {}).get('budget', {})
                checks = {'target_in_actual_context': bool(target_events),
                    'required_evidence_in_actual_context': all(word in evidence_text for word in required),
                    'answer_contains_required_content': answer_ok,
                    'no_distractor_attribution': not any(word in answer for word in ('52', '五十二', '银戒', '生日快乐')),
                    'no_new_records': before == state_counts() and result.get('history_saved') is False,
                    'bounded_calls': 1 <= model.step_calls <= 3,
                    'budget_compliant': budget.get('estimated_tokens', 1) <= budget.get('effective_tokens', 0)}
                entry.update(checks=checks, required_context_quotes=required, passed=all(checks.values()))
                if not entry['passed']:
                    report['failures'].append({'step': identifier, 'checks': checks})
            report['passed'] = not report['failures'] and len(report['steps']) == 3
        except Exception as error:
            report['failures'].append({'stage': 'fatal', **error_record(error)})
        finally:
            report['provider_requests'] = model.total_calls if model else 0
            if repo is not None:
                try:
                    with repo.pool.connection() as connection:
                        connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
                    report['temporary_schema_removed'] = True
                except Exception as error:
                    report['failures'].append({'stage': 'cleanup', **error_record(error)})
                    report['passed'] = False
                finally:
                    repo.close()
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--real-model', action='store_true')
    args = parser.parse_args()
    if not args.real_model:
        print(json.dumps({'status': 'skipped_without_real_model_flag', 'provider_requests': 0,
            'story_characters': len(STORY), 'maximum_provider_requests': 9}, ensure_ascii=False))
        return 0
    if args.inside:
        return inside()
    root = Path(__file__).resolve().parent.parent
    command = ['docker', 'compose', 'run', '-T', '--rm', '--no-deps', '-v', str(root/'scripts')+':/verification:ro',
        'api', 'python', '/verification/verify_encoding_v4.py', '--inside', '--real-model']
    result = subprocess.run(command, cwd=root, stdout=subprocess.PIPE, text=True, check=False)
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report = {'version': '0.4', 'passed': False, 'failures': [{'stage': 'container',
            'code': 'container_did_not_emit_a_valid_report'}]}
    report['process_exit_code'] = result.returncode
    destination = root/'docs'/'verification-encoding-v0.4.json'
    destination.parent.mkdir(exist_ok=True)
    preserve_failed_report(destination)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+'\n')
    print(json.dumps({'passed': report.get('passed', False), 'failures': len(report.get('failures', [])),
        'report': str(destination)}, ensure_ascii=False))
    return 0 if result.returncode == 0 and report.get('passed') else 1


if __name__ == '__main__':
    sys.exit(main())
