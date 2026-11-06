"""Real SQLite consent/outbox; mocked Switch and document store. No live messages."""

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import UUID
from pydantic import SecretStr

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from app.controllers import document_attachments as routes
from app.core.config import Settings, get_settings
from app.main import app
from app.models.document_attachments import AttachmentMessage, AttachmentGroup
from app.models.documents import DocumentScope, DocumentSearch
from app.repositories.document_attachment_jobs import AttachmentJobs
from app.repositories.document_memory import MemoryDocumentRepository
from app.services.document_attachments import DocumentAttachments
from app.services.document_chunker import digest
from app.services.document_service import DocumentService
from app.services.document_worker import DocumentWorker
from app.services.message_service import message_reference
from app.services.switch_document_media import AttachmentFailure, SwitchDocumentTransport
from app.services.switch_listener import SwitchListener
from app.core.embedding_model import get_embedder, EmbeddingError

TENANT='00000000-0000-0000-0000-000000000001'
ROOM='00000000-0000-0000-0000-000000000002'
SENDER='@fixture-human:switch.invalid'
VECTOR=[1.0]+[0.0]*383


def configuration(**changes):
    values=dict(
        switch_tenant_id=UUID(TENANT),switch_room_ids='test-room,second-room',
        switch_document_attachments_enabled=True,document_ingestion_enabled=True,local_embeddings_enabled=True,
        switch_document_adapter_token=SecretStr('fixture-adapter-token'),
        switch_document_discord_bridge_ids='fixture-bridge',
        switch_document_bot_sender_id='@fixture-client:switch.invalid',
        admin_api_token='fixture-admin',switch_bridge_url='https://switch.invalid',
        switch_document_confirmation_seconds=60)
    values.update(changes)
    return Settings(_env_file=None).model_copy(update=values)


def attachment(filename='design.md',mxc='mxc://switch.invalid/file_1',**changes):
    return dict(filename=filename,mxc=mxc,mimetype='text/markdown',size=0,**changes)


def event(**changes):
    return AttachmentMessage(**dict(dict(tenant_id=TENANT,room_id='test-room',message_id='source-1',
        sender=SENDER,sender_kind='user',body='',attachments=[attachment()],bridge_id='fixture-bridge'),**changes))


async def scope(settings,room):
    if room not in settings.allowed_switch_rooms:
        raise HTTPException(403,'scope_denied')
    return DocumentScope(str(settings.switch_tenant_id),(ROOM,))


class Embedder:
    async def embed(self,texts):
        return [VECTOR[:] for _ in texts]


async def chunks(text):
    return [dict(position=0,text=text,content_hash=digest(text),start_char=0,end_char=len(text),
                 start_line=1,end_line=text.count('\n')+1,section=None)]


class Transport:
    def __init__(self):
        self.downloads=[]
        self.posts=[]
        self.download_error=None
        self.post_error=False
        self.raw=b'The backend API uses FastAPI.'

    async def download(self,room,item):
        self.downloads.append((room,item['mxc']))
        if self.download_error:
            raise self.download_error
        return self.raw

    async def post(self,room,body,thread):
        self.posts.append((room,body,thread))
        if self.post_error:
            raise RuntimeError('fixture-sensitive-error')


def setup(tmp_path):
    now=[1000.0]
    jobs=AttachmentJobs(tmp_path/'attachments.sqlite3',lambda:now[0])
    repo=MemoryDocumentRepository(lambda:now[0])
    transport=Transport()
    factory=lambda repository:DocumentService(repository,chunks,Embedder())
    feature=DocumentAttachments(jobs,repo,scope,lambda settings:transport,factory)
    return SimpleNamespace(now=now,jobs=jobs,repo=repo,transport=transport,feature=feature,
        settings=configuration(),factory=factory,worker=DocumentWorker(repo,Embedder()))


def confirm(ref,decision='save',**changes):
    return event(message_id='decision-'+decision,attachments=[],body=decision+' '+ref,**changes)


async def publish(f):
    await f.worker.process_once(DocumentScope(TENANT,(ROOM,)))
    f.now[0]+=3
    await f.feature.work_once(f.settings)


