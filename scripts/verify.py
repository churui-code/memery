#!/usr/bin/env python3
"""Deployment/restore drill in a separate Compose project; never restores production."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
os.environ.update(COMPOSE_PROJECT_NAME='home-memory-qa',MEMORY_DATA_DIR=str(ROOT/'data/qa'),MEMORY_PORT='18765')
sys.path.insert(0,str(ROOT))
from scripts import manage


def request(method,path,data=None):
    token=(ROOT/'.secrets/api_token').read_text().strip()
    req=urllib.request.Request('http://127.0.0.1:18765'+path,
        data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method=method)
    try:
        with urllib.request.urlopen(req,timeout=300) as response:return response.status,json.load(response)
    except urllib.error.HTTPError as exc:return exc.code,json.loads(exc.read())


def wait_index(mid):
    for _ in range(90):
        status,item=request('GET','/v1/memories/'+mid)
        assert status==200,item
        if item['index_status']=='done':return item
        if item['index_status']=='failed':raise RuntimeError('real embedding indexing failed')
        time.sleep(1)
    raise RuntimeError('indexing timeout')


def verify_offline():
    """Keep API/DB on a private internal network while removing API internet access."""
    api=manage.run(['ps','-q','api'],stdout=subprocess.PIPE).stdout.decode().strip()
    db=manage.run(['ps','-q','db'],stdout=subprocess.PIPE).stdout.decode().strip()
    private='home-memory-qa-offline-'+uuid4().hex[:8]
    default='home-memory-qa_default'
    detached=False
    subprocess.run(['docker','network','create','--internal',private],check=True,stdout=subprocess.DEVNULL)
    try:
        subprocess.run(['docker','network','connect','--alias','db',private,db],check=True)
        subprocess.run(['docker','network','connect',private,api],check=True)
        subprocess.run(['docker','network','disconnect',default,api],check=True)
        detached=True
        subprocess.run(['docker','restart',api],check=True,stdout=subprocess.DEVNULL)
        script='''
import json,time,urllib.request,urllib.error
from pathlib import Path
token=Path('/run/secrets/api_token').read_text().strip()
def call(method,path,data=None):
 req=urllib.request.Request('http://127.0.0.1:8000'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method=method)
 try:
  with urllib.request.urlopen(req,timeout=110) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
for _ in range(60):
 try:
  if call('GET','/health')[0]==200:break
 except Exception:pass
 time.sleep(.5)
status,item=call('POST','/v1/memories',{'text':'离线验收：阅读时喜欢柔和的灯光。'})
assert status==201,item
try:
 status,found=call('POST','/v1/search',{'query':'看书时喜欢什么照明'})
 assert status==200 and found['mode']=='hybrid' and any(i['id']==item['id'] for i in found['items']),found
 status,error=call('POST','/v1/chat',{'text':'仅用于断网验证的合成请求'})
 assert status in (502,503,504),(status,error)
 assert call('GET','/v1/memories/'+item['id'])[0]==200
 print(json.dumps({'offline_save_and_semantic_search':True,'offline_model_failure_preserves_storage':True}))
finally:call('DELETE','/v1/memories/'+item['id'])
'''
        output=subprocess.run(['docker','exec','-i',api,'python','-'],input=script.encode(),check=True,stdout=subprocess.PIPE).stdout
        return json.loads(output)
    finally:
        if detached:
            subprocess.run(['docker','network','connect','--alias','api',default,api],check=True)
        subprocess.run(['docker','network','disconnect',private,api],check=False,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','network','disconnect',private,db],check=False,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','network','rm',private],check=False,stdout=subprocess.DEVNULL)
        subprocess.run(['docker','restart',api],check=True,stdout=subprocess.DEVNULL)
        manage.wait_api()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--real-model',action='store_true',help='用合成信息实调 M3 三次')
    args=parser.parse_args()
    report={'date':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'project':'home-memory-qa','checks':{},'model_calls':[]}
    created=[]
    conversations=[]
    try:
        manage.configure()
        manage.run(['up','-d','--wait','db'])
        manage.bootstrap_database()
        manage.run(['up','-d','--wait','api','worker'])
        manage.wait_api()
        marker='验收-'+uuid4().hex[:8]
        status,item=request('POST','/v1/memories',{'text':marker+'：卧室睡觉时我喜欢把空调设置为26度。'})
        assert status==201,item
        mid=item['id'];created.append(mid)
        wait_index(mid)
        life_script='''
import json
from uuid import UUID,uuid4
from app.config import Settings
from app.life import SelfUpdate
from app.repository import Repository
repo=Repository(Settings())
try:
 text='合成验收：讨论紫藤故事，形成关于承诺的观点。'
 result=repo.save_answer(uuid4(),text,'合成回答：承诺要通过行动体现。',[UUID(MEMORY_ID)],repo.epoch(),
     self_updates=[SelfUpdate(domain='belief',key='合成验收观点',text='承诺通过行动体现',
         reason='来自合成验收讨论',quote=text,confidence=.9)])
 print(json.dumps({'experience_id':str(result['experience_id']),'self_id':str(result['self_changes'][0]['id']),'episode_id':str(result['episode_ids'][0])}))
finally:repo.close()
'''.replace('MEMORY_ID',repr(mid))
        life_result=manage.run(['exec','-T','api','python','-'],input=life_script.encode(),stdout=subprocess.PIPE)
        life=json.loads(life_result.stdout)
        assert request('GET','/v1/experiences/'+life['experience_id'])[0]==200
        assert request('GET','/v1/episodes/'+life['episode_id'])[0]==200
        _,search=request('POST','/v1/search',{'query':'晚上睡觉冷气调几摄氏度比较合我心意？'})
        assert any(hit['id']==mid and 'semantic' in hit['methods'] for hit in search['items']),search
        report['checks']['real_local_semantic_search']=True

        report['checks'].update(verify_offline())
        if args.real_model:
            for expected,text in [('26','你记得我卧室睡觉时喜欢空调几度吗？'),('28','请根据记忆说出我现在喜欢的卧室睡眠温度。')]:
                if expected=='28':
                    status,item=request('PATCH','/v1/memories/'+mid,{'expected_revision':1,'text':marker+'：我现在卧室睡觉喜欢把空调设置为28度。'})
                    assert status==200,item
                    wait_index(mid)
                start=time.monotonic()
                status,reply=request('POST','/v1/chat',{'text':text})
                assert status==200,reply
                assert expected in reply['answer'] and any(source['id']==mid for source in reply['sources']),reply
                conversations.append(reply['conversation_id'])
                report['model_calls'].append({'expected':expected,'seconds':round(time.monotonic()-start,2),'answer':reply['answer']})

        manage.run(['up','-d','--force-recreate','--wait','api','worker'])
        manage.wait_api()
        assert request('GET','/v1/memories/'+mid)[0]==200
        report['checks']['container_recreation_persistence']=True
        backup=manage.backup()
        manage.restore(backup)
        assert request('GET','/v1/experiences/'+life['experience_id'])[1]['user_text'].startswith('合成验收')
        assert any(state['id']==life['self_id'] for state in request('GET','/v1/self')[1]['states'])
        assert request('GET','/v1/episodes/'+life['episode_id'])[0]==200
        report['checks']['streaming_restore_preserves_experience_episode_and_self']=True
        assert request('DELETE','/v1/memories/'+mid)[0]==200
        assert request('GET','/v1/memories/'+mid)[0]==404
        manage.restore(backup)
        assert request('GET','/v1/memories/'+mid)[0]==404
        assert request('GET','/v1/experiences/'+life['experience_id'])[0]==404
        assert request('GET','/v1/episodes/'+life['episode_id'])[0]==404
        assert not any(state['id']==life['self_id'] for state in request('GET','/v1/self')[1]['states'])
        report['checks']['encrypted_restore_replays_later_deletion']=True

        # Tampering must fail before stopping/replacing the live database.
        tampered=backup.with_suffix('.tampered')
        body=bytearray(backup.read_bytes());body[-1]^=1;tampered.write_bytes(body)
        try:
            try:manage.restore(tampered)
            except (subprocess.CalledProcessError,RuntimeError):pass
            else:raise AssertionError('tampered backup accepted')
            assert request('GET','/health')[0]==200
            report['checks']['tampered_backup_rejected_without_service_loss']=True
        finally:tampered.unlink(missing_ok=True)

        if args.real_model:
            status,reply=request('POST','/v1/chat',{'text':'你记得我卧室睡觉喜欢的具体空调温度吗？没有记忆请说不知道。'})
            assert status==200 and all(source['id']!=mid for source in reply['sources']),reply
            assert '不知道' in reply['answer'] or '没有' in reply['answer'] or '不记得' in reply['answer'],reply
            conversations.append(reply['conversation_id'])
            report['model_calls'].append({'expected':'unknown_after_delete','answer':reply['answer']})
            report['checks']['real_model_create_correct_forget']=True
        for cid in conversations:request('DELETE','/v1/conversations/'+cid)
        report['checks']['synthetic_online_data_cleaned']=True
        report['passed']=True
    finally:
        for mid in created:
            try:request('DELETE','/v1/memories/'+mid)
            except Exception:pass
        for cid in conversations:
            try:request('DELETE','/v1/conversations/'+cid)
            except Exception:pass
        (ROOT/'docs/verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        manage.run(['down'])
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
