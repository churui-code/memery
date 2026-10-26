from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from psycopg import sql
from app.clock import MutableClock
from app.automatic import validate_decision, validate_reassessment
from app.episodes import EpisodeProposal, locate_quote, source_hash, validate_episode_proposals
from app.errors import MemoryError
from app.life import SelfUpdate, ExperienceDescription


def proposal(quote='紫藤戒指', **changes):
    data={'title':'合成公园经历','gist':'描述紫藤戒指和承诺','quote':quote,'context_type':'reported',
          'importance':0.9,'importance_reason':'明确的承诺','emotional_salience':0.8,
          'emotional_reason':'对话描述的重要时刻',
          'details':[{'quote':quote,'kind':'object','meaning':'关键物件','importance':0.9,'emotional_salience':0.8}],
          'cues':[{'quote':quote,'text':quote,'kind':'object'}]}
    data.update(changes)
    return EpisodeProposal.model_validate(data)


def save(repo, text='我拿出了紫藤戒指。', proposals=None, roots=None, **kwargs):
    return repo.save_answer(uuid4(),text,'合成回复',[],repo.epoch(),episode_proposals=proposals,
                            context_roots=roots,**kwargs)


@pytest.mark.parametrize('legacy', [False,True])
def test_anchor_refs_preserve_second_occurrence_in_fallback_and_legacy(repo,legacy):
    text='我爱你。我们散步。我爱你。'
    raw={'experience':{'title':'合成重复引文','summary':'只标记后一次话语',
        'anchors':{'item':{'quote_refs':{'item':'u0003'},'meaning':'第二次说出的话'}}},'episodes':[]}
    decision=validate_decision(raw,text,[])
    assert decision.experience.anchors[0].occurrence==1
    if legacy:
        with repo.pool.connection() as c:
            experience_id=repo._archive_turn(c,uuid4(),text,'合成回复',decision.experience,[])
        event=repo.episode(experience_id)
        assert event['legacy_projection']
    else:
        result=save(repo,text,experience_description=decision.experience,proposals=decision.episodes)
        experience_id=result['experience_id']
        event=repo.episode(result['episode_ids'][0])
        assert event['local_fallback']
    assert repo.experience(experience_id)['anchors'][0]['occurrence']==1
    speech=[detail for detail in event['details'] if detail['kind']=='speech']
    assert len(speech)==1 and len(event['cues'])==1
    for item in speech+event['cues']:
        assert item['quote']=='我爱你。'
        assert item['start']==text.rfind('我爱你。') and item['end']==len(text)
        assert item['experience_id']==experience_id


@pytest.mark.parametrize('occurrence', [-1,2,True,'1'])
@pytest.mark.parametrize('reassessment', [False,True])
def test_anchor_occurrence_requires_valid_current_input_position(occurrence,reassessment):
    raw={'experience':{'title':'合成重复引文','summary':'无效位置应拒绝',
        'anchors':[{'quote':'我爱你。','meaning':'合成说明','occurrence':occurrence}]}}
    with pytest.raises(MemoryError):
        if reassessment:
            validate_reassessment(raw,'我爱你。我爱你。')
        else:
            validate_decision(raw,'我爱你。我爱你。',[])


def test_multiple_events_preserve_exact_duplicate_quote_positions(repo):
    text='先说我爱你，后来又说我爱你。'
    p=proposal('我爱你',occurrence=1,details=[{'quote':'我爱你','occurrence':1,'kind':'speech'}],
               cues=[{'quote':'我爱你','occurrence':1,'kind':'phrase','text':'我爱你'}])
    result=save(repo,text,[p,proposal('先说',details=[],cues=[])])
    assert len(result['episode_ids'])==2
    episode=repo.episode(result['episode_ids'][0])
    detail=episode['details'][0]
    assert detail['start']==text.rfind('我爱你')
    assert text[detail['start']:detail['end']]==detail['quote']
    assert detail['source_hash']==source_hash(text,'合成回复')
    assert detail['source_revision']==1
    assert repo.experience(result['experience_id'])['user_text']==text


