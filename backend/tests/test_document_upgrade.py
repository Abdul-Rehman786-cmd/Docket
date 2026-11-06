"""Real parsers and SQLite transactions; Switch/hosted storage are mocked."""
import asyncio
import os
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from pypdf import PdfWriter

from app.models.documents import DocumentScope, DocumentSearch
from app.services.document_commands import document_command
from app.services.document_extraction import (extract_text, extract_sync, ExtractionError,
    MAX_TEXT, MAX_PART, MAX_UPLOAD)
from app.services.document_attachments import DocumentAttachments
from test_document_attachments import setup, event, attachment, publish, TENANT, ROOM

FIXTURES = Path(__file__).parent / 'fixtures' / 'documents'


@pytest.mark.parametrize('phrase,decision', [('YES!', 'save'), ('save.', 'save'), ('Save it?', 'save'),
    ('yes please', 'save'), ('no!', 'skip'), ('skip', 'skip'), ('cancel.', 'skip'), ("Don't save", 'skip')])
def test_simple_consent(phrase, decision, tmp_path):
    async def run():
        f = setup(tmp_path)
        first = await f.feature.accept(event(thread_id='original'), f.settings)
        result = await f.feature.accept(event(message_id='answer', body=phrase, attachments=[], thread_id=None), f.settings)
        assert result.request_ref == first.request_ref
        assert result.processing_state == ('queued' if decision == 'save' else 'declined')
        assert not f.transport.downloads
    asyncio.run(run())


@pytest.mark.parametrize('phrase', ['I said yes yesterday', 'yes please book a meeting', 'do not save this', 'cancel the meeting', 'save it tomorrow'])
def test_complete_phrases_only(phrase):
    assert document_command(phrase) is None


def test_filename_choice_duplicate_names_and_replayed_confirmation(tmp_path):
    async def run():
        f = setup(tmp_path)
        first = await f.feature.accept(event(), f.settings)
        second = await f.feature.accept(event(message_id='other', attachments=[attachment('other.md', 'mxc://switch.invalid/other')]), f.settings)
        command = lambda body, mid: event(message_id=mid, attachments=[], body=body)
        assert (await f.feature.accept(command('yes', 'ambiguous'), f.settings)).processing_state == 'needs_filename'
        assert (await f.jobs.get(first.request_ref))['state'] == 'confirmation_requested'
        selected = await f.feature.accept(command('save other.md!', 'specific'), f.settings)
        assert selected.request_ref == second.request_ref and selected.processing_state == 'queued'
        await f.feature.work_once(f.settings)
        await publish(f)
        replay = await f.feature.accept(command('save other.md!', 'specific'), f.settings)
        assert replay.duplicate and replay.request_ref == second.request_ref
        assert (await f.jobs.get(first.request_ref))['state'] == 'confirmation_requested'
        duplicate = await f.feature.accept(event(message_id='duplicate', attachments=[attachment('design.md', 'mxc://switch.invalid/duplicate')]), f.settings)
        assert (await f.feature.accept(command('save design.md', 'duplicate-name'), f.settings)).processing_state == 'needs_filename'
        assert (await f.jobs.get(duplicate.request_ref))['state'] == 'confirmation_requested'
    asyncio.run(run())


def test_bare_yes_without_pending_and_unauthorized_sender_cannot_import(tmp_path):
    async def run():
        f = setup(tmp_path)
        early = event(message_id='early', attachments=[], body='yes')
        assert (await f.feature.accept(early, f.settings)).processing_state == 'ignored'
        first = await f.feature.accept(event(), f.settings)
        assert (await f.feature.accept(early, f.settings)).duplicate
        assert (await f.feature.accept(event(message_id='intruder', sender='@other:switch.invalid', attachments=[], body='yes'), f.settings)).processing_state == 'ignored'
        assert (await f.jobs.get(first.request_ref))['state'] == 'confirmation_requested'
        assert not f.transport.downloads
    asyncio.run(run())


def test_plain_consent_races_and_restart_binding(tmp_path):
    async def run():
        f = setup(tmp_path)
        first = await f.feature.accept(event(), f.settings)
        await asyncio.gather(*(f.feature.accept(event(message_id='consent', attachments=[], body='yes'), f.settings) for _ in range(4)))
        assert (await f.jobs.get(first.request_ref))['state'] == 'queued'
        restart = DocumentAttachments(f.jobs, f.repo, f.feature.scope_resolver, lambda s: f.transport, f.factory)
        second = await restart.accept(event(message_id='later-upload'), f.settings)
        replay = await restart.accept(event(message_id='consent', attachments=[], body='yes'), f.settings)
        assert replay.duplicate and replay.request_ref == first.request_ref
        assert (await f.jobs.get(second.request_ref))['state'] == 'confirmation_requested'
    asyncio.run(run())


