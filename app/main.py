from contextlib import asynccontextmanager
from typing import Literal
from typing import Annotated
from uuid import UUID
import secrets
import logging

from fastapi import Depends,FastAPI,Header,Query,Request,Response
from fastapi.responses import JSONResponse,RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials,HTTPBearer
from pydantic import AwareDatetime,BaseModel,ConfigDict,Field,field_validator,model_validator
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.config import Settings
from app.embeddings import LocalEmbeddings
from app.errors import MemoryError
from app.model import MiniMax
from app.repository import Repository
from app.service import MemoryService


class SaveMemory(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=8000)
    speaker_id:str=Field(default='user',min_length=1,max_length=100)
    subject:str=Field(default='user',min_length=1,max_length=200)
    allow_cloud:bool=True
    valid_from:AwareDatetime|None=None
    valid_until:AwareDatetime|None=None

    @field_validator('text','speaker_id','subject')
    @classmethod
    def nonempty(cls,value):
        if not value.strip():
            raise ValueError('内容不能为空白')
        return value.strip()


class CorrectMemory(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=1)
    text:str|None=Field(default=None,min_length=1,max_length=8000)
    speaker_id:str|None=Field(default=None,min_length=1,max_length=100)
    subject:str|None=Field(default=None,min_length=1,max_length=200)
    allow_cloud:bool|None=None
    valid_from:AwareDatetime|None=None
    valid_until:AwareDatetime|None=None

    @model_validator(mode='after')
    def validate_changes(self):
        if not (self.model_fields_set-{'expected_revision'}):
            raise ValueError('至少修改一个字段')
        for key in self.model_fields_set-{'expected_revision','valid_until'}:
            value=getattr(self,key)
            if value is None or isinstance(value,str) and not value.strip():
                raise ValueError(f'{key} 不能为空')
        return self


class SearchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    query:str=Field(min_length=1,max_length=2000)
    limit:int=Field(default=8,ge=1,le=30)
    subject:str|None=Field(default=None,max_length=200)

    @field_validator('query')
    @classmethod
    def nonempty(cls,value):
        if not value.strip():raise ValueError('查询不能为空白')
        return value.strip()


class ChatRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=8000)
    conversation_id:UUID|None=None
    auto_memory:bool=True
    recall_mode:Literal['natural','deep']='natural'

    @field_validator('text')
    @classmethod
    def nonempty(cls,value):
        if not value.strip():raise ValueError('问题不能为空白')
        return value


class ExperienceSearch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    query:str=Field(default='',max_length=2000)
    limit:int=Field(default=8,ge=1,le=50)
    after:AwareDatetime|None=None
    before:AwareDatetime|None=None
    time_basis:Literal['recorded_at','occurred_at']='recorded_at'
    include_stale:bool=False
    conversation_id:UUID|None=None

    @model_validator(mode='after')
    def valid_dates(self):
        if self.after and self.before and self.after>=self.before:
            raise ValueError('时间范围的结束必须晚于开始')
        return self


class RecallPreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    query:str=Field(min_length=1,max_length=8000)
    conversation_id:UUID|None=None
    recall_mode:Literal['natural','deep']='natural'
    budget:int|None=Field(default=None,ge=1000,le=100000)

    @field_validator('query')
    @classmethod
    def nonempty(cls,value):
        if not value.strip():raise ValueError('查询不能为空白')
        return value.strip()


