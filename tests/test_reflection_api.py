"""Production reflection controls remain local, authenticated and read-only until queued."""


def test_reflection_control_requires_auth(client):
    for method,path,body in [
        ('get','/v1/reflection/settings',None),
        ('patch','/v1/reflection/settings',{'enabled':True}),
        ('post','/v1/reflection/preview',{}),
        ('post','/v1/reflection/runs',None),
        ('get','/v1/reflection/runs',None),
    ]:
        result=client.request(method,path,json=body,headers={'Authorization':'Bearer invalid'})
        assert result.status_code==401


def test_reflection_preview_has_no_cloud_cost_or_write(client,repo,model):
    with repo.pool.connection() as c:
        before=c.execute('SELECT count(*) AS n FROM reflection_runs').fetchone()['n']
        before_calls=c.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n']
    preview=client.post('/v1/reflection/preview',json={})
    assert preview.status_code==200
    assert not model.requests and not model.decision_requests
    with repo.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM reflection_runs').fetchone()['n']==before
        assert c.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n']==before_calls
    assert client.get('/v1/reflection/runs').json()['items']==[]


def test_reflection_settings_strict_and_persistent(client):
    assert client.get('/v1/reflection/settings').status_code==200
    for enabled in (True,False):
        response=client.patch('/v1/reflection/settings',json={'enabled':enabled})
        assert response.status_code==200
        assert response.json()['enabled'] is enabled
        assert client.get('/v1/reflection/settings').json()['enabled'] is enabled
    assert client.patch('/v1/reflection/settings',json={'enabled':'yes'}).status_code==422
    assert client.patch('/v1/reflection/settings',json={'enabled':True,'daily_calls':999}).status_code==422
    assert client.get('/v1/reflection/runs/not-a-uuid').status_code==422


def test_invalid_provider_usage_cannot_break_call_finalization(repo):
    from uuid import uuid4
    call=repo.start_call(uuid4(),[])
    repo.finish_call(call,'failed',{'input_tokens':2**80,'output_tokens':True})
    with repo.pool.connection() as c:
        row=c.execute('SELECT status,input_tokens,output_tokens FROM model_calls WHERE id=%s',(call,)).fetchone()
    assert row=={'status':'failed','input_tokens':None,'output_tokens':None}
