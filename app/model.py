"""MiniMax Anthropic Messages transport. Never retain thinking or provider error bodies."""
import asyncio
import json
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from uuid import UUID
import httpx
from app.errors import MemoryError


_request_deadline = ContextVar('memory_request_deadline', default=None)


def safe_usage(usage):
    """Retain only valid provider counters that fit the audit's integer fields."""
    if not isinstance(usage,dict):
        return {}
    return {key:usage[key] for key in ('input_tokens','output_tokens')
            if type(usage.get(key)) is int and 0<=usage[key]<=2147483647}


def inline_tool_schema(schema, *, references_only=False):
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
                'description': '连续原文单元ID。'}
            result['required'] = [key for key in result.get('required', []) if key != 'quote']
            if references_only:
                # One provider representation prevents missing top-level evidence
                # and conflicting duplicated quote text. Legacy local input still validates.
                properties.pop('quote')
                properties.pop('occurrence', None)
                result['required'].append('quote_refs')
            else:
                properties['quote']['description'] = '逐字原话。'
                result['anyOf'] = [{'required': ['quote_refs']}, {'required': ['quote']}]
        if 'durability_quote' in properties:
            result['required'].append('durability_quote')
        if 'old_quote' in properties:
            properties['old_quote'] = {'type':'string','minLength':1,'maxLength':8000,
                'description':'选中旧detail中被否定的准确子串，只取错误主张；服务端定位，勿扩展到其他人物或金额。'}
            result['required'].append('old_quote')
        if 'time_quote' in properties:
            for key in ('occurred_at', 'occurred_until', 'time_precision'):
                properties.pop(key, None)
        if 'self_updates' in properties:
            # Require an explicit consideration, including [] when no genuine
            # change occurred; a new personal fact is not a prerequisite.
            result['required'] = list(dict.fromkeys([*result.get('required', []), 'self_updates']))
            properties['self_updates']['description'] = '必填数组。retain_turn=false或forget时必须为[]；允许留存时独立判断有据的认识变化，无变化返回[]。'
        if 'retain_turn' in properties:
            result['required'] = list(dict.fromkeys([*result.get('required', []), 'retain_turn']))
            properties['retain_turn']['description'] = '不留存时为false，experience必须为null，episodes/self_updates/rehearsals=[]；actions仅可含明确要求的forget。'
        return result
    return visit(schema)


class CloudContractError(ValueError):
    def __init__(self, path, code):
        super().__init__('invalid_cloud_contract')
        self.diagnostics = [{'code':code, 'path':list(path)}]


def validate_cloud_contract(value, schema, path=()):
    """Collect bounded metadata so one re-proposal can address all field errors."""
    diagnostics = []
    def reject(location, code):
        if len(diagnostics) < 12:
            diagnostics.append({'code': code, 'path': list(location)})
    def visit(value, schema, path):
        if value is None or len(diagnostics) >= 12:
            return
        if 'anyOf' in schema:
            candidates = [branch for branch in schema['anyOf'] if branch.get('type') != 'null']
            if len(candidates) == 1:
                schema = candidates[0]
        if ('enum' in schema and value not in schema['enum']) or schema.get('type') == 'null':
            reject(path, 'cloud_value_not_allowed')
            return
        if schema.get('type') == 'array':
            # Known provider array wrappers do not change any element's authority.
            if isinstance(value, dict) and set(value) == {'item'}:
                value = value['item'] if isinstance(value['item'], list) else [value['item']]
            if isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, schema.get('items', {}), path+(index,))
            return
        if not isinstance(value, dict):
            return  # Pydantic checks types and ranges after exact quote binding.
        properties = schema.get('properties', {})
        no_record = (not path and value.get('retain_turn') is False and type(value.get('actions')) is list
                     and not value['actions'])
        discarded = {'experience','episodes','self_updates','rehearsals','corrections'} if no_record else set()
        for key in schema.get('required', []):
            if key not in value and key not in discarded:
                reject(path+(key,), 'cloud_required_field_missing')
        if 'quote_refs' in properties and ('quote' in value or 'occurrence' in value):
            reject(path+('quote_refs',), 'cloud_duplicate_quote_representation')
        if 'old_quote' in properties and 'old_quote' in value and (
                not isinstance(value['old_quote'], str) or not value['old_quote'].strip()):
            reject(path+('old_quote',), 'cloud_old_quote_required')
        if schema.get('additionalProperties') is False and any(key not in properties for key in value):
            # Unknown model keys can contain private text; never log their names.
            reject(path+('field',), 'cloud_extra_field_forbidden')
        for key, child in properties.items():
            if key in value and key not in discarded:
                visit(value[key], child, path+(key,))
    visit(value, schema, path)
    if diagnostics:
        error = CloudContractError(diagnostics[0]['path'], diagnostics[0]['code'])
        error.diagnostics = diagnostics
        raise error


