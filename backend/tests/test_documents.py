"""Independent document domain/API tests. No hosted writes or chat delivery."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.controllers import documents as routes
from app.core.config import Settings, get_settings
from app.core.embedding_model import EmbeddingError, get_embedder, RECIPE
from app.main import app
from app.models.documents import DocumentScope, DocumentSearch, ImportMetadata, MAX_UPLOAD_BYTES
from app.repositories.document_memory import MemoryDocumentRepository
from app.repositories.document_repository import SupabaseDocumentRepository
from app.services.document_chunker import chunk_text, digest, local_chunks
from app.services.document_service import DocumentService
from app.services.document_worker import DocumentWorker

SCOPE = DocumentScope('00000000-0000-0000-0000-000000000001',('00000000-0000-0000-0000-000000000002',))
OTHER = DocumentScope('00000000-0000-0000-0000-000000000003',('00000000-0000-0000-0000-000000000004',))
UNIT = [1.0]+[0.0]*383


class CharacterTokenizer:
    def encode(self,text,add_special_tokens=True):
        return SimpleNamespace(ids=[1]*(len(text)+(2 if add_special_tokens else 0)),
                               offsets=[(i,i+1) for i in range(len(text))])


async def fake_chunks(text):
    return chunk_text(text,CharacterTokenizer())


class FakeEmbedder:
    async def embed(self,texts):
        return [UNIT[:] for _ in texts]


def metadata(expected=0,source='project',**values):
    return ImportMetadata(import_id=values.pop('import_id',uuid4()),source_key=source,
        filename=values.pop('filename','project.md'),title='Project',expected_version=expected,**values)


def domain(clock=lambda:1000):
    repository=MemoryDocumentRepository(clock)
    service=DocumentService(repository,fake_chunks,FakeEmbedder())
    worker=DocumentWorker(repository,FakeEmbedder())
    return repository,service,worker


def test_chunking_exact_slices_determinism_limits_overlap_and_coverage():
    text='# Main\n\n'+('Unicode café 🚀 text. '*90)+'\n\n## Next\n\n'+('word'*220)+'\n'
    pieces=chunk_text(text,CharacterTokenizer())
    assert pieces==chunk_text(text,CharacterTokenizer())
    assert len(pieces)>3 and pieces[0]['start_char']==0 and pieces[-1]['end_char']==len(text)
    for i,piece in enumerate(pieces):
        assert text[piece['start_char']:piece['end_char']]==piece['text']
        assert digest(piece['text'])==piece['content_hash']
        assert len(CharacterTokenizer().encode(piece['text']).ids)<=512
        assert piece['start_line']==text[:piece['start_char']].count('\n')+1
        assert piece['end_line']==text[:piece['end_char']-1].count('\n')+1
        if i:
            previous=pieces[i-1]
            assert previous['start_char']<piece['start_char']<=previous['end_char']
            assert previous['end_char']-piece['start_char']<=32
    assert any(p['section']=='Next' for p in pieces)


@pytest.mark.parametrize('raw,filename,status',[(b'bad\xff','x.txt',422),(b'\x00text','x.txt',422),
    (b'   ','x.txt',422),(b'text','x.pdf',422),(b'x'*(MAX_UPLOAD_BYTES+1),'x.md',413)])
def test_input_limits(raw,filename,status):
    async def run():
        repo,service,_=domain()
        with pytest.raises(HTTPException) as error:
            await service.import_bytes(SCOPE,metadata(filename=filename),raw)
        assert error.value.status_code==status and not repo.documents
    asyncio.run(run())


def test_import_publish_search_citation_and_source_inspection():
    async def run():
        repo,service,worker=domain()
        raw=Path(__file__).with_name('fixtures').joinpath('documents/project.md').read_bytes()
        result=await service.import_bytes(SCOPE,metadata(),raw)
        assert result.status=='processing'
        assert await service.search(SCOPE,DocumentSearch(query='API framework?'))==[]
        assert await worker.process_once(SCOPE)>0
        rows=await service.search(SCOPE,DocumentSearch(query='API framework?'))
        row=rows[0]
        assert row.source_type=='document' and str(row.document_id)==str(result.document_id)
        assert row.citation==f'document:{result.document_id}:{result.version_id}:{row.chunk_id}'
        chunk=repo.chunks[str(row.chunk_id)]
        version=repo.versions[str(result.version_id)]
        assert version['source_text']==raw.decode()
        assert row.text==version['source_text'][chunk['start_char']:chunk['end_char']]
        assert await service.search(OTHER,DocumentSearch(query='API framework?'))==[]
    asyncio.run(run())


def test_identity_deduplication_import_conflicts_and_distinct_provenance():
    async def run():
        repo,service,worker=domain()
        request=metadata()
        first=await service.import_bytes(SCOPE,request,b'Stored evidence')
        replay=await service.import_bytes(SCOPE,request,b'Stored evidence')
        unchanged=await service.import_bytes(SCOPE,metadata(1),b'Stored evidence')
        assert replay.duplicate and unchanged.duplicate
        assert first.version_id==unchanged.version_id and len(repo.chunks)==1
        separate=await service.import_bytes(SCOPE,metadata(source='other'),b'Stored evidence')
        assert separate.document_id!=first.document_id and len(repo.chunks)==2
        with pytest.raises(HTTPException) as error:
            await service.import_bytes(SCOPE,request,b'Changed evidence')
        assert error.value.status_code==409
        with pytest.raises(HTTPException):
            await service.import_bytes(SCOPE,metadata(0),b'Changed evidence')
        update=await service.import_bytes(SCOPE,metadata(1),b'Changed evidence')
        assert update.document_id==first.document_id and update.version_number==2
    asyncio.run(run())


def test_replacement_keeps_old_searchable_rejects_stale_workers_and_archives():
    async def run():
        repo,service,worker=domain()
        first=await service.import_bytes(SCOPE,metadata(),b'Original published source')
        await worker.process_once(SCOPE)
        second=await service.import_bytes(SCOPE,metadata(1),b'Pending replacement source')
        stale=(await repo.call(SCOPE,'claim',{}))[0]
        hits=await service.search(SCOPE,DocumentSearch(query='source'))
        assert str(hits[0].version_id)==str(first.version_id)
        third=await service.import_bytes(SCOPE,metadata(2),b'Newest replacement source')
        assert not await repo.call(SCOPE,'finish',dict(id=stale['id'],claim_token=stale['claim_token'],
            content_hash=stale['content_hash'],model_recipe=RECIPE,vector=UNIT,error=None))
        await worker.process_once(SCOPE)
        assert str((await service.search(SCOPE,DocumentSearch(query='source')))[0].version_id)==str(third.version_id)
        await repo.call(SCOPE,'archive',dict(document_id=str(first.document_id),expected_version=3))
        assert await service.search(SCOPE,DocumentSearch(query='source'))==[]
        assert len(repo.versions)==3 and repo.versions[str(second.version_id)]['source_text']=='Pending replacement source'
    asyncio.run(run())


def test_concurrent_imports_compare_expected_version():
    async def run():
        repo,service,worker=domain()
        results=await asyncio.gather(service.import_bytes(SCOPE,metadata(),b'A'),
                                     service.import_bytes(SCOPE,metadata(),b'B'),return_exceptions=True)
        assert sum(isinstance(r,HTTPException) and r.status_code==409 for r in results)==1
        assert len(repo.documents)==len(repo.versions)==len(repo.chunks)==1
    asyncio.run(run())


def test_partial_failure_retry_restart_expired_lease_and_wrong_token():
    async def run():
        now=[1000]
        repo,service,worker=domain(lambda:now[0])
        first=await service.import_bytes(SCOPE,metadata(),b'word '*140)
        jobs=await repo.call(SCOPE,'claim',{})
        assert len(jobs)>1 and await repo.call(SCOPE,'claim',{})==[]
        job=jobs[0]
        data=dict(id=job['id'],claim_token=job['claim_token'],content_hash=job['content_hash'],model_recipe=RECIPE,vector=UNIT,error=None)
        assert not await repo.call(SCOPE,'finish',dict(data,claim_token=str(uuid4())))
        assert await repo.call(SCOPE,'finish',dict(data,vector=None,error='inference_failed'))
        assert repo.documents[str(first.document_id)]['published_version_id'] is None
        now[0]+=301
        restarted=DocumentWorker(repo,FakeEmbedder())
        await restarted.process_once(SCOPE)
        assert repo.documents[str(first.document_id)]['published_version_id']==str(first.version_id)
        assert not await repo.call(SCOPE,'finish',data)
        assert all(c['attempts']==2 for c in repo.chunks.values())
    asyncio.run(run())


def test_exhausted_processing_preserves_source_and_previous_publication():
    class Broken:
        async def embed(self,texts):
            raise EmbeddingError('inference_failed')
    async def run():
        now=[1000]
        repo,service,worker=domain(lambda:now[0])
        first=await service.import_bytes(SCOPE,metadata(),b'Published')
        await worker.process_once(SCOPE)
        replacement=await service.import_bytes(SCOPE,metadata(1),b'Cannot embed yet')
        broken=DocumentWorker(repo,Broken())
        for _ in range(3):
            await broken.process_once(SCOPE)
            now[0]+=31
        assert await broken.process_once(SCOPE)==0
        assert repo.versions[str(replacement.version_id)]['status']=='failed'
        assert repo.versions[str(replacement.version_id)]['source_text']=='Cannot embed yet'
        assert str((await service.search(SCOPE,DocumentSearch(query='source')))[0].version_id)==str(first.version_id)
    asyncio.run(run())


@pytest.mark.parametrize('operation,data',[('document',{'document_id':str(uuid4())}),
    ('archive',{'document_id':str(uuid4()),'expected_version':1})])
def test_inaccessible_documents_do_not_reveal_existence(operation,data):
    async def run():
        repo,service,worker=domain()
        created=await service.import_bytes(SCOPE,metadata(),b'Team secret source')
        with pytest.raises(HTTPException) as error:
            await repo.call(OTHER,operation,dict(data,document_id=str(created.document_id)))
        assert error.value.status_code==404
    asyncio.run(run())


def test_api_admin_authentication_disabled_flag_and_room_authorization(monkeypatch):
    monkeypatch.setenv('ADMIN_API_TOKEN','fixture-document-admin')
    get_settings.cache_clear()
    with TestClient(app) as client:
        for path in ('','/imports','/search','/'+str(uuid4())):
            method='post' if path in ('/imports','/search') else 'get'
            assert getattr(client,method)('/api/v1/documents'+path).status_code==401
            assert getattr(client,method)('/api/v1/documents'+path,headers={'X-Ingest-Token':'test-ingest-token'}).status_code==401
        response=client.get('/api/v1/documents?room_id=test-room',headers={'X-Admin-Token':'fixture-document-admin'})
        assert response.status_code==503 and response.headers['cache-control']=='no-store'
        monkeypatch.setenv('DOCUMENT_INGESTION_ENABLED','true')
        get_settings.cache_clear()
        denied=client.get('/api/v1/documents?room_id=forbidden',headers={'X-Admin-Token':'fixture-document-admin'})
        assert denied.status_code==403


def test_api_import_version_chunks_status_and_archive(monkeypatch):
    repo,service,worker=domain()
    monkeypatch.setenv('ADMIN_API_TOKEN','fixture-document-admin')
    get_settings.cache_clear()
    app.dependency_overrides[routes.document_scope]=lambda:SCOPE
    app.dependency_overrides[routes.document_repository]=lambda:repo
    monkeypatch.setattr(routes,'DocumentService',lambda repository:service)
    try:
        with TestClient(app) as client:
            client.headers['X-Admin-Token']='fixture-document-admin'
            params=metadata().model_dump(mode='json')
            response=client.post('/api/v1/documents/imports',params=params,content=b'# Project\n\nExact source')
            assert response.status_code==202, response.text
            result=response.json()
            root='/api/v1/documents/'+result['document_id']
            version=root+'/versions/'+result['version_id']
            assert client.get(root).json()['latest_version']==1
            assert client.get(version).json()['status']=='processing'
            assert client.get(root+'/versions').json()[0]['source_text']=='# Project\n\nExact source'
            assert client.get(version+'/chunks').json()[0]['text']=='# Project\n\nExact source'
            status=client.get('/api/v1/documents/imports/'+result['import_id'])
            assert status.status_code==200 and status.json()['status']=='processing'
            asyncio.run(worker.process_once(SCOPE))
            assert client.get('/api/v1/documents/imports/'+result['import_id']).json()['status']=='completed'
            hit=client.post('/api/v1/documents/search',json={'query':'Project source'}).json()[0]
            citation=client.get(version+'/chunks/'+hit['chunk_id'])
            assert citation.status_code==200 and citation.json()['text']==hit['text']
            assert client.get('/api/v1/documents?limit=51').status_code==422
            assert client.post('/api/v1/documents/imports',params=params,content=b'x'*(MAX_UPLOAD_BYTES+1)).status_code==413
            assert client.post(root+'/archive?expected_version=0').status_code==422
            assert client.post(root+'/archive?expected_version=1').json()['active'] is False
    finally:
        app.dependency_overrides.clear()


def test_import_admission_is_bounded_and_busy_request_is_rejected():
    async def run():
        from app.services import document_service
        repo,service,worker=domain()
        # Occupy the bounded import slots; no source or job may be accepted.
        async with document_service._import_slots:
            async with document_service._import_slots:
                with pytest.raises(HTTPException) as error:
                    await service.import_bytes(SCOPE,metadata(),b'Evidence')
                assert error.value.status_code==429 and not repo.documents
    asyncio.run(run())


def test_exhausted_claim_lease_marks_version_failed_without_losing_source():
    async def run():
        now=[1000]
        repo,service,worker=domain(lambda:now[0])
        result=await service.import_bytes(SCOPE,metadata(),b'Preserved after worker crash')
        for _ in range(3):
            assert len(await repo.call(SCOPE,'claim',{}))==1
            now[0]+=301
        assert await repo.call(SCOPE,'claim',{})==[]
        version=repo.versions[str(result.version_id)]
        assert version['status']=='failed' and version['error']=='lease_exhausted'
        assert version['source_text']=='Preserved after worker crash'
    asyncio.run(run())


def test_background_worker_automatically_publishes_and_recovers_pending_versions(monkeypatch):
    from app.services import document_worker
    async def rooms(*args,**kwargs):
        return SCOPE
    monkeypatch.setattr(document_worker,'resolve_document_scope',rooms)
    async def run():
        repo,service,_=domain()
        settings=Settings(_env_file=None).model_copy(update={
            'document_ingestion_enabled':True,'local_embeddings_enabled':True,
            'supabase_url':'https://fixture.invalid','supabase_service_role_key':'fixture-key',
            'switch_tenant_id':SCOPE.tenant_id})
        first=await service.import_bytes(SCOPE,metadata(),b'First published evidence')
        async def automatic(version):
            worker=DocumentWorker(repo,FakeEmbedder(),settings_provider=lambda:settings)
            worker.start()
            try:
                async with asyncio.timeout(3):
                    while repo.documents[str(first.document_id)]['published_version_id']!=str(version.version_id):
                        await asyncio.sleep(.01)
            finally:
                await worker.close()
            assert worker.task.done()
        await automatic(first)
        pending=await service.import_bytes(SCOPE,metadata(1),b'Pending evidence after restart')
        await automatic(pending)
        assert repo.versions[str(pending.version_id)]['status']=='completed'
    asyncio.run(run())


@pytest.mark.parametrize('rows', [[],[{'id':SCOPE.room_ids[0],'tenant_id':OTHER.tenant_id,'enabled':True}],
                                 [{'id':SCOPE.room_ids[0],'tenant_id':SCOPE.tenant_id,'enabled':False}]])
def test_document_scope_checks_never_provision_missing_or_disabled_rooms(rows):
    from app.services.document_authorization import resolve_document_scope
    async def run():
        methods=[]
        settings=Settings(_env_file=None).model_copy(update={'switch_tenant_id':SCOPE.tenant_id,
            'switch_room_ids':'test-room','supabase_url':'https://fixture.invalid','supabase_service_role_key':'fixture-key'})
        async def transport(request):
            methods.append(request.method)
            return httpx.Response(200,json=rows)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            with pytest.raises(HTTPException) as error:
                await resolve_document_scope(settings,'test-room',client=client)
            assert error.value.status_code==403
        assert methods==['GET']
    asyncio.run(run())


def test_hosted_fixture_missing_schema_preflight_never_creates_rows(monkeypatch):
    from app import verify_document_fixture as fixture
    settings=Settings(_env_file=None,supabase_url='https://fixture.invalid',supabase_service_role_key='fixture-key',admin_api_token='fixture-admin')
    monkeypatch.setattr(fixture,'Settings',lambda:settings)
    methods=[]
    async def transport(request):
        methods.append(request.method)
        return httpx.Response(404,json={'code':'PGRST205'})
    real_client=httpx.AsyncClient
    monkeypatch.setattr(fixture.httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(transport),**kwargs))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(fixture.verify())
    assert methods==['GET']


def test_supabase_adapter_scopes_calls_and_sanitizes_errors():
    async def run():
        settings=Settings(_env_file=None,supabase_url='https://fixture.invalid',supabase_service_role_key='fixture-key')
        repo=SupabaseDocumentRepository()
        repo.settings=lambda:settings
        async def transport(request):
            payload=json.loads(request.content)
            assert payload['p_tenant_id']==SCOPE.tenant_id and payload['p_room_ids']==list(SCOPE.room_ids)
            return httpx.Response(409,json={'code':'40001','message':'fixture-sensitive-error'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            with pytest.raises(HTTPException) as error:
                await repo.call(SCOPE,'document',{},client=client)
        assert error.value.status_code==409 and error.value.detail=='document_version_conflict'
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get('RUN_LOCAL_MODEL_TESTS')!='1',reason='Opt-in real local tokenizer/model')
def test_real_model_import_publication_paraphrase_and_citation():
    async def run():
        repo=MemoryDocumentRepository()
        model=get_embedder()
        service=DocumentService(repo,local_chunks,model)
        worker=DocumentWorker(repo,model)
        raw=Path(__file__).with_name('fixtures').joinpath('documents/project.md').read_bytes()
        result=await service.import_bytes(SCOPE,metadata(),raw)
        unrelated=await service.import_bytes(SCOPE,metadata(source='lunch'),b'The cafeteria serves noodles for lunch.')
        await worker.process_once(SCOPE)
        hits=await service.search(SCOPE,DocumentSearch(query='Which framework powers the web API?'))
        assert str(hits[0].document_id)==str(result.document_id)
        assert hits[0].similarity>hits[-1].similarity
        assert hits[0].text in raw.decode() and hits[0].citation.startswith('document:')
        for chunk in repo.chunks.values():
            assert len(model.tokenizer.encode(chunk['text']).ids)<=512
            assert len(chunk['embedding'])==384
        long='# Long section\n\n'+('implementation details, naïve café 🚀 '*1200)
        pieces=await local_chunks(long)
        assert pieces[0]['start_char']==0 and pieces[-1]['end_char']==len(long)
        for piece in pieces:
            assert piece['text']==long[piece['start_char']:piece['end_char']]
            assert len(model.tokenizer.encode(piece['text']).ids)<=512
        for spaced in (' '*18000+'Source evidence', 'Source evidence'+' '*18000,
                       'First source'+' '*18000+'Second source'):
            slices=await local_chunks(spaced)
            assert slices[0]['start_char']==0 and slices[-1]['end_char']==len(spaced)
            for i,piece in enumerate(slices):
                assert piece['text']==spaced[piece['start_char']:piece['end_char']]
                assert len(model.tokenizer.encode(piece['text']).ids)<=512
                if i:
                    assert piece['start_char']<=slices[i-1]['end_char']
        assert get_embedder().session is model.session
    asyncio.run(run())