def test_recovered_outbox_never_reuses_old_reference_or_thread(tmp_path):
    async def run():
        f = setup(tmp_path)
        first = await f.feature.accept(event(thread_id='source-thread'), f.settings)
        # A queued payload from the prior release, not newly rendered text.
        import sqlite3, json
        with sqlite3.connect(f.jobs.path) as db:
            db.execute('UPDATE attachment_outbox SET data=?', (json.dumps(dict(kind='prompt', body='save '+first.request_ref, thread_id='source-thread')),))
        await f.feature.send_once(f.settings)
        assert f.transport.posts[0][2] is None
        assert first.request_ref not in f.transport.posts[0][1]
        assert 'yes or save' in f.transport.posts[0][1]
        row = await f.jobs.get(first.request_ref)
        assert row['data']['items'][0]['source']['thread_id'] == 'source-thread'
    asyncio.run(run())


@pytest.mark.parametrize('suffix', ['pdf', 'docx', 'pptx'])
def test_real_parser_process_and_consent_publication_search(suffix, tmp_path):
    async def run():
        raw = (FIXTURES / ('otter.'+suffix)).read_bytes()
        assert 'paper otter carries a green notebook' in await extract_text(raw, 'otter.'+suffix)
        f = setup(tmp_path)
        f.transport.raw = raw
        first = await f.feature.accept(event(attachments=[attachment('otter.'+suffix)]), f.settings)
        assert not f.transport.downloads and not f.repo.documents
        await f.feature.accept(event(message_id='consent', attachments=[], body='yes'), f.settings)
        await f.feature.work_once(f.settings)
        await publish(f)
        assert (await f.jobs.get(first.request_ref))['state'] == 'published'
        doc = next(iter(f.repo.documents.values()))
        assert doc['source_type'] == suffix
        matches = await f.factory(f.repo).search(DocumentScope(TENANT, (ROOM,)), DocumentSearch(query='What does the otter carry?'))
        assert 'green notebook' in matches[0].text
        assert matches[0].location_basis == 'stored_text_lines' and matches[0].citation.startswith('document:')
    asyncio.run(run())


@pytest.mark.parametrize('suffix,raw,reason', [('pdf', b'not a PDF', 'invalid_document'),
    ('docx', b'not a ZIP', 'invalid_document'), ('pptx', b'PK\x03\x04broken', 'invalid_document'),
    ('docx', b'\xd0\xcf\x11\xe0encrypted', 'encrypted_document'),
    ('txt', b'\xff', 'invalid_document'), ('md', b'x'*(MAX_TEXT+1), 'extracted_text_too_large'),
    ('txt', b'x'*(MAX_UPLOAD+1), 'attachment_too_large')])
def test_invalid_and_bounded_extraction(suffix, raw, reason):
    with pytest.raises(ExtractionError, match=reason):
        extract_sync(raw, '.'+suffix)


def test_pdf_encryption_scanned_and_page_limits():
    for pages, password, reason in [(1, True, 'encrypted_document'), (1, False, 'no_searchable_text'), (101, False, 'page_slide_limit')]:
        w = PdfWriter()
        for _ in range(pages):
            w.add_blank_page(width=100, height=100)
        if password:
            w.encrypt('fixture-password')
        out = BytesIO(); w.write(out)
        with pytest.raises(ExtractionError, match=reason):
            extract_sync(out.getvalue(), '.pdf')


@pytest.mark.parametrize('name,raw,reason', [('word/huge.xml', b'a'*(MAX_PART+1), 'archive_expansion_limit'),
    ('../escape.xml', b'<x/>', 'unsafe_document'), ('word/vbaProject.bin', b'x', 'unsafe_document'),
    ('word/evil.xml', b'<!DOCTYPE x [<!ENTITY a "secret">]><x>&a;</x>', 'unsafe_document')])
def test_office_archive_guards(name, raw, reason):
    out = BytesIO()
    with ZipFile(out, 'w', ZIP_DEFLATED) as z:
        z.writestr(name, raw)
    with pytest.raises(ExtractionError, match=reason):
        extract_sync(out.getvalue(), '.docx')


@pytest.mark.parametrize('changes', [{'room_id':'second-room'}, {'bridge_id':'other-bridge'},
    {'tenant_id':'00000000-0000-0000-0000-000000000099'}])
