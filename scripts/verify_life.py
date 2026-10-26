#!/usr/bin/env python3
"""Real-model life-memory acceptance, using an isolated schema and synthetic story."""
import json
from pathlib import Path
import subprocess
import sys


def inside():
    sys.path.insert(0,str(Path.cwd()))
    from dataclasses import replace
    from datetime import datetime
    from uuid import uuid4
    from zoneinfo import ZoneInfo
    from psycopg import sql
    from app.config import Settings
    from app.embeddings import LocalEmbeddings
    from app.model import MiniMax
    from app.repository import Repository
    from app.service import MemoryService
    from app.worker import process_experience

    schema='life_qa_'+uuid4().hex
    journal=Path('/tmp')/schema/'deletions.jsonl'
    journal.parent.mkdir();journal.touch()
    settings=replace(Settings(),db_schema=schema,deletion_journal=str(journal))
    repo=Repository(settings)
    report={'date':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'isolation':'temporary schema, synthetic story, independent deletion journal',
            'checks':{},'dialogue':[]}
    try:
        repo.migrate()
        embeddings=LocalEmbeddings(settings)
        service=MemoryService(repo,embeddings,MiniMax(settings))
        def chat(text):
            result=service.chat(text)
            report['dialogue'].append({'question':text,'answer':result['answer'],
                'experience_id':str(result['experience_id']),'self_changes':result['self_changes']})
            print('life verification turn '+str(len(report['dialogue']))+' completed',file=sys.stderr,flush=True)
            return result

        story=('以下全部是合成故事，不是我个人真实经历：角色林舟和阿遥去了星河公园，看花、吃火锅。'
               '林舟对阿遥说“我爱你”，随后单膝跪地、亲吻她的手、给她戴上紫藤戒指。买小吃花了37元。'
               '我们讨论这个场景：你觉得其中什么最有意义？请保留这次讨论的重要原话与动作。')
        first=chat(story)
        saved=repo.experience(first['experience_id'])
        assert saved['user_text']==story
        assert saved['context_type'] in ('fiction','hypothetical'),saved['context_type']
        assert any('我爱你' in a['quote'] for a in saved['anchors']),saved['anchors']
        assert repo.listing()['total']==0,'fiction must not become a personal fact'
        report['checks']['full_story_key_quotes_and_fiction_boundary']=True
        while process_experience(repo,embeddings):pass
        semantic=service.experiences('那次佩戴指环的告白发生了什么',cloud_only=True)
        assert any(row['id']==first['experience_id'] and 'semantic' in row['methods'] for row in semantic)
        report['checks']['real_local_semantic_retrieval']=True

        recall=chat('刚才合成故事中，林舟在公园说的原话是什么？戴戒指前做了哪些动作？小吃花了多少元？')
        assert all(word in recall['answer'] for word in ('我爱你','单膝','亲吻','37')),recall['answer']
        assert any(row['id']==first['experience_id'] for row in recall['experience_sources'])
        report['checks']['new_session_preserves_words_actions_and_small_detail']=True

        chat('我们继续讨论这个合成故事。我提出一个观点：“承诺的意义在于之后的行动，而不只是戴戒指的瞬间”。'
             '你怎样看？请形成你自己的观点，可以不同意我；把你自己的理解及形成原因保留，供以后继续讨论。')
        states=repo.self_state()
        assert states,'real model should retain a reasoned internal interpretation'
        assert all(row['reason'] and row['evidence_quote'] for row in states)
        report['checks']['real_model_forms_sourced_internal_interpretation']=True
        continued=chat('你对承诺有什么已经形成的看法？请联系先前的合成故事说明你的理由。')
        assert '承诺' in continued['answer']
        report['checks']['internal_interpretation_participates_in_next_answer']=True

        repo.close();repo=Repository(settings)
        repo.migrate();repo.replay_deletions(required=True)
        assert repo.experience(first['experience_id'])['user_text']==story and repo.self_state()
        report['checks']['reopen_preserves_experience_and_self']=True
        repo.forget_experience(first['experience_id'])
        repo.replay_deletions(required=True)
        assert repo.search_experiences('紫藤戒指')==[] and repo.self_state()==[]
        report['checks']['source_deletion_revokes_derived_recall_and_self']=True
        print(json.dumps(report,ensure_ascii=False,default=str))
    finally:
        with repo.pool.connection() as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        repo.close()


def main():
    if '--inside' in sys.argv:
        inside()
        return
    root=Path(__file__).resolve().parent.parent
    result=subprocess.run(['docker','compose','run','--rm','--no-deps','-T',
        '-v',str(root/'scripts')+':/verification:ro','api',
        'python','/verification/verify_life.py','--inside'],cwd=root,check=True,stdout=subprocess.PIPE)
    report=json.loads(result.stdout)
    destination=root/'docs/life-verification.json'
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'checks':report['checks'],'report':str(destination)},ensure_ascii=False))


if __name__=='__main__':main()