def create_app(settings=None,repo=None,embeddings=None,model=None):
    settings=settings or Settings()
    if not settings.api_token:
        raise RuntimeError('API_TOKEN 未配置，请先运行启动脚本。')

    @asynccontextmanager
    async def lifespan(app):
        repository=repo or Repository(settings)
        try:
            repository.migrate()
            repository.replay_deletions(required=True)
            app.state.repo=repository
            app.state.service=MemoryService(repository,embeddings or LocalEmbeddings(settings),model or MiniMax(settings))
            yield
        finally:
            if getattr(app.state,'lab',None):
                app.state.lab.close()
            if repo is None:
                repository.close()

    app=FastAPI(title='事件记忆与动态唤醒',version='0.4.0',lifespan=lifespan)
    if settings.lab_enabled:
        @app.middleware('http')
        async def lab_only(request,call_next):
            if request.url.path.startswith('/v1/') and not request.url.path.startswith('/v1/lab/'):
                return JSONResponse(status_code=404,content={'error':'not_found','message':'实验服务请使用 /v1/lab 接口。'})
            return await call_next(request)
    bearer=HTTPBearer(auto_error=False)

    def authenticated(credentials:Annotated[HTTPAuthorizationCredentials|None,Depends(bearer)]):
        if not credentials or not secrets.compare_digest(credentials.credentials,settings.api_token):
            raise MemoryError('unauthorized','需要有效的本地访问令牌。',401)

    auth=[Depends(authenticated)]

    @app.exception_handler(MemoryError)
    async def memory_error(request,exc):
        return JSONResponse(status_code=exc.status,content={'error':exc.code,'message':exc.message})

    async def database_error(request,exc):
        logging.getLogger('memory').error('database_unavailable: %s',type(exc).__name__)
        return JSONResponse(status_code=503,content={'error':'database_unavailable','message':'本地数据库操作失败，未确认保存成功。'})
    app.add_exception_handler(DatabaseError,database_error)
    app.add_exception_handler(PoolTimeout,database_error)

    @app.get('/',include_in_schema=False)
    def root():
        return RedirectResponse('/lab' if settings.lab_enabled else '/docs')

    @app.get('/health')
    def health(request:Request):
        return request.app.state.repo.health()

    @app.post('/v1/memories',dependencies=auth,status_code=201)
    def remember(data:SaveMemory,request:Request,response:Response,idempotency_key:Annotated[str|None,Header(min_length=1,max_length=200)]=None):
        result,created=request.app.state.repo.remember(data.model_dump(),idempotency_key)
        response.status_code=201 if created else 200
        return result

    @app.get('/v1/memories',dependencies=auth)
    def listing(request:Request,limit:int=Query(default=20,ge=1,le=100),offset:int=Query(default=0,ge=0)):
        return request.app.state.repo.listing(limit,offset)

    @app.get('/v1/memories/{memory_id}',dependencies=auth)
    def get(memory_id:UUID,request:Request,history:bool=False):
        return request.app.state.repo.get(memory_id,history)

    @app.patch('/v1/memories/{memory_id}',dependencies=auth)
    def correct(memory_id:UUID,data:CorrectMemory,request:Request):
        changes=data.model_dump(exclude_unset=True,exclude={'expected_revision'})
        return request.app.state.repo.correct(memory_id,data.expected_revision,changes)

    @app.delete('/v1/memories/{memory_id}',dependencies=auth)
    def forget(memory_id:UUID,request:Request):
        return request.app.state.repo.forget(memory_id)

    @app.post('/v1/memories/{memory_id}/reindex',dependencies=auth)
    def reindex(memory_id:UUID,request:Request):
        return request.app.state.repo.reindex(memory_id)

    @app.post('/v1/search',dependencies=auth)
    def search(data:SearchRequest,request:Request):
        return request.app.state.service.search(data.query,data.limit,subject=data.subject)

    @app.post('/v1/chat',dependencies=auth)
    def chat(data:ChatRequest,request:Request):
        return request.app.state.service.chat(data.text,data.conversation_id,data.auto_memory,data.recall_mode)

    @app.get('/v1/episodes',dependencies=auth)
    def episodes(request:Request,limit:int=Query(default=50,ge=1,le=200),offset:int=Query(default=0,ge=0)):
        return {'items':request.app.state.repo.episodes(limit,offset),'limit':limit,'offset':offset}

    @app.get('/v1/episodes/{episode_id}',dependencies=auth)
    def episode(episode_id:UUID,request:Request):
        return request.app.state.repo.episode(episode_id)

    @app.post('/v1/recall/preview',dependencies=auth)
    def recall_preview(data:RecallPreview,request:Request):
        return request.app.state.service.recall_preview(**data.model_dump())

    @app.get('/v1/conversations/{conversation_id}',dependencies=auth)
    def conversation(conversation_id:UUID,request:Request,limit:int=Query(default=50,ge=1,le=200),offset:int=Query(default=0,ge=0)):
        return {'conversation_id':conversation_id,'messages':request.app.state.repo.history(conversation_id,limit,offset,cloud_only=False),'limit':limit,'offset':offset}

    @app.delete('/v1/conversations/{conversation_id}',dependencies=auth)
    def clear_conversation(conversation_id:UUID,request:Request):
        return request.app.state.repo.clear_conversation(conversation_id)

    @app.post('/v1/experiences/search',dependencies=auth)
    def experiences(data:ExperienceSearch,request:Request):
        items=request.app.state.service.experiences(**data.model_dump())
        return {'items':items,'limit':data.limit,'may_have_more':len(items)==data.limit}

    @app.get('/v1/experiences/{experience_id}',dependencies=auth)
    def experience(experience_id:UUID,request:Request):
        return request.app.state.repo.experience(experience_id)

    @app.delete('/v1/experiences/{experience_id}',dependencies=auth)
    def forget_experience(experience_id:UUID,request:Request):
        return request.app.state.repo.forget_experience(experience_id)

    @app.get('/v1/self',dependencies=auth)
    def self_state(request:Request,per_domain:int=Query(default=6,ge=1,le=100),query:str=Query(default='',max_length=2000)):
        return {'states':request.app.state.repo.self_state(per_domain,query),'epistemic_status':'internal_interpretation'}

    @app.get('/v1/self/history',dependencies=auth)
    def self_history(request:Request,domain:Literal['emotion','relationship','identity','belief','goal']|None=None,
                     key:str|None=Query(default=None,max_length=100),limit:int=Query(default=50,ge=1,le=200),offset:int=Query(default=0,ge=0)):
        return {'observations':request.app.state.repo.self_history(domain,key,limit,offset),'limit':limit,'offset':offset}

    @app.delete('/v1/self/{observation_id}',dependencies=auth)
    def forget_observation(observation_id:UUID,request:Request):
        return request.app.state.repo.forget_observation(observation_id)

    if settings.lab_enabled:
        from app.lab import register_lab
        register_lab(app,settings,authenticated)
    return app


app=create_app()
