"""MiniMax Anthropic Messages transport. Never retain thinking or provider error bodies."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
import httpx
from app.errors import MemoryError


_request_deadline = ContextVar('memory_request_deadline', default=None)


def safe_usage(usage):
    """Retain only valid provider counters that fit the audit's integer fields."""
    if not isinstance(usage,dict):
        return {}
    return {key:usage[key] for key in ('input_tokens','output_tokens')
            if type(usage.get(key)) is int and 0<=usage[key]<=2147483647}


def inline_tool_schema(schema):
    """Inline local references for providers that incompletely resolve $defs."""
    definitions = schema.get('$defs', {})
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            return visit(definitions[value['$ref'].removeprefix('#/$defs/')])
        result = {key: visit(item) for key, item in value.items() if key != '$defs'}
        if isinstance(result.get('title'), str):
            result.pop('title')
        # Dates are derived locally from a verified original time phrase.
        # Do not ask the provider to invent timestamps for optional fields.
        properties = result.get('properties', {})
        if 'quote' in properties:
            properties['quote_refs'] = {'type': 'array', 'minItems': 1, 'maxItems': 128,
                'items': {'type': 'string'},
                'description': '当前原文单元ID，递增无重复；取首末完整连续范围。使用时省略quote/occurrence，详见系统规则。'}
            properties['quote']['description'] = '逐字连续原文，不能改写；优先用quote_refs。'
            result['required'] = [key for key in result.get('required', []) if key != 'quote']
            result['anyOf'] = [{'required': ['quote_refs']}, {'required': ['quote']}]
        if 'time_quote' in properties:
            for key in ('occurred_at', 'occurred_until', 'time_precision'):
                properties.pop(key, None)
        if 'self_updates' in properties:
            # Require an explicit consideration, including [] when no genuine
            # change occurred; a new personal fact is not a prerequisite.
            result['required'] = list(dict.fromkeys([*result.get('required', []), 'self_updates']))
            properties['self_updates']['description'] = '必填，独立判断本轮是否形成或修订有依据的情绪、关系、观点或目标。actions为空仍可有自己的新观点；无真实变化则明确返回[]。'
        return result
    return visit(schema)


def structured_tool(phase):
    from app.automatic import MemoryDecision, MemoryReassessment
    if phase == 'intake':
        return {'name': 'propose_memories', 'description': '提出本轮记忆变更；空 actions 表示不应写入。',
                'input_schema': inline_tool_schema(MemoryDecision.model_json_schema())}
    if phase == 'reassessment':
        return {'name': 'reassess_memories', 'description': '根据最终证据重评经历与内部状态，不改变固定事实操作。',
                'input_schema': inline_tool_schema(MemoryReassessment.model_json_schema())}
    return None


def request_payload(settings, system, messages, tool=None):
    payload = {'model': settings.model_name, 'system': system, 'messages': messages,
               'max_tokens': 8192 if tool else 2048, 'stream': False,
               'thinking': {'type': 'adaptive' if settings.thinking == 'enabled' and not tool else 'disabled'}}
    if tool:
        payload.update(tools=[tool], tool_choice={'type': 'tool', 'name': tool['name']})
    return payload


class MiniMax:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def request_scope(self, deadline):
        token=_request_deadline.set(deadline)
        try:
            yield
        finally:
            _request_deadline.reset(token)

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
            error=MemoryError('model_invalid_response','M3 未返回完整有效的最终回答。',502)
            error.usage=safe_usage(data.get('usage') if isinstance(data,dict) else None)
            raise error from exc

    def decide_memories(self,system,prompt):
        return self._structured(system,prompt,structured_tool('intake'))

    def reassess_memories(self,system,prompt):
        return self._structured(system,prompt,structured_tool('reassessment'))

    def _structured(self,system,prompt,tool):
        data=self._request(system,[{'role':'user','content':prompt}],tool=tool)
        try:
            calls=[block for block in data['content'] if block.get('type')=='tool_use']
            if data.get('stop_reason')!='tool_use' or len(calls)!=1 or calls[0]['name']!=tool['name']:
                raise ValueError('invalid_tool')
            if not isinstance(calls[0]['input'],dict):raise ValueError('invalid_input')
            return {'decision':calls[0]['input'],'usage':data.get('usage',{})}
        except (ValueError,KeyError,TypeError) as exc:
            error=MemoryError('memory_decision_invalid','M3 未返回有效记忆判断；本轮未写入。',502)
            error.usage=safe_usage(data.get('usage') if isinstance(data,dict) else None)
            raise error from exc

    def _request(self, system, messages,tool=None):
        if not self.settings.model_key:
            raise MemoryError('model_not_configured','未配置 M3 密钥；本地记忆功能仍可使用。',503)
        base=self.settings.model_base_url.rstrip('/')
        endpoint=base if base.endswith('/messages') else base+'/v1/messages'
        payload=request_payload(self.settings,system,messages,tool)
        deadline=_request_deadline.get()
        remaining=deadline-monotonic() if deadline is not None else self.settings.model_timeout
        if remaining<=0:
            raise MemoryError('chat_deadline','本轮达到整体等待期限，没有提交不完整记忆。',504)
        async def send():
            # HTTP phase timeouts alone cannot bound a slowly streaming body.
            # Cancellation supplies a real wall-clock limit for the entire request.
            async with asyncio.timeout(min(remaining,self.settings.model_timeout)):
                async with httpx.AsyncClient(timeout=httpx.Timeout(min(remaining,self.settings.model_timeout),connect=min(10,remaining))) as client:
                    return await client.post(endpoint,headers={'x-api-key':self.settings.model_key,'anthropic-version':'2023-06-01'},json=payload)
        try:
            response=asyncio.run(send())
        except (httpx.TimeoutException,TimeoutError) as exc:
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