def test_confirm_import_publish_search_citation_and_provenance(tmp_path):
    async def run():
        f=setup(tmp_path)
        received=await f.feature.accept(event(thread_id='thread-root'),f.settings)
        assert received.processing_state=='confirmation_requested'
        assert f.transport.downloads==[] and f.repo.documents=={}
        assert await f.feature.send_once(f.settings)
        assert 'design.md' in f.transport.posts[0][1] and 'save '+received.request_ref in f.transport.posts[0][1]
        assert f.transport.posts[0][2]=='thread-root'
        queued=await f.feature.accept(confirm(received.request_ref,thread_id='thread-root'),f.settings)
        assert queued.processing_state=='queued' and f.transport.downloads==[]
        await f.feature.work_once(f.settings)
        assert len(f.transport.downloads)==1 and len(f.repo.documents)==1
        assert next(iter(f.repo.documents.values()))['created_by']=='switch:'+SENDER
        assert (await f.jobs.get(received.request_ref))['state']=='processing'
        assert not any('Saved to searchable' in p[1] for p in f.transport.posts)
        await publish(f)
        assert (await f.jobs.get(received.request_ref))['state']=='published'
        for _ in range(5):
            await f.feature.send_once(f.settings)
        assert sum('Saved to searchable' in p[1] for p in f.transport.posts)==1
        matches=await f.factory(f.repo).search(DocumentScope(TENANT,(ROOM,)),DocumentSearch(query='API framework?'))
        assert matches[0].text=='The backend API uses FastAPI.' and matches[0].citation.startswith('document:')
        status=await f.feature.status(received.request_ref,f.settings)
        assert status.source_references[0]['message_id']=='source-1'
        assert status.source_references[0]['sender']==SENDER
        assert 'mxc' not in status.model_dump_json()
        replay=await f.feature.accept(confirm(received.request_ref,thread_id='thread-root'),f.settings)
        assert replay.duplicate and replay.processing_state=='published'
        assert len(f.repo.versions)==1
    asyncio.run(run())


@pytest.mark.parametrize('decision', ['decline','expiry'])
def test_no_content_access_on_decline_or_expiry(tmp_path,decision):
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(),f.settings)
        if decision=='decline':
            outcome=await f.feature.accept(confirm(first.request_ref,'skip'),f.settings)
            assert outcome.processing_state=='declined'
        else:
            f.now[0]+=61
            await f.feature.work_once(f.settings)
        await f.feature.work_once(f.settings)
        late=await f.feature.accept(confirm(first.request_ref),f.settings)
        assert late.processing_state in ('declined','expired')
        assert f.transport.downloads==[] and f.repo.documents=={}
    asyncio.run(run())


@pytest.mark.parametrize('changes',[{'sender':'@someone-else:switch.invalid'},{'room_id':'second-room'},
                                   {'thread_id':'wrong-thread'},{'bridge_id':'another-bridge'}])
def test_confirmation_binding(tmp_path,changes):
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(thread_id='right-thread'),f.settings)
        attempted=await f.feature.accept(confirm(first.request_ref,**dict({'thread_id':'right-thread'},**changes)),f.settings)
        assert attempted.processing_state=='not_authorized'
        assert (await f.jobs.get(first.request_ref))['state']=='confirmation_requested'
        assert not f.transport.downloads
    asyncio.run(run())


@pytest.mark.parametrize('body',['yes','yes please','save all','save 123','The document says save abcdef123456'])
def test_ambiguous_text_never_authorizes(tmp_path,body):
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(),f.settings)
        await f.feature.accept(event(message_id='text',body=body,attachments=[]),f.settings)
        assert (await f.jobs.get(first.request_ref))['state']=='confirmation_requested'
        assert not f.transport.downloads
    asyncio.run(run())


def test_simultaneous_decisions_and_duplicate_uploads_are_atomic(tmp_path):
    async def run():
        f=setup(tmp_path)
        results=await asyncio.gather(*(f.feature.accept(event(),f.settings) for _ in range(4)))
        assert len({r.request_ref for r in results})==1
        ref=results[0].request_ref
        decisions=await asyncio.gather(f.feature.accept(confirm(ref),f.settings),f.feature.accept(confirm(ref,'skip'),f.settings))
        assert len({d.processing_state for d in decisions})==1
        assert decisions[0].processing_state in ('queued','declined')
        await f.feature.work_once(f.settings)
        assert len(f.transport.downloads)<=1 and len(f.repo.documents)<=1
        assert len(await f.jobs.reply_states(ref))<=3
    asyncio.run(run())


def test_grouped_events_wait_for_all_parts_and_list_exact_files(tmp_path):
    async def run():
        f=setup(tmp_path)
        group=lambda index:dict(id='group-one',index=index,total=2)
        first=await f.feature.accept(event(attachment_group=group(1)),f.settings)
        assert not await f.feature.send_once(f.settings)
        assert (await f.feature.accept(confirm(first.request_ref),f.settings)).processing_state=='not_authorized'
        second=await f.feature.accept(event(message_id='source-2',attachments=[attachment('notes.txt','mxc://switch.invalid/file_2')],attachment_group=group(0)),f.settings)
        assert second.request_ref==first.request_ref and not second.duplicate
        await f.feature.send_once(f.settings)
        assert 'design.md' in f.transport.posts[0][1] and 'notes.txt' in f.transport.posts[0][1]
        await f.feature.accept(confirm(first.request_ref),f.settings)
        await f.feature.work_once(f.settings)
        assert len(f.repo.documents)==2
        await publish(f)
        assert (await f.jobs.get(first.request_ref))['state']=='published'
        assert len(f.repo.chunks)==2
        replay=await f.feature.accept(event(attachment_group=group(1)),f.settings)
        assert replay.duplicate and len(f.transport.downloads)==2
    asyncio.run(run())