def test_episode_evidence_rejects_unknown_quotes_and_targets_atomically(repo):
    with pytest.raises(ValueError):
        save(repo,proposals=[proposal('没有出现的戒指')])
    with pytest.raises(ValueError):
        save(repo,proposals=[proposal(target_id=uuid4())])
    assert repo.episodes()==[]
    assert repo.search_experiences()==[]
    with pytest.raises(ValueError):
        locate_quote('重复重复','重复',2)
    with pytest.raises(ValueError):
        validate_episode_proposals([proposal(occurred_at=datetime.now(timezone.utc),time_quote='昨日')],
                                  '我拿出了紫藤戒指。')


@pytest.mark.parametrize('field,reason', [('importance','importance_reason'),('emotional_salience','emotional_reason')])
def test_nondefault_salience_requires_an_explained_reason(field,reason):
    with pytest.raises(ValueError):
        proposal(**{field:0.8,reason:'   '})


def test_duplicate_phrase_cannot_be_attached_to_the_wrong_event_scope():
    text='第一天买了戒指。第二天买了戒指。'
    p=proposal('第二天买了戒指。',details=[{'quote':'戒指','occurrence':0,'kind':'object'}],cues=[])
    with pytest.raises(ValueError,match='outside_event_scope'):
        validate_episode_proposals([p],text)
    p.details[0].occurrence=1
    assert validate_episode_proposals([p],text)==[p]


def test_fallback_retains_full_source_and_indexes_late_keyword(repo):
    text=''.join(f'合成地点{i}。' for i in range(400))+'最后出现星河暗号。'
    result=save(repo,text)
    episode=repo.episode(result['episode_ids'][0])
    assert episode['local_fallback']
    assert any(e['quote']==text for e in episode['evidence'])
    found=repo.episode_candidates('星河暗号')
    assert any(e['id']==episode['id'] and 'keyword' in e['methods'] for e in found)


def test_association_is_not_a_deletion_dependency(repo):
    first=save(repo,proposals=[proposal()])
    second=save(repo,'另一天又看到紫藤戒指。',[proposal()])
    linked=repo.episode_associated(first['episode_ids'])
    assert [e['id'] for e in linked]==second['episode_ids']
    repo.forget_experience(first['experience_id'])
    with pytest.raises(MemoryError):repo.episode(first['episode_ids'][0])
    assert repo.episode(second['episode_ids'][0])['title']=='合成公园经历'
    assert repo.episode_associated(second['episode_ids'])==[]
    with repo.pool.connection() as c:
        for table in ('episode_evidence','episode_details','episode_cues','episode_chunks'):
            assert c.execute(f'SELECT count(*) AS n FROM {table} WHERE episode_id=%s',
                             (first['episode_ids'][0],)).fetchone()['n']==0
        old=c.execute('SELECT * FROM episode_versions WHERE episode_id=%s',(first['episode_ids'][0],)).fetchone()
        assert old['title']==old['gist']==old['source_hash']==''
    repo.replay_deletions(required=True)
    assert repo.backfill_episodes()==0


def test_episode_source_revocation_blocks_all_derived_retrieval(repo):
    fact=repo.remember({'text':'合成私人事实'})[0]
    result=repo.save_answer(uuid4(),'我拿出了紫藤戒指。','合成回复',[fact['id']],repo.epoch(),
                           episode_proposals=[proposal()])
    repo.correct(fact['id'],1,{'allow_cloud':False})
    event=repo.episode(result['episode_ids'][0])
    assert not event['allow_cloud'] and event['context_stale']
    assert repo.episode_candidates('紫藤戒指',cloud_only=True)==[]
    assert repo.episode_associated(result['episode_ids'])==[]


def test_new_revision_cannot_be_overwritten_by_old_worker(repo):
    first=save(repo,proposals=[proposal()])
    eid=first['episode_ids'][0]
    old=repo.claim_episode_job()
    second=save(repo,'后来给紫藤戒指加了刻字。',[proposal(target_id=eid)],roots=[('episode',eid)])
    assert second['episode_ids']==[eid]
    assert repo.episode(eid)['revision']==2
    vector=[1.0]+[0.0]*511
    assert repo.finish_episode_index(old,['旧内容'],[vector]) is False
    new=repo.claim_episode_job()
    assert new['revision']==2 and new['source_hash']!=old['source_hash']
    assert repo.finish_episode_index(new,['紫藤戒指'],[vector]) is True
    exact=repo.episode_candidates('紫藤戒指',vector=vector,exact=True)
    approx=repo.episode_candidates('紫藤戒指',vector=vector)
    assert exact[0]['id']==approx[0]['id']==eid
    assert 'semantic' in exact[0]['methods']