def _reflection_schema(schema, phase, payload):
    """Constrain provider IDs to the same frozen sources validated locally."""
    evidence_ids = set()
    for episode in payload.get('episodes', []):
        for evidence in episode.get('evidence', []):
            value = evidence.get('id')
            try:
                UUID(value)
            except (ValueError, TypeError, AttributeError):
                continue
            evidence_ids.add(value)
    if phase == 'reflection_proposal':
        field = schema['properties']['updates']['items']['properties']['evidence_ids']
        field['items']['enum'] = sorted(evidence_ids)
        field['description'] += ' 必须逐字选择enum中的原文证据ID，不得使用episode/self/target的ID。'
        return schema
    proposals = payload.get('proposals', [])
    units = payload.get('review_units', [])
    mapping = {row['id']: {'evidence_ids': sorted(set(row['evidence_ids']) & evidence_ids),
        'unit_ids': [unit['unit_id'] for unit in units if unit['proposal_id'] == row['id']]}
        for row in proposals}
    fields = schema['properties']['reviews']['items']['properties']
    fields['proposal_id']['enum'] = list(mapping)
    checks = fields['checks']['items']['properties']
    checks['unit_id']['enum'] = [uid for row in mapping.values() for uid in row['unit_ids']]
    checks['evidence_ids']['items']['enum'] = sorted({eid for row in mapping.values() for eid in row['evidence_ids']})
    # A global enum prevents invented IDs; the server still checks each exact
    # proposal/unit/source association instead of trusting the provider schema.
    fields['checks']['description'] = ('每条review只可使用该proposal对应的unit_ids和evidence_ids；'
        '逐一覆盖全部unit_ids且不得重复。对应关系：' + json.dumps(mapping, ensure_ascii=False, separators=(',', ':')))
    return schema