@pytest.mark.parametrize('marker',[{'id':'x','index':True,'total':2},{'id':'x','index':2,'total':1},{}])
def test_malformed_group_degrades_to_single_event(marker):
    assert event(attachment_group=marker).attachment_group is None


@pytest.mark.parametrize('file,error',[(attachment('x.pdf'),'unsupported_format'),
    (attachment('../x.md'),'unsafe_filename'),(attachment('C:\\x.md'),'unsafe_filename'),
    (attachment(mxc='http://127.0.0.1/private'),'unsafe_media_reference'),
    (dict(attachment(),size=262145),'attachment_too_large')])
def test_rejected_metadata_never_downloads(tmp_path,file,error):
    async def run():
        f=setup(tmp_path)
        received=await f.feature.accept(event(attachments=[file]),f.settings)
        assert received.processing_state=='failed'
        assert (await f.jobs.get(received.request_ref))['data']['error']==error
        await f.feature.accept(confirm(received.request_ref),f.settings)
        await f.feature.work_once(f.settings)
        assert not f.transport.downloads and not f.repo.documents
    asyncio.run(run())


def test_file_count_and_invalid_sender_fail_closed(tmp_path):
    async def run():
        f=setup(tmp_path)
        too_many=await f.feature.accept(event(attachments=[attachment(mxc='mxc://switch.invalid/file_'+str(i)) for i in range(5)]),f.settings)
        assert too_many.processing_state=='failed'
        for i,changes in enumerate(({'sender':'display-name'},{'sender':f.settings.switch_document_bot_sender_id})):
            result=await f.feature.accept(event(message_id='source-'+str(i+10),**changes),f.settings)
            assert result.processing_state==('ignored' if changes['sender']==f.settings.switch_document_bot_sender_id else 'not_authorized')
            assert result.request_ref is None
        assert not f.transport.downloads
    asyncio.run(run())


def test_restart_recovers_consent_and_import_without_redownloading_committed_content(tmp_path,monkeypatch):
    async def run():
        f=setup(tmp_path)
        receipt=await f.feature.accept(event(),f.settings)
        restart=DocumentAttachments(AttachmentJobs(f.jobs.path,lambda:f.now[0]),f.repo,scope,lambda s:f.transport,f.factory)
        await restart.accept(confirm(receipt.request_ref),f.settings)
        async def lost_update(*args,**kwargs):
            raise RuntimeError('simulated_crash_after_document_commit')
        monkeypatch.setattr(restart.jobs,'change',lost_update)
        with pytest.raises(RuntimeError):
            await restart.work_once(f.settings)
        assert len(f.repo.documents)==1 and len(f.transport.downloads)==1
        f.now[0]+=301
        restart=DocumentAttachments(AttachmentJobs(f.jobs.path,lambda:f.now[0]),f.repo,scope,lambda s:f.transport,f.factory)
        await restart.work_once(f.settings)
        assert len(f.repo.documents)==1 and len(f.transport.downloads)==1
        f.feature=restart
        await publish(f)
        assert (await f.jobs.get(receipt.request_ref))['state']=='published'
    asyncio.run(run())


def test_confirmed_import_survives_live_only_listener_restart(tmp_path):
    async def run():
        f=setup(tmp_path)
        listener_path=tmp_path/'listener.json'
        listener=SwitchListener(f.settings,state_path=listener_path)
        listener.state.cursor,listener.state.generation=12,3
        listener.state.save(listener_path)
        receipt=await f.feature.accept(event(),f.settings)
        await f.feature.accept(confirm(receipt.request_ref),f.settings)
        assert (await f.jobs.get(receipt.request_ref))['state']=='queued'

        restarted_listener=SwitchListener(f.settings,state_path=listener_path)
        assert restarted_listener.state.cursor is None
        assert restarted_listener.state.connection_id!=listener.state.connection_id
        f.feature=DocumentAttachments(AttachmentJobs(f.jobs.path,lambda:f.now[0]),
            f.repo,scope,lambda settings:f.transport,f.factory)
        await f.feature.work_once(f.settings)
        await publish(f)
        assert (await f.jobs.get(receipt.request_ref))['state']=='published'
        assert len(f.repo.documents)==1 and len(f.transport.downloads)==1
    asyncio.run(run())