def test_simple_consent_scope_isolation(tmp_path,changes):
    from fastapi import HTTPException
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(),f.settings)
        try:
            result=await f.feature.accept(event(message_id='isolated',attachments=[],body='yes',**changes),f.settings)
            assert result.processing_state in ('ignored','not_authorized')
        except HTTPException as error:
            assert error.status_code==403
        assert (await f.jobs.get(first.request_ref))['state']=='confirmation_requested'
    asyncio.run(run())


def test_expired_and_incomplete_are_not_eligible_for_plain_yes(tmp_path):
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(),f.settings)
        f.now[0]+=61
        assert (await f.feature.accept(event(message_id='expired-answer',attachments=[],body='yes'),f.settings)).processing_state=='ignored'
        await f.feature.accept(event(message_id='group',attachment_group=dict(id='group',index=0,total=2)),f.settings)
        assert (await f.feature.accept(event(message_id='incomplete-answer',attachments=[],body='save'),f.settings)).processing_state=='ignored'
        assert not f.transport.downloads
    asyncio.run(run())


def test_confirmation_rolls_back_state_replay_and_outbox_together(tmp_path,monkeypatch):
    async def run():
        f=setup(tmp_path)
        first=await f.feature.accept(event(),f.settings)
        enqueue=f.jobs._enqueue
        def crash(*args): raise RuntimeError('fixture-transaction-failure')
        monkeypatch.setattr(f.jobs,'_enqueue',crash)
        answer=event(message_id='atomic-answer',attachments=[],body='yes')
        with pytest.raises(RuntimeError): await f.feature.accept(answer,f.settings)
        assert (await f.jobs.get(first.request_ref))['state']=='confirmation_requested'
        monkeypatch.setattr(f.jobs,'_enqueue',enqueue)
        result=await f.feature.accept(answer,f.settings)
        assert result.processing_state=='queued' and not result.duplicate
        assert len(await f.jobs.reply_states(first.request_ref))==2
    asyncio.run(run())


def test_archive_total_slide_and_empty_limits():
    out=BytesIO()
    with ZipFile(out,'w',ZIP_DEFLATED) as z:
        for i in range(9): z.writestr(str(i),b'a'*MAX_PART)
    with pytest.raises(ExtractionError,match='archive_expansion_limit'):
        extract_sync(out.getvalue(),'.docx')
    for suffix in ('docx','pptx'):
        source=FIXTURES/('otter.'+suffix)
        out=BytesIO()
        with ZipFile(source) as old, ZipFile(out,'w',ZIP_DEFLATED) as z:
            for name in old.namelist():
                raw=old.read(name).replace(b'The paper otter carries a green notebook.',b'')
                z.writestr(name,raw)
        with pytest.raises(ExtractionError,match='no_searchable_text'):
            extract_sync(out.getvalue(),'.'+suffix)
    out=BytesIO()
    with ZipFile(FIXTURES/'otter.pptx') as old, ZipFile(out,'w',ZIP_DEFLATED) as z:
        for name in old.namelist():
            raw=old.read(name)
            if name=='ppt/presentation.xml': raw=raw.replace(b'<p:sldId id="256" r:id="rId1"/>',b'<p:sldId id="256" r:id="rId1"/>'*201)
            z.writestr(name,raw)
    with pytest.raises(ExtractionError,match='page_slide_limit'):
        extract_sync(out.getvalue(),'.pptx')


def test_parser_timeout_is_reported_safely(monkeypatch):
    import subprocess
    import app.services.document_extraction as parser
    def timeout(*args,**kwargs):
        assert kwargs['timeout']==20
        raise subprocess.TimeoutExpired('fixture-parser',20)
    monkeypatch.setattr(parser.subprocess,'run',timeout)
    with pytest.raises(ExtractionError,match='parser_timeout'):
        asyncio.run(extract_text(b'%PDF-fixture','fixture.pdf'))


def test_failed_binary_replacement_keeps_published_version(tmp_path):
    from uuid import uuid4
    from fastapi import HTTPException
    from app.models.documents import ImportMetadata
    async def run():
        f=setup(tmp_path); f.transport.raw=(FIXTURES/'otter.pdf').read_bytes()
        await f.feature.accept(event(attachments=[attachment('otter.pdf')]),f.settings)
        await f.feature.accept(event(message_id='consent',attachments=[],body='yes'),f.settings)
        await f.feature.work_once(f.settings); await publish(f)
        doc=next(iter(f.repo.documents.values())); published=doc['published_version_id']
        metadata=ImportMetadata(import_id=uuid4(),source_key=doc['source_key'],filename='otter.pdf',title='Otter',expected_version=1)
        with pytest.raises(HTTPException):
            await f.factory(f.repo).import_bytes(DocumentScope(TENANT,(ROOM,)),metadata,b'%PDF-broken')
        assert doc['published_version_id']==published and len(f.repo.versions)==1
    asyncio.run(run())