def test_event_supplement_invalidates_old_self_and_advances_epoch(repo):
    first=save(repo,proposals=[proposal()])
    eid=first['episode_ids'][0]
    state=SelfUpdate(domain='belief',key='synthetic_promise',text='合成承诺的意义来自后续行动',
                     reason='对当前故事的解释',quote='紫藤戒指',confidence=.8,source_episode_ids=[eid])
    save(repo,'回看紫藤戒指。',self_updates=[state])
    assert repo.self_state()
    epoch=repo.epoch()
    result=save(repo,'后来给紫藤戒指加了刻字。',[proposal(target_id=eid)],roots=[('episode',eid)])
    assert result['epoch']==epoch+1
    assert repo.self_state()==[]
    assert repo.self_history()[0]['invalidated_at'] is not None
    assert len(repo.episode(eid)['source_experience_ids'])==2


def test_revoked_target_cannot_be_supplemented_and_new_source_rolls_back(repo):
    fact=repo.remember({'text':'合成隐私来源'})[0]
    first=repo.save_answer(uuid4(),'我拿出了紫藤戒指。','合成回复',[fact['id']],repo.epoch(),
                          episode_proposals=[proposal()])
    eid=first['episode_ids'][0]
    repo.correct(fact['id'],1,{'allow_cloud':False})
    before=len(repo.search_experiences(include_stale=True))
    with pytest.raises(MemoryError,match='context_changed'):
        save(repo,'后来给紫藤戒指加了刻字。',[proposal(target_id=eid)],roots=[('episode',eid)])
    assert len(repo.search_experiences(include_stale=True))==before
    assert repo.episode(eid)['revision']==1


def test_late_episode_worker_cannot_resurrect_erased_evidence(repo):
    result=save(repo,proposals=[proposal()])
    job=repo.claim_episode_job()
    repo.forget_experience(result['experience_id'])
    assert repo.finish_episode_index(job,['紫藤戒指'],[[1.0]+[0.0]*511]) is False
    assert repo.episode_candidates('紫藤戒指')==[]


def test_deletion_replay_erases_restored_event_representations(repo):
    result=save(repo,proposals=[proposal()])
    eid=result['episode_ids'][0]
    repo.forget_experience(result['experience_id'])
    with repo.pool.connection() as c:
        c.execute("UPDATE episodes SET title='旧备份紫藤戒指',gist='旧备份内容',deleted_at=NULL WHERE id=%s",(eid,))
        c.execute("UPDATE episode_versions SET gist='旧备份摘要' WHERE episode_id=%s",(eid,))
        c.execute("UPDATE experiences SET user_text='旧备份紫藤戒指',deleted_at=NULL WHERE id=%s",(result['experience_id'],))
    repo.replay_deletions(required=True)
    with repo.pool.connection() as c:
        assert c.execute('SELECT gist FROM episode_versions WHERE episode_id=%s',(eid,)).fetchone()['gist']==''
    assert repo.episode_candidates('紫藤戒指')==[]
    assert repo.backfill_episodes()==0


def test_business_clock_controls_sources_facts_self_and_retention(repo):
    clock=MutableClock(datetime(2040,1,1,tzinfo=timezone.utc))
    repo.clock=clock
    fact=repo.remember({'text':'合成限时事实','valid_until':clock.now()+timedelta(hours=2)})[0]
    update=SelfUpdate(domain='emotion',key='synthetic_joy',text='对合成故事感到欣喜',reason='故事中的承诺',
                      quote='紫藤戒指',confidence=0.9,intensity=0.8,half_life_hours=1)
    result=save(repo,proposals=[proposal()],self_updates=[update])
    assert fact['created_at']==clock.now()
    assert repo.episode(result['episode_ids'][0])['recorded_at']==clock.now()
    assert repo.experience(result['experience_id'])['recorded_at']==clock.now()
    clock.advance(hours=1)
    assert repo.self_state()[0]['current_intensity']==0.4
    assert repo.search('限时事实')
    clock.advance(days=40)
    assert repo.search('限时事实')==[]
    repo.expire_history()
    assert repo.experience(result['experience_id'])['user_text']=='我拿出了紫藤戒指。'
    assert repo.episode(result['episode_ids'][0])['deleted_at'] is None
    with repo.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messages').fetchone()['n']==0
    with pytest.raises(ValueError):clock.advance(seconds=-1)