@pytest.mark.parametrize('error',[AttachmentFailure('media_unavailable'),AttachmentFailure('media_unavailable',True)])
def test_missing_media_and_bounded_download_retries(tmp_path,error):
    async def run():
        f=setup(tmp_path)
        f.transport.download_error=error
        received=await f.feature.accept(event(),f.settings)
        await f.feature.accept(confirm(received.request_ref),f.settings)
        for _ in range(4):
            await f.feature.work_once(f.settings)
            f.now[0]+=31
        row=await f.jobs.get(received.request_ref)
        assert row['state']=='failed' and row['data']['error']=='media_unavailable'
        assert len(f.transport.downloads)==(3 if error.retryable else 1)
        assert not f.repo.documents
        assert 'upload it again' in row['data']['replies']['failed']
    asyncio.run(run())


def test_durable_outbox_has_bounded_retries_and_does_not_change_consent(tmp_path):
    async def run():
        f=setup(tmp_path)
        f.settings=f.settings.model_copy(update={'switch_document_confirmation_seconds':900})
        received=await f.feature.accept(event(),f.settings)
        f.transport.post_error=True
        for _ in range(4):
            await f.feature.send_once(f.settings)
            f.now[0]+=31
        assert len(f.transport.posts)==3
        assert await f.jobs.reply_states(received.request_ref)==['failed']
        assert not f.transport.downloads
    asyncio.run(run())


def test_failed_new_upload_preserves_existing_published_document(tmp_path):
    class Broken:
        async def embed(self,texts):
            raise EmbeddingError('inference_failed')
    async def run():
        f=setup(tmp_path)
        original=await f.feature.accept(event(),f.settings)
        await f.feature.accept(confirm(original.request_ref),f.settings)
        await f.feature.work_once(f.settings)
        await publish(f)
        first=next(iter(f.repo.documents.values()))
        published=first['published_version_id']
        replacement=await f.feature.accept(event(message_id='new-source',attachments=[attachment(mxc='mxc://switch.invalid/new_file')]),f.settings)
        await f.feature.accept(confirm(replacement.request_ref),f.settings)
        await f.feature.work_once(f.settings)
        assert len(f.repo.documents)==2  # matching filenames cannot overwrite each other
        broken=DocumentWorker(f.repo,Broken())
        for _ in range(3):
            await broken.process_once(DocumentScope(TENANT,(ROOM,)))
            f.now[0]+=31
        await f.feature.work_once(f.settings)
        assert (await f.jobs.get(replacement.request_ref))['state']=='failed'
        assert first['published_version_id']==published
        hits=await f.factory(f.repo).search(DocumentScope(TENANT,(ROOM,)),DocumentSearch(query='backend?'))
        assert len(hits)==1 and str(hits[0].document_id)==first['id']
    asyncio.run(run())


def test_invalid_utf8_actual_size_and_text_limit_fail_after_consent_only(tmp_path):
    async def run():
        for index,raw in enumerate((b'bad\xff',b'x'*262145,b'%PDF-1.7 ASCII PDF content',b'\x00binary',b'')):
            f=setup(tmp_path/str(index))
            f.transport.raw=raw
            first=await f.feature.accept(event(),f.settings)
            assert f.transport.downloads==[]
            await f.feature.accept(confirm(first.request_ref),f.settings)
            await f.feature.work_once(f.settings)
            assert (await f.jobs.get(first.request_ref))['state']=='failed' and not f.repo.documents
        f=setup(tmp_path/'text')
        f.settings=f.settings.model_copy(update={'switch_document_max_text_chars':3})
        first=await f.feature.accept(event(),f.settings)
        await f.feature.accept(confirm(first.request_ref),f.settings)
        await f.feature.work_once(f.settings)
        assert (await f.jobs.get(first.request_ref))['state']=='failed'
    asyncio.run(run())


def test_stream_limit_applies_without_or_with_lying_content_length(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                yield b'x'*60000
    async def run():
        from contextlib import asynccontextmanager
        for headers in ({},{'content-length':'2'}):
            transport=SwitchDocumentTransport(configuration())
            @asynccontextmanager
            async def http():
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,headers=headers,stream=Stream()))) as client:
                    yield client
            monkeypatch.setattr(transport.api,'http',http)
            with pytest.raises(AttachmentFailure) as error:
                await transport.download('test-room',attachment())
            assert error.value.code=='attachment_too_large'
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get('RUN_LOCAL_MODEL_TESTS')!='1',reason='Opt-in actual local tokenizer/model')
def test_confirmed_attachment_real_local_embeddings_and_citation(tmp_path):
    async def run():
        f=setup(tmp_path)
        f.feature.service_factory=DocumentService
        f.worker=DocumentWorker(f.repo,get_embedder())
        first=await f.feature.accept(event(),f.settings)
        assert f.transport.downloads==[] and not f.repo.documents
        await f.feature.accept(confirm(first.request_ref),f.settings)
        await f.feature.work_once(f.settings)
        await publish(f)
        assert (await f.jobs.get(first.request_ref))['state']=='published'
        hits=await DocumentService(f.repo).search(DocumentScope(TENANT,(ROOM,)),DocumentSearch(query='Which framework powers our web backend?'))
        assert hits and hits[0].similarity>.5
        chunk=f.repo.chunks[str(hits[0].chunk_id)]
        assert len(chunk['embedding'])==384 and hits[0].text==f.transport.raw.decode()
        assert hits[0].citation==f'document:{hits[0].document_id}:{hits[0].version_id}:{hits[0].chunk_id}'
    asyncio.run(run())


