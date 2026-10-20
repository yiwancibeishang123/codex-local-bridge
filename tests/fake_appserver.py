"""Deterministic protocol fixture. NEVER a real Codex engine or deployment proof."""
import json
import sys
for line in sys.stdin:
    msg=json.loads(line);method=msg.get('method');params=msg.get('params',{})
    def emit(x): print(json.dumps(x),flush=True)
    if 'id' not in msg: continue
    if method=='initialize': result={'userAgent':'test-fixture'}
    elif method=='account/read': result={'account':{'type':'chatgpt','email':'fixture.invalid'}}
    elif method in ('thread/start','thread/resume'): result={'thread':{'id':params.get('threadId','fixture-thread')}}
    elif method=='turn/start':
        prompt=params['input'][0]['text']
        result={'turn':{'id':'fixture-turn','status':'inProgress'}}
        emit({'id':msg['id'],'result':result})
        edits=[] if 'MODE: read\n' in prompt else [{'path':'calculator.py','content':'def add(a, b):\n    return a + b\n'+ ('\ndef multiply(a, b):\n    return a * b\n' if 'multiply' in prompt else '')}]
        emit({'method':'item/completed','params':{'threadId':'fixture-thread','turnId':'fixture-turn',
              'item':{'id':'answer','type':'agentMessage','text':json.dumps({'summary':'Protocol fixture output','edits':edits})}}})
        emit({'method':'turn/completed','params':{'threadId':'fixture-thread','turn':{'id':'fixture-turn','status':'completed'}}})
        continue
    else: result={}
    emit({'id':msg['id'],'result':result})