def test_previews_never_strengthen_and_meaningful_rehearsals_are_bounded(repo):
    repo.clock=MutableClock(datetime(2040,1,1,tzinfo=timezone.utc))
    result=save(repo,proposals=[proposal()])
    eid=result['episode_ids'][0]
    for _ in range(4):repo.episode_candidates('紫藤戒指')
    assert repo.episode(eid)['rehearsal_count']==0
    save(repo,'再谈那枚紫藤戒指。',rehearsals=[{'episode_id':eid,'kind':'discussion'}])
    save(repo,'再谈那枚紫藤戒指。',rehearsals=[{'episode_id':eid,'kind':'discussion'}])
    assert repo.episode(eid)['rehearsal_count']==1
    repo.clock.advance(hours=25)
    save(repo,'确认那枚紫藤戒指。',rehearsals=[{'episode_id':eid,'kind':'confirmation'}])
    assert repo.episode(eid)['rehearsal_count']==2


def test_local_backfill_is_restartable_without_model(repo):
    with repo.pool.connection() as c:
        eid=repo._archive_turn(c,uuid4(),'迁移原文有紫藤戒指。','旧回复',None,[])
    candidate=repo.episode_candidates('紫藤戒指')[0]
    assert candidate['legacy_projection'] and candidate['source_experience_ids']==[eid]
    assert candidate['id']==eid
    assert repo.episode(eid)['legacy_projection']
    assert repo.episodes()==[]
    assert repo.backfill_episodes()==1
    assert repo.backfill_episodes()==0
    assert repo.episodes()[0]['source_experience_ids']==[eid]
    assert repo.experience(eid)['user_text']=='迁移原文有紫藤戒指。'


def test_cue_frequency_counts_visible_records_without_exposing_private_ones(repo):
    first=save(repo,proposals=[proposal()])
    second=save(repo,'又提到紫藤戒指。',[proposal()])
    found=repo.episode_candidates('紫藤戒指')
    assert all(row['matched_cues'][0]['frequency']==2 for row in found)
    repo.forget_experience(second['experience_id'])
    found=repo.episode_candidates('紫藤戒指')
    assert found[0]['id']==first['episode_ids'][0]
    assert found[0]['matched_cues'][0]['frequency']==1


def test_source_reliability_is_server_classification_not_importance(repo):
    result=save(repo,proposals=[proposal()])
    event=repo.episode(result['episode_ids'][0])
    assert event['importance']==.9
    assert event['source_reliability']=={'version':'source-reliability-v1','quote_verified':True,
                                        'speaker':'user','claim':'reported_unverified'}
    assert event['details'][0]['source_reliability']==event['source_reliability']
    assert event['details'][0]['importance_reason']=='关键物件'
    assert event['encoding_version']=='episode-encoding-v1'
    fallback=save(repo,'只是记录一段普通合成对话。')
    assert repo.episode(fallback['episode_ids'][0])['encoding_version']=='legacy-local-v1'


def test_day_precision_discards_model_invented_noon(repo):
    text='2025年10月12日，我拿出了紫藤戒指。'
    p=proposal(text,time_quote='2025年10月12日',occurred_at=datetime(2025,10,12,12,34,tzinfo=timezone.utc))
    result=save(repo,text,[p])
    event=repo.episode(result['episode_ids'][0])
    from zoneinfo import ZoneInfo
    assert event['time_precision']=='day'
    assert event['occurred_at'].astimezone(ZoneInfo('Asia/Shanghai')).hour==0
    assert event['occurred_until']-event['occurred_at']==timedelta(days=1)
    with repo.pool.connection() as c:
        version=c.execute('SELECT * FROM episode_versions WHERE episode_id=%s',(event['id'],)).fetchone()
        assert version['time_precision']=='day' and version['occurred_until']==event['occurred_until']


@pytest.mark.parametrize('text', ['多年前拿出了紫藤戒指。','我们继续讨论星河公园的合成故事。'])
def test_uncertain_or_non_temporal_phrase_cannot_create_exact_event_date(repo,text):
    p=proposal(text,time_quote=text,occurred_at=datetime(2025,10,12,12,tzinfo=timezone.utc))
    result=save(repo,text,[p])
    event=repo.episode(result['episode_ids'][0])
    assert event['time_precision']=='unknown'
    assert event['occurred_at'] is None and event['occurred_until'] is None
    assert event['time_quote']==text