def test_private_adapter_auth_and_admin_status(tmp_path,monkeypatch):
    f=setup(tmp_path)
    monkeypatch.setenv('ADMIN_API_TOKEN','fixture-admin')
    get_settings.cache_clear()
    with TestClient(app) as client:
        monkeypatch.setattr(routes,'get_settings',lambda:f.settings)
        app.state.document_attachments=f.feature
        path='/messages/document-attachments'
        payload=event().model_dump(mode='json')
        assert client.post(path,json=payload).status_code==401
        assert client.post(path,json=payload,headers={'X-Ingest-Token':'test-ingest-token'}).status_code==401
        assert client.post(path,json=payload,headers={'X-Admin-Token':'fixture-admin'}).status_code==401
        response=client.post(path,json=payload,headers={'X-Ingest-Token':'test-ingest-token','X-Document-Adapter-Token':'fixture-adapter-token'})
        assert response.status_code==200, response.text
        ref=response.json()['request_ref']
        status='/api/v1/document-attachments/'+ref
        assert client.get(status).status_code==401
        read=client.get(status,headers={'X-Admin-Token':'fixture-admin'})
        assert read.status_code==200 and read.json()['state']=='confirmation_requested'
        assert read.headers['cache-control']=='no-store'
        assert not f.transport.downloads


@pytest.mark.parametrize('code,headers,raw,error',[(404,{},b'','media_unavailable'),
    (403,{},b'','media_access_denied'),(302,{'location':'http://127.0.0.1/private'},b'','media_unavailable'),
    (200,{'content-length':'999999'},b'','attachment_too_large'),
    (200,{},b'x'*262145,'attachment_too_large')])
def test_media_transport_uses_only_switch_and_rejects_redirects_and_size(monkeypatch,code,headers,raw,error):
    async def run():
        transport=SwitchDocumentTransport(configuration())
        requests=[]
        def handle(request):
            requests.append(request)
            assert request.url.host=='switch.invalid'
            assert request.url.path=='/agents/test-agent/rooms/test-room/media'
            assert request.url.params['mxc']=='mxc://switch.invalid/file_1'
            assert request.headers['authorization']=='Bearer test-agent-key'
            return httpx.Response(code,headers=headers,content=raw)
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def http():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle),follow_redirects=False) as client:
                yield client
        monkeypatch.setattr(transport.api,'http',http)
        with pytest.raises(AttachmentFailure) as failure:
            await transport.download('test-room',attachment())
        assert failure.value.code==error and len(requests)==1
        with pytest.raises(AttachmentFailure):
            await transport.download('test-room',attachment(mxc='https://private.invalid/file'))
        assert len(requests)==1
    asyncio.run(run())


@pytest.mark.parametrize('sender_kind',[None,'user','platform'])
def test_listener_forwards_metadata_and_commands_without_downloading(tmp_path,sender_kind):
    async def run():
        listener=SwitchListener(configuration(),state_path=tmp_path/'listener.json')
        listener.state.cursor=0
        queue=asyncio.Queue()
        payload=event().model_dump(mode='json')
        payload.pop('tenant_id');payload.pop('room_id');payload.pop('attachment_group')
        payload['sender_kind']=sender_kind
        payload['content']={'sender_kind':'user','actor':SENDER}
        payload['com.switch.attachment_group']={'id':'group','index':0,'total':1}
        queue.put_nowait(('message',dict(room_id='test-room',payload=payload,bridge_id='fixture-bridge'),1))
        seen=[]
        def handle(request):
            seen.append(json.loads(request.content))
            assert request.url.path=='/messages/document-attachments'
            assert request.headers['x-document-adapter-token']=='fixture-adapter-token'
            return httpx.Response(200,json={'status':'received','message_ref':message_reference(event()),
                'received_at':'2026-11-05T00:00:00Z','duplicate':False,
                'processing_state':'confirmation_requested','request_ref':'abcdef123456'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle),base_url='http://fixture/') as client:
            task=asyncio.create_task(listener._forward_events(queue,client))
            async with asyncio.timeout(2):
                while listener.state.cursor!=1:
                    if task.done():
                        task.result()
                    await asyncio.sleep(.01)
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        assert seen[0]['attachments'][0]['mxc']=='mxc://switch.invalid/file_1'
        assert seen[0]['attachment_group']['id']=='group'
        assert seen[0]['bridge_id']=='fixture-bridge'
        assert seen[0]['sender_kind']==sender_kind  # Never promote missing metadata from content.
    asyncio.run(run())


