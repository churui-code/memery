"""MiniMax Anthropic Messages transport. Never retain thinking or provider error bodies."""
import httpx
from app.errors import MemoryError


class MiniMax:
    def __init__(self, settings):
        self.settings = settings

    def chat(self, system, messages):
        data=self._request(system,messages)
        try:
            if data.get('stop_reason')!='end_turn':
                raise ValueError('incomplete_response')
            text='\n'.join(block['text'] for block in data.get('content',[]) if block.get('type')=='text').strip()
            if not text:
                raise ValueError('empty_response')
            import re
            text=re.sub(r'<think\b[^>]*>[\s\S]*?</think>','',text,flags=re.I).strip()
            if not text or re.search(r'<think\b',text,re.I):
                raise ValueError('reasoning_only')
            return {'text':text,'usage':data.get('usage',{})}
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            raise MemoryError('model_invalid_response','M3 未返回完整有效的最终回答。',502) from exc

    def decide_memories(self,system,prompt):
        from app.automatic import MemoryDecision
        tool={'name':'propose_memories','description':'提出本轮记忆变更；空 actions 表示不应写入。',
              'input_schema':MemoryDecision.model_json_schema()}
        data=self._request(system,[{'role':'user','content':prompt}],tool=tool)
        try:
            calls=[block for block in data['content'] if block.get('type')=='tool_use']
            if data.get('stop_reason')!='tool_use' or len(calls)!=1 or calls[0]['name']!='propose_memories':
                raise ValueError('invalid_tool')
            if not isinstance(calls[0]['input'],dict):raise ValueError('invalid_input')
            return {'decision':calls[0]['input'],'usage':data.get('usage',{})}
        except (ValueError,KeyError,TypeError) as exc:
            raise MemoryError('memory_decision_invalid','M3 未返回有效记忆判断；本轮未写入。',502) from exc

    def _request(self, system, messages,tool=None):
        if not self.settings.model_key:
            raise MemoryError('model_not_configured','未配置 M3 密钥；本地记忆功能仍可使用。',503)
        base=self.settings.model_base_url.rstrip('/')
        endpoint=base if base.endswith('/messages') else base+'/v1/messages'
        payload={'model':self.settings.model_name,'system':system,'messages':messages,
                 'max_tokens':4096 if tool else 2048,'stream':False,
                 'thinking':{'type':'adaptive' if self.settings.thinking=='enabled' and not tool else 'disabled'}}
        if tool:
            payload.update(tools=[tool],tool_choice={'type':'tool','name':tool['name']})
        try:
            with httpx.Client(timeout=httpx.Timeout(self.settings.model_timeout,connect=10)) as client:
                response=client.post(endpoint,headers={'x-api-key':self.settings.model_key,'anthropic-version':'2023-06-01'},
                    json=payload)
        except httpx.TimeoutException as exc:
            raise MemoryError('model_timeout','M3 请求超时，本地记忆未受影响。',504) from exc
        except httpx.HTTPError as exc:
            raise MemoryError('model_unavailable','无法连接 M3，本地记忆仍可使用。',502) from exc
        if response.status_code==429:
            raise MemoryError('model_rate_limited','M3 当前限流，请稍后再试。',503)
        if response.status_code>=400:
            raise MemoryError('model_rejected',f'M3 请求失败（HTTP {response.status_code}）。',502)
        try:
            data=response.json()
            if not isinstance(data,dict) or data.get('type')=='error' or data.get('error'):
                raise ValueError('provider_error')
            return data
        except (ValueError,KeyError,TypeError) as exc:
            raise MemoryError('model_invalid_response','M3 未返回完整有效的最终回答。',502) from exc