def test_explicit_date_range_is_half_open_and_matches_interior_day(repo):
    from zoneinfo import ZoneInfo
    zone=ZoneInfo('Asia/Shanghai')
    text='2025年10月12日至2025年10月14日，我们保管紫藤戒指。'
    p=proposal(text,time_quote='2025年10月12日至2025年10月14日')
    result=save(repo,text,[p])
    event=repo.episode(result['episode_ids'][0])
    assert event['time_precision']=='range'
    assert event['occurred_at']==datetime(2025,10,12,tzinfo=zone)
    assert event['occurred_until']==datetime(2025,10,15,tzinfo=zone)
    found=repo.episode_candidates('紫藤戒指',after=datetime(2025,10,13,tzinfo=zone),
                                  before=datetime(2025,10,14,tzinfo=zone),time_basis='occurred_at')
    assert found[0]['id']==event['id']


def test_detail_nondefault_weights_need_their_own_explanation():
    from app.episodes import EpisodeDetail
    with pytest.raises(ValueError):EpisodeDetail(quote='戒指',importance=.9)
    with pytest.raises(ValueError):EpisodeDetail(quote='戒指',emotional_salience=.9)
    assert EpisodeDetail(quote='戒指',importance=.9,meaning='这件物品是明确承诺的见证')


@pytest.mark.parametrize('phrase,has_day', [('2025年10月12日',True),('继续讨论合成故事',False)])
def test_new_raw_archive_metadata_also_rejects_model_invented_noon(repo,phrase,has_day):
    from zoneinfo import ZoneInfo
    text=phrase+'，提到了紫藤戒指。'
    description=ExperienceDescription(title='合成日期描述',summary='测试原始归档时间来源',
        occurred_at=datetime(2025,10,12,12,tzinfo=timezone.utc),time_quote=phrase)
    result=save(repo,text,experience_description=description)
    archived=repo.experience(result['experience_id'])
    assert archived['user_text']==text and archived['time_quote']==phrase
    if has_day:
        assert archived['occurred_at'].astimezone(ZoneInfo('Asia/Shanghai')).hour==0
        assert repo.episode(result['episode_ids'][0])['time_precision']=='day'
    else:
        assert archived['occurred_at'] is None
        assert repo.episode(result['episode_ids'][0])['time_precision']=='unknown'


def test_unknown_time_in_supplement_preserves_old_time_and_its_quote(repo):
    text='2025年10月12日，我拿出了紫藤戒指。'
    first=save(repo,text,[proposal(text,time_quote='2025年10月12日')])
    eid=first['episode_ids'][0]
    original=repo.episode(eid)
    continuation='我们继续讨论紫藤戒指的合成故事。'
    supplement=proposal(continuation,target_id=eid,time_quote=continuation,
                        occurred_at=datetime(2026,10,26,12,tzinfo=timezone.utc))
    second=save(repo,continuation,[supplement],roots=[('episode',eid)])
    updated=repo.episode(eid)
    keys=('occurred_at','occurred_until','time_precision','time_quote')
    assert {key:updated[key] for key in keys}=={key:original[key] for key in keys}
    assert any(row['experience_id']==second['experience_id'] and row['quote']==continuation for row in updated['evidence'])
    with repo.pool.connection() as c:
        version=c.execute('SELECT * FROM episode_versions WHERE episode_id=%s AND revision=2',(eid,)).fetchone()
        assert {key:version[key] for key in keys}=={key:original[key] for key in keys}
    corrected='准确日期是2025年10月13日，我们再次讨论紫藤戒指。'
    save(repo,corrected,[proposal(corrected,target_id=eid,time_quote='2025年10月13日')],roots=[('episode',eid)])
    assert repo.episode(eid)['time_quote']=='2025年10月13日'
    assert repo.episode(eid)['occurred_at']==original['occurred_at']+timedelta(days=1)