def test_no_caption_or_signed_url_is_persisted_before_consent(tmp_path,caplog):
    async def run():
        f=setup(tmp_path)
        signed='https://private.invalid/file?signature=fixture-sensitive-url'
        received=await f.feature.accept(event(body='fixture-private-caption',attachments=[attachment(mxc=signed)]),f.settings)
        assert received.processing_state=='failed'
        stored=f.jobs.path.read_bytes()
        assert b'fixture-private-caption' not in stored and b'fixture-sensitive-url' not in stored
        assert 'fixture-sensitive-url' not in caplog.text and 'fixture-private-caption' not in caplog.text
        assert not f.transport.downloads
    asyncio.run(run())


def test_total_download_timeout_stops_slow_stream(monkeypatch):
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                await asyncio.sleep(.03)
                yield b'x'
    async def run():
        from contextlib import asynccontextmanager
        transport=SwitchDocumentTransport(configuration(switch_document_download_seconds=.02))
        @asynccontextmanager
        async def http():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Slow()))) as client:
                yield client
        monkeypatch.setattr(transport.api,'http',http)
        with pytest.raises(AttachmentFailure) as error:
            await transport.download('test-room',attachment())
        assert error.value.code=='media_unavailable' and error.value.retryable
    asyncio.run(run())


def test_unknown_bridge_is_rejected_without_a_confirmation_or_download(tmp_path):
    async def run():
        f=setup(tmp_path)
        received=await f.feature.accept(event(bridge_id=None),f.settings)
        assert received.request_ref is None and received.processing_state=='not_authorized'
        assert not await f.feature.send_once(f.settings) and not f.transport.downloads
    asyncio.run(run())


def test_slow_import_does_not_block_another_confirmation_prompt(tmp_path,monkeypatch):
    async def run():
        f=setup(tmp_path)
        f.feature.settings_provider=lambda:f.settings
        first=await f.feature.accept(event(),f.settings)
        await f.feature.accept(confirm(first.request_ref),f.settings)
        second=await f.feature.accept(event(message_id='second-upload',attachments=[attachment('second.txt','mxc://switch.invalid/file_2')]),f.settings)
        entered=asyncio.Event()
        block=asyncio.Event()
        async def slow(*args):
            entered.set()
            await block.wait()
            return b'File evidence'
        monkeypatch.setattr(f.transport,'download',slow)
        f.feature.start()
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                while not any(second.request_ref in p[1] and 'Should I save' in p[1] for p in f.transport.posts):
                    await asyncio.sleep(.01)
        finally:
            await f.feature.close()
        assert f.feature.task.done()
        assert (await f.jobs.get(second.request_ref))['state']=='confirmation_requested'
        assert not f.repo.documents
    asyncio.run(run())


def test_capacity_limits_and_expiry_do_not_discard_replay_history(tmp_path):
    async def run():
        f=setup(tmp_path)
        f.settings=f.settings.model_copy(update={'switch_document_max_pending':1,'switch_document_max_records':2})
        first=await f.feature.accept(event(),f.settings)
        with pytest.raises(HTTPException) as error:
            await f.feature.accept(event(message_id='second'),f.settings)
        assert error.value.status_code==429
        f.now[0]+=61
        await f.feature.work_once(f.settings)
        second=await f.feature.accept(event(message_id='second'),f.settings)
        await f.feature.accept(confirm(second.request_ref,'skip'),f.settings)
        with pytest.raises(HTTPException):
            await f.feature.accept(event(message_id='third'),f.settings)
        replay=await f.feature.accept(event(),f.settings)
        assert replay.request_ref==first.request_ref and replay.processing_state=='expired'
        assert not f.transport.downloads
    asyncio.run(run())


