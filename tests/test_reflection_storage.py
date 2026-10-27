"""Reflection transactions, durable privacy fences and real-time quotas."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb
from app.clock import MutableClock
from app.backup import export_reflection_quota, merge_reflection_quota
from app.context import content_hash
from app.errors import MemoryError
from app.life import ExperienceDescription, SelfUpdate
from app.source_units import source_units
from reflection_storage_helpers import drop_reflection_schema


def source(repo,text='合成故事里，承诺需要后续行动。',**kwargs):
    result=repo.save_answer(uuid4(),text,'合成回复。',[],repo.epoch(),**kwargs)
    return repo.episode(result['episode_ids'][0])


def prepare(repo,events=None,states=None,**kwargs):
    events=events if events is not None else repo.reflection_candidates()
    refs=[]
    for event in events:
        refs.append({'source_type':'episode','id':str(event['id']),'revision':event['revision'],
                     'source_hash':event['source_hash'],'hash':content_hash(str(event['id'])),
                     'evidence_ids':[str(row['id']) for row in event['evidence']],'pending':True})
        for eid in event['source_experience_ids']:
            original=repo.experience(eid)
            refs.append({'source_type':'experience','id':str(eid),'revision':original['revision'],
                         'source_hash':original['source_hash'],'hash':original['source_hash'],'spans':[]})
    targets=[]
    for state in states or []:
        refs.append({'source_type':'self','id':str(state['id']),'revision':state['revision'],
                     'hash':content_hash(str(state['id'])),'as_of':repo.clock.now().isoformat()})
        targets.append({'domain':state['domain'],'state_key':state['state_key'],
                        'id':str(state['id']),'revision':state['revision']})
    args={'manifest':refs,'expected_epoch':repo.epoch(),'target_versions':targets,
          'input_signature':content_hash([refs,targets]),'pending_ids':[str(event['id']) for event in events]}
    args.update(kwargs)
    return args


def start(repo,**kwargs):
    run=repo.enqueue_reflection(**prepare(repo,**kwargs))
    return repo.claim_reflection(run['id'])


def calls(repo,job,proposed=1):
    phases=['reflection_proposal']+(['reflection_review'] if proposed else [])
    for phase in phases:
        cid=repo.start_reflection_call(job,phase,estimated_input_tokens=100,estimator_version='test-v1')
        repo.finish_call(cid,phase+'_completed',{'input_tokens':20,'output_tokens':10})
    job['audit']={'proposed':proposed,'accepted':proposed,'rejected':0,'calls':len(phases)}


def update(event,key='承诺',domain='belief',**kwargs):
    quote=max(event['evidence'],key=lambda row:len(row['quote']))
    result={'id':uuid4().hex,'domain':domain,'key':key,'text':'承诺应结合后续行动理解。','reason':'从合成故事的言语与行动联系重新理解。',
            'confidence':.7,'evidence_ids':[str(quote['id'])],'expected_revision':0,'target_id':None}
    result.update(kwargs)
    if domain=='emotion':result.setdefault('intensity_direction','decrease' if result['target_id'] else 'new')
    return result


def review_for(updates):
    return {'reviews':[{'proposal_id':data['id'],'accept':True,'reason':'合成证据逐句核对。',
        'checks':[{'unit_id':f'{data["id"]}:{field}:{unit["id"]}','support':'interpretation',
                   'evidence_ids':data['evidence_ids'],'note':'原文支持这项有边界的解释。'}
                  for field in ('text','reason') for unit in source_units(data[field])]} for data in updates]}


def save(repo,job,updates):
    return repo.save_reflection(job,updates,review=review_for(updates) if updates else None)


def count(repo,table):
    with repo.pool.connection() as c:
        return c.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']


def test_preview_and_enqueue_do_not_spend_quota_and_manual_runs_when_disabled(repo):
    source(repo)
    epoch=repo.epoch()
    before=count(repo,'model_calls')
    status=repo.reflection_settings()
    assert not status['enabled'] and not status['eligible'] and status['manual_eligible']
    assert status['quota']['rounds_used']==status['quota']['calls_used']==0
    run=repo.enqueue_reflection(**prepare(repo))
    assert run['status']=='pending'
    assert repo.reflection_settings()['quota']['rounds_used']==0
    assert repo.epoch()==epoch and count(repo,'model_calls')==before
    job=repo.claim_reflection(run['id'])
    assert job['status']=='running' and job['lease_until']-job['started_at']==timedelta(seconds=300)
    assert repo.reflection_settings()['quota']['rounds_used']==1


def test_success_creates_only_versioned_self_with_all_exposed_dependencies(repo):
    first=source(repo)
    other=source(repo,'另一段合成背景说明行动的意义。')
    before={table:count(repo,table) for table in ('experiences','episodes','messages','memories','episode_rehearsals')}
    arguments=prepare(repo)
    run=repo.enqueue_reflection(**arguments)
    job=repo.claim_reflection(run['id'])
    calls(repo,job)
    epoch=repo.epoch()
    completed=save(repo,job,[update(first)])
    assert completed['status']=='completed' and completed['cloud_calls']==2
    assert completed['usage']=={'input_tokens':40,'output_tokens':20}
    assert repo.epoch()==epoch+1
    assert {table:count(repo,table) for table in before}==before
    state=repo.reflection_states()[0]
    assert state['generation_type']=='reflection' and state['revision']==1
    assert state['evidence_manifest'][0]['experience_id']==str(first['source_experience_ids'][0])
    assert save(repo,job,[update(first)])['id']==completed['id']
    assert count(repo,'self_observations')==1 and not repo.reflection_candidates()
    # Idempotency is checked before changed epochs or exhausted pending material.
    assert repo.enqueue_reflection(**arguments)['id']==run['id']
    repo.forget_experience(other['source_experience_ids'][0])
    assert repo.self_state()==[]
    with repo.pool.connection() as c:
        erased=c.execute('SELECT * FROM self_observations WHERE id=%s',(state['id'],)).fetchone()
    assert erased['text']==erased['reason']==erased['evidence_quote']=='' and erased['evidence_manifest']==[]
    assert repo.reflection_run(run['id'])['invalidated_at'] is not None
    assert repo.episode(first['id'])['deleted_at'] is None


def test_empty_proposal_completes_once_without_self_or_epoch_change(repo):
    source(repo)
    job=start(repo)
    calls(repo,job,proposed=0)
    epoch=repo.epoch()
    result=save(repo,job,[])
    assert result['result_ids']==[] and result['cloud_calls']==1
    assert repo.epoch()==epoch and not repo.reflection_candidates()
    assert count(repo,'self_observations')==0


def test_two_stages_required_and_multi_update_failure_is_atomic(repo):
    event=source(repo)
    job=start(repo)
    cid=repo.start_reflection_call(job,'reflection_proposal')
    repo.finish_call(cid,'reflection_proposal_completed')
    with pytest.raises(MemoryError,match='reflection_review_missing'):
        save(repo,job,[update(event)])
    cid=repo.start_reflection_call(job,'reflection_review')
    repo.finish_call(cid,'reflection_review_completed')
    with pytest.raises(MemoryError,match='reflection_target_changed'):
        save(repo,job,[update(event),update(event,key='无效版本',expected_revision=8)])
    assert count(repo,'self_observations')==count(repo,'reflection_processed_episodes')==0
    assert repo.reflection_run(job['id'])['status']=='running'


def test_failed_material_can_retry_but_real_day_quota_survives_logical_time_advance(repo):
    source(repo)
    repo.clock=MutableClock(datetime(2040,1,1,tzinfo=timezone.utc))
    for _ in range(2):
        job=start(repo)
        calls(repo,job)
        repo.fail_reflection(job,'synthetic_failure')
        repo.clock.advance(days=365)
    state=repo.reflection_settings()
    assert state['pending_count']==1 and state['quota']['rounds_used']==2 and state['quota']['calls_used']==4
    assert state['quota']['day'].year!=repo.clock.now().year
    with pytest.raises(MemoryError,match='reflection_not_eligible'):
        repo.enqueue_reflection(**prepare(repo))
    assert count(repo,'self_observations')==0


def test_only_real_shanghai_midnight_reopens_quota(repo,monkeypatch):
    source(repo)
    at=[datetime(2026,10,26,15,59,tzinfo=timezone.utc)]
    monkeypatch.setattr(repo,'_reflection_wall_time',lambda c:at[0])
    for _ in range(2):
        job=start(repo)
        calls(repo,job)
        repo.fail_reflection(job,'synthetic_failure')
    assert repo.reflection_settings()['quota']['rounds_used']==2
    at[0]+=timedelta(minutes=2)
    assert repo.reflection_settings()['quota']['rounds_used']==0
    assert start(repo)['quota_day'].isoformat()=='2026-10-27'


def test_concurrent_claims_reserve_one_round_and_old_token_cannot_commit(repo):
    event=source(repo)
    run=repo.enqueue_reflection(**prepare(repo))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:repo.claim_reflection(run['id']),range(2)))
    job=next(row for row in results if row)
    assert sum(row is not None for row in results)==1
    assert repo.reflection_settings()['quota']['rounds_used']==1
    calls(repo,job)
    old={**job,'lease_token':uuid4()}
    with pytest.raises(MemoryError,match='reflection_lease_lost'):
        save(repo,old,[update(event)])
    assert count(repo,'self_observations')==0


@pytest.mark.parametrize('when',['queued','running'])
def test_source_delete_cancels_work_and_late_response_cannot_resurrect(repo,when):
    event=source(repo)
    run=repo.enqueue_reflection(**prepare(repo))
    job=repo.claim_reflection(run['id']) if when=='running' else None
    if job:calls(repo,job)
    repo.forget_experience(event['source_experience_ids'][0])
    assert repo.reflection_run(run['id'])['status']=='cancelled'
    if job:
        with pytest.raises(MemoryError,match='reflection_lease_lost'):
            save(repo,job,[update(event)])
    assert repo.claim_reflection(run['id']) is None
    assert count(repo,'self_observations')==count(repo,'reflection_processed_episodes')==0


def test_epoch_change_cancels_queued_run_without_leaving_busy_slot(repo):
    source(repo)
    run=repo.enqueue_reflection(**prepare(repo))
    fact=repo.remember({'text':'合成新事实'})[0]
    repo.correct(fact['id'],fact['revision'],{'text':'合成更正事实'})
    assert repo.claim_reflection(run['id']) is None
    assert repo.reflection_run(run['id'])['status']=='cancelled'
    assert repo.reflection_settings()['manual_eligible']


def test_source_hash_mismatch_is_rejected_even_without_epoch_change(repo):
    event=source(repo)
    args=prepare(repo)
    args['manifest'][0]['source_hash']='0'*64
    with pytest.raises(MemoryError,match='reflection_source_changed'):
        repo.enqueue_reflection(**args)
    assert count(repo,'reflection_runs')==0
    job=start(repo)
    calls(repo,job)
    with repo.pool.connection() as c:
        c.execute('UPDATE episodes SET source_hash=%s WHERE id=%s',('1'*64,event['id']))
    with pytest.raises(MemoryError,match='reflection_source_changed'):
        save(repo,job,[update(event)])
    assert count(repo,'self_observations')==0


def test_deleted_self_key_stays_blocked_until_a_new_chat_version(repo):
    item=SelfUpdate(domain='belief',key='承诺',text='合成认识',reason='合成原因',quote='承诺',confidence=.7)
    event=source(repo,self_updates=[item])
    deleted=repo.self_state()[0]
    repo.forget_observation(deleted['id'])
    job=start(repo)
    calls(repo,job)
    with pytest.raises(MemoryError,match='reflection_key_deleted'):
        save(repo,job,[update(event)])
    repo.fail_reflection(job,'reflection_key_deleted')
    event=source(repo,'新的合成对话重新讨论承诺。',self_updates=[item])
    state=repo.self_state()[0]
    assert state['revision']==2 and not state['deleted_at']
    job=start(repo,states=[state])
    calls(repo,job)
    save(repo,job,[update(event,target_id=state['id'],expected_revision=2)])
    assert repo.self_state()[0]['revision']==3


def test_reflected_emotion_has_new_cause_and_version_without_reusing_old_intensity(repo):
    item=SelfUpdate(domain='emotion',key='合成关注',text='关注故事中的承诺',reason='合成初始原因',
                    quote='承诺',confidence=.7,intensity=.8,half_life_hours=1)
    event=source(repo,self_updates=[item])
    state=repo.self_state()[0]
    job=start(repo,states=[state])
    calls(repo,job)
    result=save(repo,job,[update(event,key='合成关注',domain='emotion',
        target_id=state['id'],expected_revision=state['revision'],intensity=.2,half_life_hours=2)])
    current=repo.self_state()[0]
    assert result['result_ids'] and current['revision']==2 and current['intensity']==.2
    history=repo.self_history()
    assert next(row for row in history if row['revision']==1)['intensity']==.8


def test_restore_cancels_leases_and_disables_schedule_without_refunding_quota(repo):
    source(repo)
    repo.set_reflection_enabled(True)
    job=start(repo)
    cid=repo.start_reflection_call(job,'reflection_proposal')
    repo.finish_call(cid,'reflection_proposal_completed',{'input_tokens':10})
    assert repo.reset_reflections_after_restore()==1
    state=repo.reflection_settings()
    assert not state['enabled'] and state['quota']['rounds_used']==state['quota']['calls_used']==1
    run=repo.reflection_run(job['id'])
    assert run['status']=='cancelled' and run['lease_token'] is None and run['lease_until'] is None
    assert run['usage']=={'input_tokens':10,'output_tokens':None}


def test_business_clock_moves_schedule_but_not_usage_or_epoch(repo):
    repo.clock=MutableClock(datetime(2040,1,1,0,tzinfo=timezone.utc))
    source(repo)
    repo.set_reflection_enabled(True)
    epoch=repo.epoch()
    repo.record_chat_activity()
    status=repo.reflection_settings()
    assert 'chat_not_idle' in status['reasons'] and status['next_due_at']==repo.clock.now()+timedelta(minutes=15)
    repo.clock.advance(seconds=900)
    assert repo.reflection_settings()['eligible']
    assert repo.epoch()==epoch
    job=start(repo,automatic=True)
    calls(repo,job,proposed=0)
    save(repo,job,[])
    source(repo,'第二条合成新材料。')
    assert 'minimum_spacing' in repo.reflection_settings()['reasons']
    repo.clock.advance(days=1)
    assert repo.reflection_settings()['eligible']
    assert repo.reflection_settings()['quota']['rounds_used']==1


def test_migration_six_is_additive_and_defaults_schedule_off(repo):
    repo.clock=MutableClock(datetime(2040,1,2,4,5,tzinfo=timezone.utc))
    original='合成故事发生在 2039年12月30日。承诺需要后续行动。'
    description=ExperienceDescription(title='迁移合成事件',summary='非空原文迁移验证',context_type='fiction',
        participants=['合成人物'],time_quote='2039年12月30日',anchors=[{'quote':'承诺','meaning':'合成线索'}])
    event=source(repo,original,experience_description=description)
    archived=repo.experience(event['source_experience_ids'][0])
    assert archived['user_text']==original and archived['occurred_at'] is not None
    with repo.pool.connection() as c:
        drop_reflection_schema(c)
        assert c.execute('SELECT max(version) AS version FROM schema_migrations').fetchone()['version']==5
    repo.migrate();repo.migrate()
    assert repo.experience(archived['id'])==archived
    assert repo.reflection_candidates()[0]['id']==event['id']
    assert not repo.reflection_settings()['enabled']
    assert count(repo,'reflection_runs')==count(repo,'model_calls')==0


def test_production_cannot_enable_mock_quota_bypass(repo):
    source(repo)
    with pytest.raises(ValueError,match='invalid_reflection_model_mode'):
        repo.enqueue_reflection(**prepare(repo),model_mode='mock')


def test_invalidated_target_repair_uses_new_evidence_but_tracks_exposed_metadata(repo):
    fact=repo.remember({'text':'合成旧解释'})[0]
    item=SelfUpdate(domain='belief',key='承诺',text='合成旧认识',reason='旧解释',quote='承诺',confidence=.7)
    source(repo,self_updates=[item],context_roots=[('memory',fact['id'])])
    previous=repo.self_state()[0]
    repo.correct(fact['id'],1,{'text':'合成解释已纠正'})
    assert repo.reflection_states()==[]
    targets=repo.reflection_targets()
    assert targets==[{'id':previous['id'],'domain':'belief','key':'承诺','revision':1}]
    event=source(repo,'新的独立合成证据说明承诺应结合行动。')
    arguments=prepare(repo,target_versions=targets)
    assert not any(row['source_type']=='self' for row in arguments['manifest'])
    job=repo.claim_reflection(repo.enqueue_reflection(**arguments)['id'])
    calls(repo,job)
    save(repo,job,[update(event,target_id=previous['id'],expected_revision=1)])
    current=repo.self_state()[0]
    assert current['revision']==2 and current['invalidated_at'] is None
    assert current['evidence_quote'] in '新的独立合成证据说明承诺应结合行动。'
    with repo.pool.connection() as c:
        assert c.execute('''SELECT 1 FROM context_links WHERE source_type='self'
            AND source_id=%s AND target_type='self' AND target_id=%s''',(previous['id'],current['id'])).fetchone()
        exposed=c.execute('SELECT source_refs FROM model_calls WHERE reflection_run_id=%s LIMIT 1',(job['id'],)).fetchone()['source_refs']
        assert any(ref.get('metadata_only') and ref['id']==str(previous['id']) for ref in exposed)
    # The old body is never evidence, while the exposed key still carries a
    # privacy dependency back to the source that introduced that metadata.
    repo.forget(fact['id'])
    assert repo.self_state()==[]
    assert repo.reflection_run(job['id'])['invalidated_at'] is not None
    assert repo.reflection_run(job['id'])['target_versions']==[]


@pytest.mark.parametrize('boundary',['enqueue','claim','call','save'])
def test_all_frozen_targets_are_fenced_even_when_they_are_not_updated(repo,boundary):
    item=SelfUpdate(domain='goal',key='不修改的目标',text='合成目标',reason='合成原因',quote='承诺',confidence=.7)
    event=source(repo,self_updates=[item])
    targets=repo.reflection_targets()
    arguments=prepare(repo,target_versions=targets)
    run=repo.enqueue_reflection(**arguments) if boundary!='enqueue' else None
    job=repo.claim_reflection(run['id']) if boundary in ('call','save') else None
    if boundary=='save':calls(repo,job)
    # Deliberately keep epoch unchanged to isolate the target revision fence.
    with repo.pool.connection() as c:
        c.execute('UPDATE self_observations SET revision=revision+1 WHERE id=%s',(targets[0]['id'],))
    if boundary=='enqueue':
        with pytest.raises(MemoryError,match='reflection_target_changed'):repo.enqueue_reflection(**arguments)
    elif boundary=='claim':
        assert repo.claim_reflection(run['id']) is None
        assert repo.reflection_run(run['id'])['status']=='cancelled'
    elif boundary=='call':
        with pytest.raises(MemoryError,match='reflection_target_changed'):
            repo.start_reflection_call(job,'reflection_proposal')
        assert count(repo,'model_calls')==0
    else:
        with pytest.raises(MemoryError,match='reflection_target_changed'):
            save(repo,job,[update(event)])
    assert count(repo,'self_observations')==1 and count(repo,'reflection_processed_episodes')==0


def test_revoked_cloud_ancestor_hides_target_key_and_rejects_frozen_metadata(repo):
    fact=repo.remember({'text':'合成私有背景'})[0]
    item=SelfUpdate(domain='belief',key='合成私密目标键',text='合成认识',reason='合成原因',quote='承诺',confidence=.7)
    source(repo,self_updates=[item],context_roots=[('memory',fact['id'])])
    targets=repo.reflection_targets()
    source(repo,'新的独立合成材料。')
    repo.correct(fact['id'],1,{'allow_cloud':False})
    assert repo.reflection_targets()==repo.reflection_states()==[]
    with pytest.raises(MemoryError,match='reflection_source_changed'):
        repo.enqueue_reflection(**prepare(repo,target_versions=targets))
    assert count(repo,'model_calls')==count(repo,'reflection_runs')==0


def test_recursive_target_permission_is_rechecked_before_cloud_call(repo):
    fact=repo.remember({'text':'合成背景'})[0]
    item=SelfUpdate(domain='belief',key='合成目标键',text='合成认识',reason='合成原因',quote='承诺',confidence=.7)
    source(repo,self_updates=[item],context_roots=[('memory',fact['id'])])
    event=source(repo,'独立的新合成材料。')
    job=start(repo,events=[event],target_versions=repo.reflection_targets())
    # Keep epoch and descendants intact to specifically test recursive
    # permission validation of metadata that is not in source_manifest.
    with repo.pool.connection() as c:
        c.execute('UPDATE memories SET allow_cloud=false WHERE id=%s',(fact['id'],))
    assert repo.reflection_targets()==[]
    with pytest.raises(MemoryError,match='reflection_source_changed'):
        repo.start_reflection_call(job,'reflection_proposal')
    assert count(repo,'model_calls')==0


def test_target_only_metadata_revocation_cancels_run_and_interrupts_call(repo):
    fact=repo.remember({'text':'合成背景'})[0]
    item=SelfUpdate(domain='belief',key='合成目标键',text='合成认识',reason='合成原因',quote='承诺',confidence=.7)
    source(repo,self_updates=[item],context_roots=[('memory',fact['id'])])
    event=source(repo,'独立的新合成材料。')
    job=start(repo,events=[event],target_versions=repo.reflection_targets())
    repo.start_reflection_call(job,'reflection_proposal')
    repo.correct(fact['id'],1,{'allow_cloud':False})
    run=repo.reflection_run(job['id'])
    assert run['status']=='cancelled' and run['invalidated_at']
    assert run['calls'][0]['status']=='interrupted'


def test_any_rejected_review_prevents_every_write_and_consumption(repo):
    event=source(repo)
    job=start(repo)
    calls(repo,job,proposed=2)
    job['audit'].update(accepted=1,rejected=1)
    epoch=repo.epoch()
    with pytest.raises(MemoryError,match='reflection_review_rejected'):
        save(repo,job,[update(event)])
    assert repo.epoch()==epoch
    assert count(repo,'self_observations')==count(repo,'reflection_processed_episodes')==0
    assert repo.reflection_candidates()[0]['id']==event['id']


def test_independent_deletion_journal_scrubs_reflection_restored_from_old_snapshot(repo):
    event=source(repo)
    original=repo.experience(event['source_experience_ids'][0])
    job=start(repo)
    calls(repo,job)
    save(repo,job,[update(event)])
    state=repo.self_state()[0]
    repo.forget_experience(original['id'])
    # Reintroduce the source and derived rows as an older database snapshot
    # would, while preserving the independent deletion journal on disk.
    with repo.pool.connection() as c:
        c.execute('''UPDATE experiences SET user_text=%s,assistant_text=%s,source_hash=%s,
            deleted_at=NULL WHERE id=%s''',
            (original['user_text'],original['assistant_text'],original['source_hash'],original['id']))
        c.execute('''UPDATE episodes SET title=%s,gist=%s,source_hash=%s,deleted_at=NULL WHERE id=%s''',
            (event['title'],event['gist'],event['source_hash'],event['id']))
        c.execute('''UPDATE self_observations SET text=%s,reason=%s,evidence_quote=%s,evidence_manifest=%s,
            deleted_at=NULL,invalidated_at=NULL WHERE id=%s''',
            (state['text'],state['reason'],state['evidence_quote'],Jsonb(state['evidence_manifest']),state['id']))
        c.execute('UPDATE reflection_runs SET invalidated_at=NULL WHERE id=%s',(job['id'],))
    repo.replay_deletions(required=True)
    repo.reset_reflections_after_restore()
    assert repo.self_state()==repo.reflection_targets()==repo.reflection_candidates()==[]
    assert repo.backfill_episodes()==0
    with repo.pool.connection() as c:
        erased=c.execute('SELECT * FROM self_observations WHERE id=%s',(state['id'],)).fetchone()
        assert erased['text']==erased['reason']==erased['evidence_quote']==''
        assert erased['evidence_manifest']==[] and erased['deleted_at']
        assert c.execute('SELECT user_text FROM experiences WHERE id=%s',(original['id'],)).fetchone()['user_text']==''
    assert repo.reflection_run(job['id'])['invalidated_at'] is not None
    assert repo.reflection_settings()['quota']['calls_used']==2


def test_failure_persists_only_safe_counts_and_local_trigger_reasons(repo):
    source(repo)
    job=start(repo)
    assert 'manual' in job['trigger_reasons']
    job['audit']={'proposed':2,'accepted':1,'rejected':1,'calls':2,
                  'text':'不能保留的合成推理','prompt':'不能保留的合成提示','body':{'secret':'synthetic'}}
    failed=repo.fail_reflection(job,'reflection_review_rejected')
    assert failed['audit']=={'proposed':2,'accepted':1,'rejected':1,'calls':2}
    assert failed['trigger_reasons']==job['trigger_reasons']


def test_reflection_run_listing_honors_limits_above_one_hundred(repo):
    with repo.pool.connection() as c:
        with c.cursor() as cursor:
            cursor.executemany('''INSERT INTO reflection_runs(id,status,input_signature,policy_version,
                expected_epoch,business_at) VALUES(%s,'failed',%s,'test-v1',0,%s)''',
                [(uuid4(),content_hash(index),repo.clock.now()) for index in range(120)])
    assert len(repo.reflection_runs(limit=200))==120
    assert len(repo.reflection_runs(limit=110,offset=10))==110


@pytest.mark.parametrize('stop',['failure','restore','delete','disabled','expiry'])
def test_interrupted_calls_accept_late_usage_without_reopening_status(repo,stop):
    event=source(repo)
    repo.clock=MutableClock(datetime(2040,1,1,0,tzinfo=timezone.utc))
    repo.set_reflection_enabled(True)
    job=start(repo,automatic=True)
    cid=repo.start_reflection_call(job,'reflection_proposal')
    if stop=='failure':repo.fail_reflection(job,'synthetic_failure')
    elif stop=='restore':repo.reset_reflections_after_restore()
    elif stop=='delete':repo.forget_experience(event['source_experience_ids'][0])
    elif stop=='disabled':repo.set_reflection_enabled(False)
    else:
        with repo.pool.connection() as c:
            c.execute("UPDATE reflection_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job['id'],))
        assert repo.claim_reflection(job['id']) is None
    result=repo.reflection_run(job['id'])
    assert result['status'] in ('failed','cancelled')
    assert result['calls'][0]['status']=='interrupted'
    assert result['usage']=={'input_tokens':None,'output_tokens':None}
    repo.finish_call(cid,'reflection_proposal_completed',{'input_tokens':23,'output_tokens':7})
    repo.finish_call(cid,'reflection_model_failed')
    updated=repo.reflection_run(job['id'])
    assert updated['status']==result['status'] and updated['calls'][0]['status']=='interrupted'
    assert updated['usage']=={'input_tokens':23,'output_tokens':7}
    job['audit']={'proposed':1,'accepted':0,'rejected':0,'calls':1}
    repo.fail_reflection(job,'late_result')
    assert repo.reflection_run(job['id'])['audit']==job['audit']
    assert count(repo,'self_observations')==count(repo,'reflection_processed_episodes')==0


@pytest.mark.parametrize('invalid',['missing','omitted_unit','unknown_evidence','accept_conflict','rejected','audit'])
def test_direct_commit_requires_consistent_complete_sentence_review(repo,invalid):
    event=source(repo)
    job=start(repo)
    calls(repo,job)
    proposal=update(event,text='这是一项合成理解。其适用范围仍然有限。')
    review=review_for([proposal])
    expected='reflection_review_invalid'
    if invalid=='missing':review=None
    elif invalid=='omitted_unit':review['reviews'][0]['checks'].pop()
    elif invalid=='unknown_evidence':review['reviews'][0]['checks'][0]['evidence_ids']=[str(uuid4())]
    elif invalid=='accept_conflict':review['reviews'][0]['checks'][0]['support']='conflict'
    elif invalid=='rejected':
        review['reviews'][0]['checks'][0]['support']='unsupported'
        review['reviews'][0]['accept']=False
        expected='reflection_review_rejected'
    else:
        job['audit']['proposed']=2
        expected='reflection_audit_mismatch'
    epoch=repo.epoch()
    with pytest.raises(MemoryError,match=expected):
        repo.save_reflection(job,[proposal],review=review)
    assert count(repo,'self_observations')==count(repo,'reflection_processed_episodes')==0
    assert repo.epoch()==epoch and repo.reflection_candidates()


@pytest.mark.parametrize('direction',[None,'decrease','maintain','increase'])
def test_direct_new_emotion_commit_requires_new_direction(repo,direction):
    event=source(repo)
    job=start(repo)
    calls(repo,job)
    proposal=update(event,domain='emotion',intensity=.3,half_life_hours=2,intensity_direction=direction)
    with pytest.raises(MemoryError,match='reflection_proposal_invalid'):
        save(repo,job,[proposal])
    assert count(repo,'self_observations')==count(repo,'reflection_processed_episodes')==0


def test_emotion_commit_direction_uses_frozen_displayed_intensity_not_commit_time(repo):
    repo.clock=MutableClock(datetime(2040,1,1,tzinfo=timezone.utc))
    item=SelfUpdate(domain='emotion',key='担忧',text='合成初始担忧',reason='初始证据',quote='承诺',
                    confidence=.7,intensity=.8,half_life_hours=1)
    source(repo,self_updates=[item])
    repo.clock.advance(hours=1)
    event=source(repo,'新的合成证据缓解了这项担忧。')
    old=repo.self_state()[0]
    assert old['current_intensity']==.4
    job=start(repo,states=[old])
    calls(repo,job)
    repo.clock.advance(hours=1)
    assert repo.self_state()[0]['current_intensity']==.2
    proposal=update(event,key='担忧',domain='emotion',target_id=old['id'],expected_revision=1,
                    intensity=.3,half_life_hours=2,intensity_direction='decrease')
    completed=save(repo,job,[proposal])
    assert completed['status']=='completed'
    current=repo.self_state()[0]
    assert current['revision']==2 and current['intensity']==.3
    assert next(row for row in repo.self_history() if row['revision']==1)['intensity']==.8


@pytest.mark.parametrize('direction',['decrease','maintain','new'])
def test_emotion_commit_rejects_direction_opposite_frozen_current_intensity(repo,direction):
    repo.clock=MutableClock(datetime(2040,1,1,tzinfo=timezone.utc))
    item=SelfUpdate(domain='emotion',key='担忧',text='合成初始担忧',reason='初始证据',quote='承诺',
                    confidence=.7,intensity=.35,half_life_hours=1)
    event=source(repo,self_updates=[item])
    repo.clock.advance(hours=1)
    previous=repo.self_state()[0]
    assert previous['current_intensity']==.175
    job=start(repo,states=[previous])
    calls(repo,job)
    proposal=update(event,key='担忧',domain='emotion',target_id=previous['id'],expected_revision=1,
                    intensity=.22,half_life_hours=2,intensity_direction=direction)
    with pytest.raises(MemoryError,match='reflection_proposal_invalid'):
        save(repo,job,[proposal])
    assert count(repo,'self_observations')==1 and count(repo,'reflection_processed_episodes')==0


def test_invalidated_emotion_metadata_cannot_supply_unseen_current_intensity(repo):
    fact=repo.remember({'text':'合成旧原因'})[0]
    item=SelfUpdate(domain='emotion',key='担忧',text='合成旧担忧',reason='旧原因',quote='承诺',
                    confidence=.7,intensity=.8,half_life_hours=1)
    source(repo,self_updates=[item],context_roots=[('memory',fact['id'])])
    previous=repo.self_state()[0]
    repo.correct(fact['id'],1,{'text':'合成原因已纠正'})
    event=source(repo,'新的合成材料。')
    assert repo.reflection_states()==[]
    job=start(repo,target_versions=repo.reflection_targets())
    calls(repo,job)
    proposal=update(event,key='担忧',domain='emotion',target_id=previous['id'],expected_revision=1,
                    intensity=.2,half_life_hours=2,intensity_direction='decrease')
    with pytest.raises(MemoryError,match='reflection_proposal_invalid'):
        save(repo,job,[proposal])
    assert count(repo,'self_observations')==1 and count(repo,'reflection_processed_episodes')==0


def test_restore_quota_merges_live_high_water_marks_without_logical_clock_reset(repo):
    source(repo)
    job=start(repo)
    calls(repo,job)
    repo.fail_reflection(job,'synthetic_failure')
    live=export_reflection_quota(repo)
    assert live['days'][0]['rounds']==1 and live['days'][0]['calls']==2
    day=live['days'][0]['day']
    with repo.pool.connection() as c:
        c.execute('UPDATE reflection_daily_usage SET rounds=2,calls=0 WHERE day=%s',(day,))
        c.execute("INSERT INTO reflection_daily_usage(day,rounds,calls) VALUES('2020-01-01',2,4)")
    epoch=repo.epoch()
    repo.clock=MutableClock(datetime(2040,1,1,tzinfo=timezone.utc))
    merge_reflection_quota(repo,live)
    merge_reflection_quota(repo,live)
    merged=export_reflection_quota(repo)
    assert next(row for row in merged['days'] if row['day']==day)=={'day':day,'rounds':2,'calls':2}
    assert {'day':'2020-01-01','rounds':2,'calls':4} in merged['days']
    assert repo.epoch()==epoch


def test_schema_five_quota_export_is_explicitly_empty_then_merges_after_migration(repo):
    source(repo)
    with repo.pool.connection() as c:drop_reflection_schema(c)
    payload=export_reflection_quota(repo)
    assert payload=={'version':1,'source_schema':5,'days':[]}
    repo.migrate()
    merge_reflection_quota(repo,payload)
    assert repo.reflection_settings()['quota']['rounds_used']==0
    assert not repo.reflection_settings()['enabled']


@pytest.mark.parametrize('bad',[{'day':'2026-01-01','rounds':-1,'calls':2},
    {'day':'2026-01-01','rounds':1,'calls':True}, {'day':'2026-01-01','rounds':1,'calls':2,'text':'不允许正文'}])
def test_invalid_quota_merge_is_atomic_and_does_not_fabricate_usage(repo,bad):
    payload={'version':1,'source_schema':6,'days':[{'day':'2025-01-01','rounds':2,'calls':4},bad]}
    with pytest.raises(ValueError,match='invalid_quota_high_water_mark'):merge_reflection_quota(repo,payload)
    assert count(repo,'reflection_daily_usage')==0


def test_missing_schema_six_quota_table_is_not_treated_as_unused_quota(repo):
    with repo.pool.connection() as c:c.execute('DROP TABLE reflection_daily_usage')
    with pytest.raises(Exception):export_reflection_quota(repo)
