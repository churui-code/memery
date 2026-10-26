"""Bind current-input unit references to exact quotations before validation."""
from copy import deepcopy
from pydantic import BaseModel


_SENTENCE_END = frozenset('。！？!?；;')
_CLOSING = frozenset('\"\'”’」』）)]】»')
_MAX_UNITS = 64


def source_units(text):
    """Group complete sentences/lines, preserving every Unicode code point."""
    if not isinstance(text,str):
        raise TypeError('source_text_must_be_string')
    result=[]
    start=index=0
    while index<len(text):
        char=text[index]
        boundary=char in _SENTENCE_END or char in '\r\n'
        if char=='.':
            following=text[index+1:index+2]
            boundary=not following or following.isspace() or following in _CLOSING
        if not boundary:
            index+=1
            continue
        end=index+1
        if char=='\r' and end<len(text) and text[end]=='\n':
            end+=1
        elif char not in '\r\n':
            while end<len(text) and (text[end] in _SENTENCE_END or text[end] in _CLOSING):
                end+=1
        result.append({'id':f'u{len(result)+1:04d}','start':start,'end':end,'text':text[start:end]})
        start=index=end
    if start<len(text):
        result.append({'id':f'u{len(result)+1:04d}','start':start,'end':len(text),'text':text[start:]})
    if len(result)>_MAX_UNITS:
        group_size=(len(result)+_MAX_UNITS-1)//_MAX_UNITS
        grouped=[]
        for index in range(0,len(result),group_size):
            start=result[index]['start']
            end=result[min(index+group_size,len(result))-1]['end']
            grouped.append({'id':f'u{len(grouped)+1:04d}',
                            'start':start,'end':end,'text':text[start:end]})
        return grouped
    return result


def _occurrence(text, quote, wanted_start):
    start=-1
    count=0
    while True:
        start=text.find(quote,start+1)
        if start<0 or start>wanted_start:
            raise ValueError('quote_refs_source_position_invalid')
        if start==wanted_start:
            return count
        count+=1


def _indexed_quote_path(path):
    if len(path)>=3 and path[-3:-1]==('experience','anchors') and isinstance(path[-1],int):
        return True
    if len(path)>=2 and path[-2]=='episodes' and isinstance(path[-1],int):
        return True
    return (len(path)>=4 and path[-4]=='episodes' and isinstance(path[-3],int)
            and path[-2] in ('details','cues') and isinstance(path[-1],int))


def bind_source_references(raw, originaltext):
    """Resolve only quote_refs from this exact input, without changing raw.

    Ordered references delimit one continuous span including intervening units.
    Existing quote fields must agree exactly. Other fields retain their input
    values for the normal Pydantic validators; references cannot supply offsets,
    alternate sources, or arbitrary replacement fields.
    """
    units=source_units(originaltext)
    positions={unit['id']:index for index,unit in enumerate(units)}

    def walk(value,path=()):
        if isinstance(value,BaseModel):
            value=value.model_dump(mode='python')
        if isinstance(value,list):
            return [walk(item,path+(index,)) for index,item in enumerate(value)]
        if isinstance(value,tuple):
            return tuple(walk(item,path+(index,)) for index,item in enumerate(value))
        if not isinstance(value,dict):
            return deepcopy(value)
        if set(value)=={'item'}:
            # Preserve the provider's array wrapper for existing validators,
            # while keeping its structural path identical to an ordinary list.
            item=value['item']
            return {'item':walk(item,path if isinstance(item,list) else path+(0,))}
        result={key:walk(item,path+(key,)) for key,item in value.items() if key!='quote_refs'}
        if 'quote_refs' not in value:
            return result
        refs=value['quote_refs']
        if isinstance(refs,dict) and set(refs)=={'item'}:
            item=refs['item']
            refs=item if isinstance(item,list) else [item]
        if not isinstance(refs,list) or not refs:
            raise ValueError('quote_refs_must_be_nonempty_list')
        if any(not isinstance(ref,str) or ref not in positions for ref in refs):
            raise ValueError('quote_refs_unknown_current_source')
        indexes=[positions[ref] for ref in refs]
        if any(current<=previous for previous,current in zip(indexes,indexes[1:])):
            raise ValueError('quote_refs_must_be_ordered_and_unique')
        start=units[indexes[0]]['start']
        end=units[indexes[-1]]['end']
        quote=originaltext[start:end]
        existing=result.get('quote')
        if existing is not None and existing!='' and existing!=quote:
            raise ValueError('quote_refs_quote_conflict')
        occurrence=_occurrence(originaltext,quote,start)
        if 'occurrence' in result and (type(result['occurrence']) is not int or result['occurrence']!=occurrence):
            raise ValueError('quote_refs_occurrence_conflict')
        result['quote']=quote
        if _indexed_quote_path(path):
            result['occurrence']=occurrence
        return result

    return walk(raw)