def test_parser_process_concurrency_is_bounded(monkeypatch):
    import threading
    import app.services.document_extraction as parser
    async def run():
        monkeypatch.setattr(parser,'_slots',asyncio.Semaphore(2))
        active=0; maximum=0; ready=threading.Event(); release=threading.Event(); lock=threading.Lock()
        def parse(*args):
            nonlocal active,maximum
            with lock:
                active+=1; maximum=max(active,maximum)
                if active==2: ready.set()
            assert release.wait(3)
            with lock: active-=1
            return 'Fixture text'
        monkeypatch.setattr(parser,'_run_parser',parse)
        tasks=[asyncio.create_task(extract_text(b'%PDF-fixture','x.pdf')) for _ in range(3)]
        assert await asyncio.to_thread(ready.wait,2)
        assert active==2
        release.set(); await asyncio.gather(*tasks)
        assert maximum==2
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get('RUN_LOCAL_MODEL_TESTS')!='1',reason='Opt-in real local model')
def test_pdf_real_extraction_inference_publication_and_citation(tmp_path):
    from app.services.document_service import DocumentService
    from app.services.document_worker import DocumentWorker
    async def run():
        f=setup(tmp_path)
        f.feature.service_factory=lambda repo:DocumentService(repo)
        f.worker=DocumentWorker(f.repo)
        f.transport.raw=(FIXTURES/'otter.pdf').read_bytes()
        first=await f.feature.accept(event(attachments=[attachment('otter.pdf')]),f.settings)
        await f.feature.accept(event(message_id='real-model-consent',attachments=[],body='yes'),f.settings)
        await f.feature.work_once(f.settings); await publish(f)
        assert (await f.jobs.get(first.request_ref))['state']=='published'
        hits=await DocumentService(f.repo).search(DocumentScope(TENANT,(ROOM,)),DocumentSearch(query='What does the paper otter carry?'))
        assert 'green notebook' in hits[0].text and hits[0].location_basis=='stored_text_lines'
    asyncio.run(run())


@pytest.mark.parametrize('body',['yes!','save it','no.','save planning.pdf'])
def test_listener_and_backend_share_simple_command_routing(body,tmp_path):
    import json, httpx
    from app.services.switch_listener import SwitchListener
    from app.services.message_service import message_reference
    from test_document_attachments import configuration
    async def run():
        listener=SwitchListener(configuration(),state_path=tmp_path/'listener.json')
        listener.state.cursor=0
        message=event(message_id='routing',attachments=[],body=body)
        payload=message.model_dump(mode='json')
        queue=asyncio.Queue()
        queue.put_nowait(('message',dict(room_id='test-room',bridge_id='fixture-bridge',payload=payload),1))
        seen=[]
        def respond(request):
            seen.append(request.url.path)
            assert request.url.path=='/messages/document-attachments'
            assert json.loads(request.content)['body']==body
            return httpx.Response(200,json=dict(status='received',message_ref=message_reference(message),
                received_at='2026-11-06T00:00:00Z',processing_state='ignored',duplicate=False))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),base_url='http://fixture/') as client:
            task=asyncio.create_task(listener._forward_events(queue,client))
            async with asyncio.timeout(2):
                while listener.state.cursor!=1:
                    if task.done(): task.result()
                    await asyncio.sleep(.01)
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        assert len(seen)==1
    asyncio.run(run())



def test_office_edit_password_protection_is_rejected():
    out=BytesIO()
    with ZipFile(FIXTURES/'otter.docx') as old, ZipFile(out,'w',ZIP_DEFLATED) as z:
        for name in old.namelist(): z.writestr(name,old.read(name))
        z.writestr('word/settings.xml','<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:documentProtection w:enforcement="1"/></w:settings>')
    with pytest.raises(ExtractionError,match='encrypted_document'):
        extract_sync(out.getvalue(),'.docx')


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Windows Uvicorn reload event loop')
def test_office_parser_under_windows_selector_loop():
    # This reproduces Uvicorn --reload, unlike asyncio.run's default Proactor loop.
    loop=asyncio.SelectorEventLoop()
    try:
        text=loop.run_until_complete(extract_text((FIXTURES/'otter.docx').read_bytes(),'otter.docx'))
        assert 'green notebook' in text
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
