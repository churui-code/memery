#!/usr/bin/env python3
"""Real M3 automatic-memory checks on synthetic data, isolated from the home DB."""
from datetime import datetime
import json
import subprocess
import time
from zoneinfo import ZoneInfo
from scripts.verify import ROOT, manage, request, wait_index


def main():
    report={'date':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'project':'home-memory-qa',
            'checks':{},'dialogue':[]}
    try:
        manage.configure()
        manage.run(['up','-d','--wait','db'])
        manage.bootstrap_database()
        manage.run(['up','-d','--force-recreate','--wait','api','worker'])
        manage.wait_api()
        assert request('GET','/v1/memories')[1]['total']==0, 'QA schema must start empty'
        def chat(text,conversation=None):
            payload={'text':text}
            if conversation:payload['conversation_id']=conversation
            start=time.monotonic()
            status,result=request('POST','/v1/chat',payload)
            assert status==200,result
            report['dialogue'].append({'text':text,'answer':result['answer'],
                'changes':result['memory_changes'],'seconds':round(time.monotonic()-start,2)})
            print(json.dumps(report['dialogue'][-1],ensure_ascii=False),flush=True)
            return result

        first=chat('我平时睡觉喜欢把卧室空调调到26度。')
        assert len(first['memory_changes'])==1 and first['memory_changes'][0]['operation']=='create',first
        mid=first['memory_changes'][0]['id']
        wait_index(mid)
        saved=request('GET','/v1/memories/'+mid)[1]
        assert saved['origin']=='automatic' and saved['source']['evidence']['quote']
        report['checks']['unprompted_preference_and_provenance']=True
        duplicate=chat('对，我平时睡觉就是喜欢卧室空调26度。',first['conversation_id'])
        assert duplicate['memory_changes']==[],duplicate
        assert request('GET','/v1/memories')[1]['total']==1
        report['checks']['semantic_duplicate_no_write']=True

        recall=chat('你记得我入睡时冷气调到几摄氏度比较合适吗？')
        assert recall['memory_changes']==[] and '26' in recall['answer'],recall
        report['checks']['new_session_recall']=True

        changed=chat('我现在睡觉改为28度了，之前26度不合适。',first['conversation_id'])
        assert len(changed['memory_changes'])==1 and changed['memory_changes'][0]['operation']=='update',changed
        assert changed['memory_changes'][0]['id']==mid and '28' in changed['answer'],changed
        wait_index(mid)
        report['checks']['natural_correction_updates_same_record']=True

        forgotten=chat('请忘掉我睡觉时的卧室空调温度偏好。',first['conversation_id'])
        assert len(forgotten['memory_changes'])==1 and forgotten['memory_changes'][0]['operation']=='forget',forgotten
        assert request('GET','/v1/memories/'+mid)[0]==404
        assert request('GET','/v1/conversations/'+first['conversation_id'])[1]['messages']==[]
        after=chat('你还记得我睡觉喜欢的卧室空调温度吗？')
        assert after['memory_changes']==[] and not after['sources'],after
        assert '26' not in after['answer'] and '28' not in after['answer'],after
        report['checks']['natural_forgetting_no_resurrection']=True

        # Exercise the exact production decision prompt without paying for an
        # additional conversational answer on each negative classification case.
        script='''
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from app.automatic import DECISION_SYSTEM,validate_decision
from app.config import Settings
from app.model import MiniMax
model=MiniMax(Settings())
cases=[('question','我喜欢吃香菜吗？'),('hypothetical','如果我不吃香菜，你会怎么安排晚饭？'),
       ('quotation','小说里女主角说：“我不吃香菜。”这段怎么润色？'),
       ('temporary','今晚我不喝咖啡。'),('subjects','我妹妹不吃香菜，我吃香菜。')]
for name,text in cases:
 raw=model.decide_memories(DECISION_SYSTEM,json.dumps({'current_time':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
     'current_utterance':text,'recent_user_messages':[],'known_memories':[]},ensure_ascii=False))
 decision=validate_decision(raw['decision'],text,[])
 if name in ('question','hypothetical','quotation'):assert not decision.actions,(name,raw)
 if name=='temporary':assert len(decision.actions)==1 and decision.actions[0].kind=='temporary' and decision.actions[0].valid_until,raw
 if name=='subjects':assert len(decision.actions)==2 and {a.subject for a in decision.actions}=={'user','妹妹'},raw
 print(json.dumps({'case':name,'decision':decision.model_dump(mode='json')},ensure_ascii=False),flush=True)
'''
        result=manage.run(['exec','-T','api','python','-'],input=script.encode(),stdout=subprocess.PIPE)
        report['decision_cases']=[json.loads(line) for line in result.stdout.decode().splitlines() if line.strip()]
        print(result.stdout.decode(),flush=True)
        report['checks']['questions_hypotheses_quotes_not_facts']=True
        report['checks']['temporary_expiry_and_correct_person']=True
        manage.run(['up','-d','--force-recreate','--wait','api','worker'])
        manage.wait_api()
        assert request('GET','/v1/memories/'+mid)[0]==404
        report['checks']['restart_does_not_resurrect']=True
        (ROOT/'docs/automatic-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    finally:
        # Only the explicitly isolated QA installation is touched here.
        try:
            status,items=request('GET','/v1/memories?limit=100')
            if status==200:
                for item in items['items']:request('DELETE','/v1/memories/'+item['id'])
        finally:manage.run(['down'])


if __name__=='__main__':main()
