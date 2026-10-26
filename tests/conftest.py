from dataclasses import replace
from pathlib import Path
from uuid import uuid4
import pytest
from psycopg import sql
from fastapi.testclient import TestClient
from app.config import Settings
from app.repository import Repository
from app.main import create_app


class FakeEmbeddings:
    def embed(self,texts,query=False):
        result=[]
        for text in texts:
            v=[0.0]*512
            group=1 if any(w in text for w in ('空调','温度','几度','调温')) else 2 if any(w in text for w in ('咖啡','饮料')) else 3
            v[group]=1.0
            result.append(v)
        return result


class FakeModel:
    def __init__(self):
        self.requests=[]
        self.callback=None
        self.error=None
        self.decision={'actions':[]}
        self.decision_requests=[]

    def decide_memories(self,system,prompt):
        self.decision_requests.append(prompt)
        return {'decision':self.decision,'usage':{}}

    def chat(self,system,messages):
        self.requests.append(messages)
        if self.callback: self.callback()
        if self.error: raise self.error
        return {'text':'根据提供的记忆回答。','usage':{'input_tokens':10,'output_tokens':5}}


@pytest.fixture
def repo(tmp_path):
    settings=replace(Settings(),db_schema='test_'+uuid4().hex,api_token='test-token',deletion_journal=str(tmp_path/'deletions.jsonl'))
    Path(settings.deletion_journal).touch()
    repo=Repository(settings)
    repo.migrate()
    yield repo
    with repo.pool.connection() as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(settings.db_schema)))
    repo.close()


@pytest.fixture
def model():
    return FakeModel()


@pytest.fixture
def client(repo,model):
    app=create_app(repo.settings,repo=repo,embeddings=FakeEmbeddings(),model=model)
    with TestClient(app,headers={'Authorization':'Bearer test-token'}) as client:
        yield client