def test_estimator_version_is_audited_and_v4_database_upgrade_is_additive(repo):
    call_id=repo.start_call(uuid4(),[],phase='intake',estimated_input_tokens=123,
                            estimator_version='utf8-half-v1',policy_version='episodic-activation-v1')
    with repo.pool.connection() as c:
        row=c.execute('SELECT * FROM model_calls WHERE id=%s',(call_id,)).fetchone()
        assert row['estimator_version']=='utf8-half-v1' and row['estimated_input_tokens']==123
        c.execute('ALTER TABLE model_calls DROP COLUMN estimator_version')
        c.execute('DELETE FROM schema_migrations WHERE version=5')
    repo.migrate()
    repo.migrate()
    with repo.pool.connection() as c:
        row=c.execute('SELECT * FROM model_calls WHERE id=%s',(call_id,)).fetchone()
        assert row['phase']=='intake' and row['estimator_version'] is None
        assert c.execute('SELECT max(version) AS version FROM schema_migrations').fetchone()['version']==5


def test_v3_archive_upgrade_preserves_original_content_and_times_and_backfills_locally(repo):
    user_text='  合成往事：2020年1月2日12时34分56秒，在公园说“我爱你”。\r\n随后拿出紫藤戒指。🙂 e\u0301  '
    assistant_text='合成旧回复：这段叙述有自己的时间。\n原样保存全部符号与空白。  '
    description=ExperienceDescription(title='合成旧版经历',summary='迁移前已经存在的经历轮廓',
        context_type='reported',participants=['合成人物甲','合成人物乙'],significance=.8,
        anchors=[{'quote':'紫藤戒指','meaning':'原文中的合成关键物件'}],
        time_quote='2020年1月2日12时34分56秒')
    recorded_at=datetime(2024,6,7,8,9,10,123456,tzinfo=timezone.utc)
    occurred_at=datetime(2020,1,2,12,34,56,tzinfo=timezone(timedelta(hours=8)))
    with repo.pool.connection() as c:
        experience_id=repo._archive_turn(c,uuid4(),user_text,assistant_text,description,[])
        # Old v3 metadata may retain a precise timestamp and anchors without the
        # newer occurrence field. Migration must preserve both byte-for-byte.
        c.execute('''UPDATE experiences SET recorded_at=%s,occurred_at=%s,
            anchors=jsonb_build_array((anchors->0) - 'occurrence') WHERE id=%s''',
            (recorded_at,occurred_at,experience_id))
        for table in ('episode_details','episode_cues','episode_evidence','episode_versions',
                      'episode_associations','episode_rehearsals','episode_chunks','episode_jobs','episodes'):
            c.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(table)))
        c.execute('ALTER TABLE experiences DROP COLUMN revision,DROP COLUMN source_hash')
        c.execute('''ALTER TABLE model_calls DROP COLUMN phase,DROP COLUMN source_refs,
            DROP COLUMN estimated_input_tokens,DROP COLUMN policy_version,DROP COLUMN estimator_version''')
        c.execute('ALTER TABLE context_links DROP CONSTRAINT context_links_source_type_check')
        c.execute('ALTER TABLE context_links DROP CONSTRAINT context_links_target_type_check')
        c.execute("ALTER TABLE context_links ADD CHECK(source_type IN ('memory','experience','self'))")
        c.execute("ALTER TABLE context_links ADD CHECK(target_type IN ('experience','self'))")
        c.execute('DELETE FROM schema_migrations WHERE version>=4')
        before=c.execute('SELECT * FROM experiences WHERE id=%s',(experience_id,)).fetchone()
        assert c.execute('SELECT max(version) AS version FROM schema_migrations').fetchone()['version']==3
    for _ in range(2):
        repo.migrate()
        with repo.pool.connection() as c:
            after=c.execute('SELECT * FROM experiences WHERE id=%s',(experience_id,)).fetchone()
            for field,value in before.items():
                assert after[field]==value,field
            assert after['source_hash']==source_hash(user_text,assistant_text)
            assert after['revision']==1
            assert c.execute('SELECT max(version) AS version FROM schema_migrations').fetchone()['version']==5
            assert c.execute('SELECT count(*) AS n FROM episodes').fetchone()['n']==0
    assert repo.backfill_episodes()==1
    assert repo.backfill_episodes()==0
    event=repo.episodes()[0]
    assert event['local_fallback'] and event['source_experience_ids']==[experience_id]
    assert any(item['quote']==user_text for item in event['evidence'])
    assert any(item['quote']=='紫藤戒指' for item in event['cues'])
    with repo.pool.connection() as c:
        final=c.execute('SELECT * FROM experiences WHERE id=%s',(experience_id,)).fetchone()
        for field,value in before.items():
            assert final[field]==value,field
