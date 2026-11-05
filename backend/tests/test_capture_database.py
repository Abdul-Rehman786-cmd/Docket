"""Real disposable PostgreSQL + ONNX. AI/HTTP/Switch are mocked; no hosted writes.

The HTTP mock delegates repository RPCs to actual SQL via a local Node pipe.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from app.core.http_clients import use_http_clients
from app.core.embedding_model import MODEL_DIR,EmbeddingError,get_embedder
from app.repositories.message_receipts import MessageReceiptRepository
from app.services import ai_service,embedding_service,retrieval_service
from app.models.ai import LLMDecision
from app.services.extraction_service import storage_items
from test_useful_storage import config,message

ROOT=Path(__file__).resolve().parents[2]
NODE=shutil.which('node')
SQL_AVAILABLE=bool(NODE and (ROOT/'supabase/tests/node_modules/@electric-sql/pglite').exists())
MODEL_AVAILABLE=bool(importlib.util.find_spec('onnxruntime') and importlib.util.find_spec('tokenizers')
    and (MODEL_DIR/'onnx/model.onnx').exists() and (MODEL_DIR/'tokenizer.json').exists())
pytestmark=pytest.mark.skipif(not SQL_AVAILABLE,reason='Install existing supabase/tests devDependencies')

@pytest.fixture
def anyio_backend():return 'asyncio'

class Database:
    def __init__(self):
        self.lock=threading.Lock()
        self.process=subprocess.Popen([NODE,str(ROOT/'supabase/tests/capture-database.mjs')],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        ready=json.loads(self.process.stdout.readline());assert ready.get('ready'),ready
    def query(self,sql,*params):
        with self.lock:
            self.process.stdin.write(json.dumps({'sql':sql,'params':params})+'\n');self.process.stdin.flush()
            response=json.loads(self.process.stdout.readline())
        if 'error' in response:raise ValueError(response['error'])
        return response['rows']
    def close(self):
        self.process.stdin.close()
        try:self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.process.stdout.close();self.process.stderr.close()

@pytest.fixture
def db():
    database=Database()
    try:yield database
    finally:database.close()

def seed(db):
    tenant,room=str(uuid4()),str(uuid4())
    db.query("INSERT INTO public.tenants(id,name) VALUES($1::uuid,'Capture fixture')",tenant)
    db.query("INSERT INTO public.rooms(id,tenant_id,switch_room_id,platform) VALUES($1::uuid,$2::uuid,'test-room','discord')",room,tenant)
    return tenant,room

def transport(db,tenant,room,*,lose_first_ack=False):
    lost=False
    async def handler(request):
        nonlocal lost
        async def query(sql,*params):return await asyncio.to_thread(db.query,sql,*params)
        name=request.url.path.rsplit('/',1)[-1]
        if request.method=='GET':
            if name=='rooms':return httpx.Response(200,json=await query('SELECT id,tenant_id,enabled FROM public.rooms WHERE id=$1::uuid',room))
            if name in ('messages','commitments','decisions'):
                return httpx.Response(200,json=await query(f'SELECT * FROM public.{name} WHERE tenant_id=$1::uuid AND room_id=$2::uuid',tenant,room))
            raise AssertionError('Unexpected test route')
        p=json.loads(request.content)
        try:
            if name=='store_message_extraction':
                assert p['p_capture'] is True
                rows=await query('SELECT public.store_message_extraction($1::uuid,$2,$3::jsonb,$4::jsonb,$5::boolean) result',
                    p['p_tenant_id'],p['p_switch_room_id'],json.dumps(p['p_source']),json.dumps(p['p_items']),p['p_capture'])
                result=rows[0]['result']
                if lose_first_ack and not lost:
                    lost=True
                    raise httpx.ReadError('Simulated acknowledgement loss',request=request)
            elif name=='claim_memory_embeddings':
                result=await query('SELECT * FROM public.claim_memory_embeddings($1::uuid,$2::uuid[],$3::integer)',
                    p['p_tenant_id'],'{'+','.join(p['p_room_ids'])+'}',p['p_limit'])
            elif name=='finish_memory_embedding':
                rows=await query('SELECT public.finish_memory_embedding($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6::extensions.vector,$7) result',
                    p['p_memory_id'],p['p_tenant_id'],p['p_room_id'],p['p_claim_token'],p['p_content_hash'],
                    json.dumps(p['p_embedding']) if p['p_embedding'] else None,p['p_error_code']);result=rows[0]['result']
            elif name=='search_memories':
                result=await query('SELECT * FROM public.search_memories($1::uuid,$2::uuid,$3::extensions.vector,$4::integer)',
                    p['p_tenant_id'],p['p_room_id'],json.dumps(p['p_embedding']),p['p_limit'])
            else:raise AssertionError('Unexpected test RPC')
        except ValueError:return httpx.Response(400,json={'message':'fixture_sql_rejected'})
        return httpx.Response(200,json=result)
    return httpx.MockTransport(handler)

@pytest.mark.anyio
@pytest.mark.skipif(not MODEL_AVAILABLE,reason='Install pinned embedding dependencies and run app.embedding_setup')
async def test_real_capture_worker_retry_and_paraphrased_semantic_search(db,monkeypatch):
    tenant,room=seed(db)
    db.query('SET ROLE service_role')
    settings=config(switch_tenant_id=tenant,local_embeddings_enabled=True,switch_reply_to_discord=True,
        supabase_url='https://fixture.invalid',supabase_service_role_key='sb_secret_fixture_not_real',
        automatic_commitments_enabled=False,commitment_timezone='UTC')
    posted=[];calls=[]
    async def mocked_ai(*args,**kwargs):
        calls.append(True);return '{"action":"store_only","commitments":[{"description":"finish report","owner_name":"Wrong label"}]}'
    async def post(self,room_id,body,thread_id=None):posted.append(body)
    monkeypatch.setattr(ai_service,'ask_switch',mocked_ai);monkeypatch.setattr(ai_service.SwitchAPI,'post_room_message',post)
    async with httpx.AsyncClient(transport=transport(db,tenant,room)) as client:
        with use_http_clients(SimpleNamespace(database=client)):
            service=ai_service.AIService()
            incoming=message(settings,"@bot I’ll finish the report by Friday.",addressed=True,sender='unknown-switch-sender',sender_is_bot=None)
            receipt=await service.ingest(incoming,settings,MessageReceiptRepository());await service.queue.join()
            assert (await service.journal.get(receipt.message_ref))['state']=='done'
            assert posted[-1]=='Task saved in Supabase. Search indexing is pending.'
            tasks=db.query('SELECT * FROM public.commitments');sources=db.query('SELECT * FROM public.messages');memories=db.query('SELECT * FROM public.memories')
            assert len(tasks)==len(sources)==len(memories)==1
            assert tasks[0]['owner_switch_user_id']=='unknown-switch-sender' and tasks[0]['due_at'] is None
            assert tasks[0]['approval_status']=='pending' and tasks[0]['approval_at'] is None
            assert sources[0]['body']==incoming.body and sources[0]['switch_author_id']==incoming.sender
            assert memories[0]['message_id']==tasks[0]['message_id']==sources[0]['id']
            assert 'Deadline wording: Friday' in memories[0]['content']
            assert memories[0]['embedding'] is None and memories[0]['embedding_status']=='pending'
            assert not db.query('SELECT * FROM public.room_members')
            assert (await service.ingest(incoming,settings,MessageReceiptRepository())).duplicate
            assert len(db.query('SELECT id FROM public.commitments'))==len(db.query('SELECT id FROM public.memories'))==1
            real=get_embedder()
            async def fail(texts):raise EmbeddingError('inference_failed')
            monkeypatch.setattr(embedding_service,'get_embedder',lambda:SimpleNamespace(embed=fail))
            worker=embedding_service.EmbeddingWorker();assert await worker.process_once(settings)==1
            assert db.query('SELECT embedding_status FROM public.memories')[0]['embedding_status']=='failed'
            assert len(db.query('SELECT id FROM public.commitments'))==1
            db.query("UPDATE public.memories SET embedding_next_attempt_at=now()-interval '1 second'")
            monkeypatch.setattr(embedding_service,'get_embedder',lambda:real)
            assert await worker.process_once(settings)==1
            state=db.query('SELECT embedding_status,embedding_content_hash=content_hash valid,extensions.vector_dims(embedding) dimensions FROM public.memories')[0]
            assert state=={'embedding_status':'completed','valid':True,'dimensions':384}
            decision=LLMDecision(action='store_only',memories=[{'content':'The cafeteria serves noodles and soup for lunch.'}])
            source={'switch_message_id':'food','body':'The cafeteria serves noodles and soup for lunch.','occurred_at':'2026-11-05T10:00:00Z'}
            db.query('SELECT public.store_message_extraction($1::uuid,$2,$3::jsonb,$4::jsonb)',tenant,'test-room',json.dumps(source),json.dumps(storage_items(decision,settings)))
            assert await worker.process_once(settings)==1
            result=await retrieval_service.query_team_memory(settings,search_query='Who promised to complete the written report and when?',switch_room_id='test-room')
            assert result['retrieval'][0]['mode']=='semantic' and result['memories'][0]['message_id']==sources[0]['id']
            vector=(await real.embed(['Who promised to complete the written report?']))[0]
            assert not db.query('SELECT * FROM public.search_memories($1::uuid,$2::uuid,$3::extensions.vector,5)',str(uuid4()),room,json.dumps(vector))
            assert len(calls)==1
            await service.close()

def test_atomic_capture_rollback_privileges_and_replay(db):
    tenant,room=seed(db)
    db.query('SET ROLE service_role')
    source={'switch_message_id':'fixture','switch_author_id':'unknown-sender','author_name':'Label','body':'@bot I will finish report.','occurred_at':'2026-11-05T10:00:00Z'}
    items={'commitments':[{'extraction_key':'capture-fixture','description':'finish report','owner_switch_user_id':'attacker','due_at':None}],
        'decisions':[],'memories':[{'extraction_key':'capture-fixture','memory_type':'invalid','content':'Fixture'}]}
    def store():return db.query('SELECT public.store_message_extraction($1::uuid,$2,$3::jsonb,$4::jsonb,true) result',tenant,'test-room',json.dumps(source),json.dumps(items))
    with pytest.raises(ValueError):store()
    assert not db.query('SELECT id FROM public.messages') and not db.query('SELECT id FROM public.commitments')
    # Fail after the original RPC and owner initialization, inside the wrapper.
    items['memories'][0]['memory_type']='fact'
    with pytest.raises(ValueError):store()
    assert not db.query('SELECT id FROM public.messages') and not db.query('SELECT id FROM public.memories')
    items['memories'][0]['memory_type']='commitment'
    first=store();assert store()==first
    assert db.query('SELECT owner_switch_user_id FROM public.commitments')[0]['owner_switch_user_id']=='unknown-sender'
    assert len(db.query('SELECT id FROM public.messages'))==len(db.query('SELECT id FROM public.memories'))==1
    source['switch_author_id']='attacker'
    with pytest.raises(ValueError):store()
    assert db.query('SELECT owner_switch_user_id FROM public.commitments')[0]['owner_switch_user_id']=='unknown-sender'
    assert db.query("SELECT has_function_privilege('anon','public.store_message_extraction(uuid,text,jsonb,jsonb,boolean)','EXECUTE') allowed")[0]['allowed'] is False

@pytest.mark.anyio
async def test_committed_capture_recovers_after_lost_ack_and_restart(db,monkeypatch):
    tenant,room=seed(db);db.query('SET ROLE service_role')
    settings=config(switch_tenant_id=tenant,switch_reply_to_discord=True,supabase_url='https://fixture.invalid',
        supabase_service_role_key='sb_secret_fixture_not_real',automatic_commitments_enabled=False)
    calls=[];posted=[]
    async def ask(*args,**kwargs):
        calls.append(True);return '{"action":"store_only","commitments":[{"description":"finish report"}]}'
    async def post(self,room_id,body,thread_id=None):posted.append(body)
    monkeypatch.setattr(ai_service,'ask_switch',ask);monkeypatch.setattr(ai_service.SwitchAPI,'post_room_message',post)
    async with httpx.AsyncClient(transport=transport(db,tenant,room,lose_first_ack=True)) as client:
        with use_http_clients(SimpleNamespace(database=client)):
            first=ai_service.AIService()
            incoming=message(settings,"@bot I'll finish report by Friday.",addressed=True,sender='fresh-sender')
            receipt=await first.ingest(incoming,settings,MessageReceiptRepository());await first.queue.join()
            assert (await first.journal.get(receipt.message_ref))['state']=='failed'
            assert not any('saved' in text.lower() for text in posted)
            assert len(db.query('SELECT id FROM public.commitments'))==1
            await first.close()
            restarted=ai_service.AIService();await restarted.journal.retry(receipt.message_ref)
            await restarted._enqueue_durable(await restarted.journal.get(receipt.message_ref),settings);await restarted.queue.join()
            assert (await restarted.journal.get(receipt.message_ref))['state']=='done'
            assert posted[-1]=='Task saved in Supabase. Search indexing is pending.'
            assert len(db.query('SELECT id FROM public.messages'))==len(db.query('SELECT id FROM public.commitments'))==len(db.query('SELECT id FROM public.memories'))==1
            assert db.query('SELECT embedding_attempts FROM public.memories')[0]['embedding_attempts']==0
            assert len(calls)==1
            await restarted.close()