def test_uncertain_send_can_repeat_but_never_authorizes_import(tmp_path):
    async def run():
        f=setup(tmp_path)
        f.settings=f.settings.model_copy(update={'switch_document_confirmation_seconds':900})
        first=await f.feature.accept(event(),f.settings)
        claimed=await f.jobs.claim_reply(TENANT,f.settings.allowed_switch_rooms)
        await f.transport.post(claimed['room'],claimed['data']['body'],claimed['data']['thread_id'])
        # Simulate a crash after Switch accepted the post but before the local ack.
        f.now[0]+=61
        restart=DocumentAttachments(AttachmentJobs(f.jobs.path,lambda:f.now[0]),f.repo,scope,lambda s:f.transport,f.factory)
        await restart.send_once(f.settings)
        assert len(f.transport.posts)==2 and f.transport.posts[0]==f.transport.posts[1]
        assert not f.transport.downloads and not f.repo.documents
    asyncio.run(run())


def test_reply_attempt_uses_one_documented_operation_without_fallback(monkeypatch,tmp_path):
    async def run():
        from contextlib import asynccontextmanager
        from app.services.switch_api import SwitchError
        from app.repositories.listener_state import ListenerState,listener_identity,document_reply_session
        import app.services.switch_document_media as media
        settings=configuration()
        monkeypatch.setattr(media,'BACKEND_DIR',tmp_path)
        state=ListenerState(listener_identity(settings),'00000000-0000-0000-0000-000000000099',generation=7)
        state.save(tmp_path/'.local'/'switch-listener.json')
        transport=SwitchDocumentTransport(settings)
        paths=[]
        def handle(request):
            paths.append(request.url.path)
            body=json.loads(request.content)
            assert body=={'body':'Safe fixture reply','thread_id':'source-1'}
            assert request.headers['x-switch-connection-id']==state.connection_id
            assert request.headers['x-switch-session-id']==document_reply_session('test-room')
            return httpx.Response(500,json={'detail':'fixture-private-response'})
        @asynccontextmanager
        async def http():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                yield client
        monkeypatch.setattr(transport.api,'http',http)
        with pytest.raises(SwitchError):
            await transport.post('test-room','Safe fixture reply','source-1')
        assert paths==['/agents/test-agent/ops/post_message']
    asyncio.run(run())


def test_bot_replies_do_not_reenter_attachment_or_existing_ai_pipeline(tmp_path):
    from app.services.ai_service import AIService
    from app.repositories.message_receipts import MessageReceiptRepository
    async def run():
        f=setup(tmp_path)
        ai=AIService()
        try:
            bot=event(message_id='bot-echo',sender=f.settings.switch_document_bot_sender_id,
                      attachments=[],body='Should I save this document to team memory?')
            ignored=await f.feature.accept(bot,f.settings)
            assert ignored.request_ref is None
            receipt=await ai.ingest(bot,f.settings,MessageReceiptRepository())
            assert await ai.journal.get(receipt.message_ref) is None
            human=event(message_id='ordinary-human',attachments=[],body='Planning is Friday.')
            received=await ai.ingest(human,f.settings,MessageReceiptRepository())
            assert await ai.journal.get(received.message_ref) is not None
            assert not f.transport.downloads
        finally:
            await ai.close()
    asyncio.run(run())


def test_excessive_files_have_a_bounded_safe_rejection_reply(tmp_path):
    async def run():
        f=setup(tmp_path)
        files=[attachment('\U00010400'*180+'.md',f'mxc://switch.invalid/file_{i}') for i in range(30)]
        received=await f.feature.accept(event(attachments=files),f.settings)
        assert received.processing_state=='failed'
        await f.feature.send_once(f.settings)
        assert len(f.transport.posts)==1 and len(f.transport.posts[0][1].encode('utf-16-le'))//2<1900
        assert not f.transport.downloads
    asyncio.run(run())


@pytest.mark.parametrize('decision', ['save','skip'])
def test_unknown_person_needs_no_registration_and_owns_consent(tmp_path,decision):
    async def run():
        f=setup(tmp_path)
        person='@previously-unknown:switch.invalid'
        first=await f.feature.accept(event(sender=person,sender_name='Same Name'),f.settings)
        assert first.processing_state=='confirmation_requested'
        for wrong in (confirm(first.request_ref,sender=SENDER,sender_name='Same Name'),
                      confirm('abcdef012345',sender=person)):
            assert (await f.feature.accept(wrong,f.settings)).processing_state=='not_authorized'
        result=await f.feature.accept(confirm(first.request_ref,decision,sender=person),f.settings)
        assert result.processing_state==('queued' if decision=='save' else 'declined')
        await f.feature.work_once(f.settings)
        if decision=='save':
            await publish(f)
            assert (await f.jobs.get(first.request_ref))['state']=='published'
            assert next(iter(f.repo.documents.values()))['created_by']=='switch:'+person
        else:
            assert not f.transport.downloads and not f.repo.documents
    asyncio.run(run())