def structured_tool(phase, payload=None):
    from app.automatic import MemoryDecision, MemoryReassessment
    if phase == 'intake':
        schema = inline_tool_schema(MemoryDecision.model_json_schema(), references_only=True)
        if isinstance(payload,dict):
            fields = schema['properties']['actions']['items']['properties']
            targets = sorted({str(row['id']) for row in payload.get('memory_evidence',[])})
            fields['target_id']['description'] = '仅可选择当前memory_evidence中的事实ID，事件ID只用于corrections。'
            if targets:
                fields['target_id']['anyOf'][0]['enum'] = targets
            else:
                fields['operation']['enum'] = ['create']
                fields['target_id'] = {'type':'null','description':'没有独立事实目标，不可更新或遗忘事件ID。'}
            from app.corrections import correction_targets
            targets = correction_targets(payload)
            correction = schema['properties']['corrections']['items']
            coordinates = {'target_id','expected_revision','experience_id','start','end'}
            correction['properties'] = {key:value for key,value in correction['properties'].items()
                                        if key not in coordinates}
            correction['properties']['detail_ref'] = {'type':'string',
                'description':'从本次correction_targets选择一个已展示用户细节；程序绑定其事件、版本及原文范围，勿自行生成坐标。'}
            correction['required'] = [key for key in correction['required'] if key not in coordinates] + ['detail_ref']
            if not targets:
                schema['properties']['corrections']['maxItems'] = 0
            else:
                correction['properties']['detail_ref']['enum'] = list(targets)
        return {'name': 'propose_memories', 'description': '先决定留存，再提出允许的事实与内部状态变更。',
                'input_schema': schema}
    if phase == 'reassessment':
        schema = inline_tool_schema(MemoryReassessment.model_json_schema(), references_only=True)
        if isinstance(payload,dict):
            excluded={str(row['target_id']) for row in payload.get('planned_episode_corrections',[])}
            targets=sorted({str(row['id']) for row in payload.get('experience_evidence',[])
                            if not row.get('legacy_projection') and str(row['id']) not in excluded})
            field=schema['properties']['episodes']['items']['properties']['target_id']
            field['description']='只可追加本轮允许的既有事件；正在纠错的目标不允许追加，纠错证据另建事件。'
            if targets:
                field['anyOf'][0]['enum']=targets
            else:
                schema['properties']['episodes']['items']['properties']['target_id']={
                    'type':'null','description':field['description']}
        return {'name': 'reassess_memories', 'description': '根据最终证据重评经历与内部状态，不改变固定事实操作。',
                'input_schema': schema}
    if phase in ('reflection_proposal', 'reflection_review'):
        from app.reflection_types import ReflectionProposal, ReflectionReview
        model = ReflectionProposal if phase == 'reflection_proposal' else ReflectionReview
        schema = inline_tool_schema(model.model_json_schema())
        if payload is not None:
            schema = _reflection_schema(schema, phase, payload)
        return {'name': 'propose_reflection' if phase == 'reflection_proposal' else 'review_reflection',
                'description': '根据冻结原文提出内部认识变化。' if phase == 'reflection_proposal' else '逐句核对原反思提案与绑定证据，完整返回各单元判定后接受或拒绝，不修改提案。',
                'input_schema': schema}
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
        try:
            payload=json.loads(prompt)
        except (ValueError,TypeError):
            payload=None
        from app.corrections import correction_targets
        return self._structured(system,prompt,structured_tool('intake',payload),
                                correction_sources=correction_targets(payload) if isinstance(payload,dict) else None)

    def reassess_memories(self,system,prompt):
        try:
            payload=json.loads(prompt)
        except (ValueError,TypeError):
            payload=None
        return self._structured(system,prompt,structured_tool('reassessment',payload))

    def propose_reflection(self,system,prompt):
        return self._structured(system,prompt,structured_tool('reflection_proposal', json.loads(prompt)))

    def review_reflection(self,system,prompt):
        return self._structured(system,prompt,structured_tool('reflection_review', json.loads(prompt)))

    def _structured(self,system,prompt,tool,correction_sources=None):
        data=self._request(system,[{'role':'user','content':prompt}],tool=tool)
        try:
            calls=[block for block in data['content'] if block.get('type')=='tool_use']
            if data.get('stop_reason')!='tool_use' or len(calls)!=1 or calls[0]['name']!=tool['name']:
                raise ValueError('invalid_tool')
            if not isinstance(calls[0]['input'],dict):raise ValueError('invalid_input')
            if tool['name'] in ('propose_memories','reassess_memories'):
                validate_cloud_contract(calls[0]['input'],tool['input_schema'])
            decision = calls[0]['input']
            no_record = (decision.get('retain_turn') is False and type(decision.get('actions')) is list
                         and not decision['actions'])
            if correction_sources is not None and decision.get('corrections') and not no_record:
                from copy import deepcopy
                decision = deepcopy(decision)
                rows = decision['corrections']
                if isinstance(rows,dict) and set(rows)=={'item'}:
                    rows = rows['item'] if isinstance(rows['item'],list) else [rows['item']]
                    decision['corrections'] = rows
                if isinstance(rows,list):
                    for index,row in enumerate(rows):
                        if not isinstance(row,dict):
                            continue  # Typed validation rejects malformed elements.
                        ref = row.pop('detail_ref', None)
                        if not isinstance(ref,str) or ref not in correction_sources:
                            raise CloudContractError(('corrections',index,'detail_ref'),'cloud_value_not_allowed')
                        row.update({key:value for key,value in correction_sources[ref].items() if key!='quote'})
            return {'decision':decision,'usage':data.get('usage',{})}
        except (ValueError,KeyError,TypeError) as exc:
            error=MemoryError('memory_decision_invalid','M3 未返回有效记忆判断；本轮未写入。',502)
            error.usage=safe_usage(data.get('usage') if isinstance(data,dict) else None)
            if isinstance(exc,CloudContractError):
                error.diagnostics=exc.diagnostics
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