@pytest.mark.parametrize('kind', [None,'platform','agent','human'])
def test_upload_and_confirmation_do_not_require_sender_classification(tmp_path,kind):
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(sender_kind=kind),f.settings)
        assert first.processing_state=='confirmation_requested'
        attempted=await f.feature.accept(confirm(first.request_ref,sender_kind=kind),f.settings)
        assert attempted.processing_state=='queued'
        await f.feature.work_once(f.settings)
        await publish(f)
        assert (await f.jobs.get(first.request_ref))['state']=='published'
    asyncio.run(run())


def test_missing_or_user_supplied_actor_is_not_identity():
    from pydantic import ValidationError
    payload=event().model_dump()
    payload.pop('sender')
    with pytest.raises(ValidationError):
        AttachmentMessage(**payload)
    payload=event().model_dump()
    payload['actor']=SENDER
    with pytest.raises(ValidationError):
        AttachmentMessage(**payload)


def test_listener_binds_replies_to_its_existing_connection(tmp_path,monkeypatch):
    async def run():
        from app.repositories.listener_state import document_reply_session
        listener=SwitchListener(configuration(),state_path=tmp_path/'listener.json')
        listener.state.generation=7
        requests=[]
        def handle(request):
            requests.append(request)
            payload=json.loads(request.content)
            assert request.url.path=='/agents/test-agent/connection/placements'
            assert request.headers['authorization']=='Bearer test-agent-key'
            assert 'x-ingest-token' not in request.headers
            assert payload==dict(connection_id=listener.state.connection_id,generation=7,
                placements={document_reply_session(r):r for r in listener.settings.allowed_switch_rooms})
            return httpx.Response(200,json=dict(placements=payload['placements']))
        original=httpx.AsyncClient
        monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
        await listener.bind_document_reply_rooms()
        assert len(requests)==1
    asyncio.run(run())


def test_document_trace_logs_stages_without_private_payloads(tmp_path,caplog,monkeypatch):
    import logging
    monkeypatch.setattr(logging.getLogger('team_memory'),'propagate',True)
    caplog.set_level(logging.INFO)
    async def run():
        f=setup(tmp_path)
        result=await f.feature.accept(event(body='fixture-private-caption',sender_name='fixture-private-name',
            attachments=[attachment('fixture-private-file.pdf')]),f.settings)
        f.transport.post_error=True
        await f.feature.send_once(f.settings)
        assert result.processing_state=='failed'
        text=caplog.text
        for stage in ['stage=api_received','stage=metadata_rejected','stage=receipt','stage=reply_attempt','stage=reply_failed']:
            assert stage in text
        assert 'unsupported_format' in text and 'error_type=RuntimeError' in text
        for private in ['fixture-private-caption','fixture-private-name','fixture-private-file','fixture-sensitive-error','fixture-adapter-token','mxc://']:
            assert private not in text
    asyncio.run(run())


@pytest.mark.parametrize('reference,valid',[
    ('switch-media://'+'a'*32,True),
    ('mxc://switch.invalid/fixture_1',True),
    ('switch-media://'+ 'a'*31,False),
    ('switch-media://'+ 'g'*32,False),
    ('switch-media://'+ 'a'*32+'?token=fixture-secret',False),
    ('switch-media://'+ 'a'*32+'/../private',False),
    ('https://private.invalid/file',False),
    ('file:///private',False),
])
def test_native_and_legacy_media_reference_contract(reference,valid):
    from app.services.switch_document_media import valid_media_reference
    assert valid_media_reference(reference) is valid


def test_native_switch_handle_requires_consent_then_publishes(tmp_path):
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(attachments=[attachment(mxc='switch-media://'+'a'*32)]),f.settings)
        assert first.processing_state=='confirmation_requested'
        assert not f.transport.downloads
        await f.feature.accept(confirm(first.request_ref),f.settings)
        await f.feature.work_once(f.settings)
        await publish(f)
        assert f.transport.downloads==[('test-room','switch-media://'+'a'*32)]
        assert (await f.jobs.get(first.request_ref))['state']=='published'
    asyncio.run(run())


def test_native_handle_is_a_query_value_never_a_network_destination(monkeypatch):
    async def run():
        from contextlib import asynccontextmanager
        transport=SwitchDocumentTransport(configuration())
        handle='switch-media://'+'a'*32
        calls=[]
        def respond(request):
            calls.append(request)
            assert request.url.scheme=='https' and request.url.host=='switch.invalid'
            assert request.url.path=='/agents/test-agent/rooms/test-room/media'
            assert request.url.params['mxc']==handle
            return httpx.Response(200,content=b'Fixture text.')
        @asynccontextmanager
        async def http():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                yield client
        monkeypatch.setattr(transport.api,'http',http)
        assert await transport.download('test-room',attachment(mxc=handle))==b'Fixture text.'
        assert len(calls)==1
    asyncio.run(run())
