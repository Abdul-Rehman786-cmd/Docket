"""Document chat with scoped in-memory storage and deterministic AI; no live chat."""
import asyncio
import json
import os
import re
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.models.documents import DocumentScope
from app.repositories.document_memory import MemoryDocumentRepository
from app.services.document_chat import DocumentChat, TTL
from app.services.document_service import DocumentService
from test_documents import FakeEmbedder, fake_chunks

TENANT = str(uuid4())
ROOM = str(uuid4())


def mock_intent(message):
    """Canned AI proposals for domain fixtures, not a production intent parser."""
    lower = message.casefold()
    if lower in {'clear selection'}:
        return {'action': 'clear'}
    if lower in {'more documents'}:
        return {'action': 'more'}
    if message == '2':
        return {'action': 'select', 'choice': 2}
    if lower == 'summarize it':
        return {'action': 'summarize', 'use_selected': True}
    if lower in {'who is responsible', 'what deadline does it mention'}:
        return {'action': 'ask', 'use_selected': True}
    if lower.startswith(('list ', 'show ', 'which document', 'which file', 'what files', 'what documents', 'could you list', 'please show')):
        if 'mention the launch deadline' in lower:
            return {'action': 'find', 'query': 'launch deadline'}
        return {'action': 'list'}
    filenames = re.findall(r'[\w.-]+\.(?:pdf|docx|pptx|md|txt)', message)
    reference = 'Long Plan.pdf' if 'Long Plan.pdf' in message else filenames[0] if filenames else None
    if reference:
        return {'action': 'ask' if lower.startswith('what ') else 'select', 'reference': reference}
    if lower in {'use budget', 'select long launch plan'}:
        return {'action': 'select', 'reference': message.split(' ', 1)[1]}
    if lower.startswith('find '):
        return {'action': 'find', 'query': message}
    return {'action': 'none'}


def fixture():
    now = [1000.0]
    repo = MemoryDocumentRepository()
    settings = Settings(_env_file=None, switch_tenant_id=TENANT, switch_room_ids='room,other', document_ingestion_enabled=True)
    async def scope(settings, room):
        if room not in settings.allowed_switch_rooms:
            raise HTTPException(403)
        return DocumentScope(str(settings.switch_tenant_id), (ROOM if room == 'room' else 'other-room',))
    service = DocumentService(repo, fake_chunks, FakeEmbedder())
    chat = DocumentChat(repo, service, scope, lambda: now[0])
    prompts = []
    routing_prompts = []
    async def ai(prompt):
        if prompt.startswith('DOCUMENT_INTENT_ROUTING'):
            routing_prompts.append(prompt)
            return mock_intent(json.loads(prompt.split('\n')[-1])['message'])
        prompts.append(prompt)
        return {'answer': 'Mira owns the launch; the deadline is Friday.', 'supported': True, 'sources': ['D1']}
    async def send(text, sender='sender', room='room', message=None, synthesize=None, settings_override=None, intent=None):
        async def gateway(prompt):
            if prompt.startswith('DOCUMENT_INTENT_ROUTING'):
                if intent is not None:
                    routing_prompts.append(prompt)
                    return intent
                return await ai(prompt)
            return await (synthesize or ai)(prompt)
        return await chat.handle(settings_override or settings, dict(origin='discord', durable=True, adapter_verified=True,
            switch_author_id=sender, switch_message_id=message or str(uuid4()), room_id=room), '@bot '+text, gateway)
    return SimpleNamespace(now=now, repo=repo, settings=settings, service=service, chat=chat, prompts=prompts, routing_prompts=routing_prompts, ai=ai, send=send, scope=scope)


def seed(f, name='planning.pdf', title='Launch plan', *, room=ROOM, tenant=TENANT, active=True, published=True, count=3):
    doc_id, version_id = str(uuid4()), str(uuid4())
    f.repo.documents[doc_id] = dict(id=doc_id, tenant_id=tenant, room_id=room, active=active, title=title,
        published_version_id=version_id if published else None, latest_version=1)
    f.repo.versions[version_id] = dict(id=version_id, document_id=doc_id, filename=name, title=title,
        status='completed', version_number=1)
    for i in range(count):
        chunk_id = str(uuid4())
        f.repo.chunks[chunk_id] = dict(id=chunk_id, document_id=doc_id, version_id=version_id,
            position=i, text=f'Section {i}: Mira owns the launch; the deadline is Friday.', section=f'Section {i}',
            start_line=i+1, end_line=i+1, status='completed', content_hash='fixture', embedding_content_hash='fixture',
            model_recipe=__import__('app.core.embedding_model', fromlist=['RECIPE']).RECIPE, embedding=[1.0]+[0.0]*383)
    return doc_id


def test_scoped_metadata_listing_pagination_without_generated_inventory():
    async def run():
        f = fixture()
        ids = [seed(f, name=f'plan{i}.pdf', title=f'Plan {i}') for i in range(7)]
        seed(f, name='archived.pdf', active=False)
        seed(f, name='pending.pdf', published=False)
        seed(f, name='other-room.pdf', room='other-room')
        seed(f, name='other-tenant.pdf', tenant=str(uuid4()))
        first = await f.send('List our documents.')
        second = await f.send('more documents')
        for name in ('archived', 'pending', 'other-room', 'other-tenant'):
            assert name not in first+second
        assert sum('plan'+str(i)+'.pdf' in first for i in range(7)) == 5
        assert sum('plan'+str(i)+'.pdf' in second for i in range(7)) == 2
        assert not f.prompts and all(doc_id not in first+second for doc_id in ids)
        assert len(f.routing_prompts) == 2
    asyncio.run(run())


@pytest.mark.parametrize('question', ['which document has been saved', 'Which documents have been saved?',
    'what files are stored', 'Which file was uploaded?', 'What documents do we have?', 'show saved documents',
    'which documents are saved right now', 'Which documents are saved at the moment?',
    'What files are currently stored in this room?', 'Could you list our documents right now?',
    'Please show me the files available here', 'Which documents are saved in the database right now?'])
def test_saved_inventory_wording_uses_metadata_not_semantic_search(question):
    async def run():
        f = fixture(); seed(f, 'summary.docx', 'Summary')
        async def forbidden(*args, **kwargs):
            raise AssertionError('Inventory must not call semantic retrieval or synthesis')
        f.chat.service.search = forbidden
        answer = await f.send(question, synthesize=forbidden)
        assert 'summary.docx' in answer
    asyncio.run(run())


@pytest.mark.parametrize('question', ["What's in our shared library?", "Show me everything we've kept for reference",
    'Kaunsi files humne rakhi hui hain?', 'Could you bring up the stuff we collected?'])
def test_model_routed_inventory_has_no_keyword_or_exact_phrase_gate(question):
    async def run():
        f = fixture(); seed(f, 'summary.docx', 'Summary')
        async def forbidden(*args, **kwargs):
            raise AssertionError('Inventory only reads scoped metadata after classification')
        f.chat.service.search = forbidden
        answer = await f.send(question, intent={'action': 'list'}, synthesize=forbidden)
        assert 'summary.docx' in answer and len(f.routing_prompts) == 1
        payload = json.loads(f.routing_prompts[0].split('\n')[-1])
        assert payload['message'] == question.rstrip('.!?')
        assert not payload['context']['selected']
    asyncio.run(run())


def test_ai_ordinal_selection_summary_and_unrelated_proposal():
    async def run():
        f = fixture(); seed(f); seed(f, 'deck.pptx', 'Deck')
        await f.send('Bring up our reading shelf', intent={'action': 'list'})
        chosen = next(iter(f.chat.states.values())).choices[1]
        assert 'Selected' in await f.send('The latter one, please', intent={'action': 'select', 'choice': 2})
        assert next(iter(f.chat.states.values())).selected == chosen
        reply = await f.send('Give me the gist of that', intent={'action': 'summarize', 'use_selected': True})
        assert 'Partial summary' in reply
        context = json.loads(f.routing_prompts[-1].split('\n')[-1])['context']
        assert context['selected'] is True and not context['displayed_choices']
        async def forbidden(*args, **kwargs): raise AssertionError('Unrelated route must not access documents')
        f.repo.call = forbidden
        assert await f.send('How is the weather?', intent={'action': 'none'}) is None
    asyncio.run(run())


@pytest.mark.parametrize('proposal', [None, {}, {'action': 'destroy'}, {'action': 'list', 'document_id': 'invented'},
    {'action': 'list', 'tenant_id': 'invented'}, {'action': 'select', 'choice': 9},
    {'action': 'select', 'choice': True}, {'action': 'select', 'choice': 1, 'use_selected': True},
    {'action': 'select', 'reference': 'invented.pdf'}, {'action': 'list', 'query': 'unsupported filter'},
    {'action': 'none', 'reference': 'summary.docx'}, {'action': 'find', 'query': 'launch', 'use_selected': True},
    {'action': 'ask'}, {'action': 'summarize'}])
def test_invalid_or_ungrounded_classifier_proposals_never_read_or_mutate(proposal):
    async def run():
        f = fixture()
        async def forbidden(*args, **kwargs): raise AssertionError('Invalid classifier response must not read documents')
        f.repo.call = forbidden
        async def classify(prompt):
            assert prompt.startswith('DOCUMENT_INTENT_ROUTING')
            return proposal
        meta = dict(origin='discord', durable=True, adapter_verified=True, switch_author_id='sender',
                    switch_message_id='invalid', room_id='room')
        reply = await f.chat.handle(f.settings, meta, 'Can you open summary.docx?', classify)
        assert 'could not interpret' in reply and not f.chat.states
    asyncio.run(run())


def test_consent_bypasses_ai_classifier_and_does_not_select():
    async def run():
        f = fixture(); seed(f)
        for text in ('yes', 'yes please', 'save', 'no', 'skip', 'save summary.docx'):
            assert await f.send(text, intent={'action': 'select', 'choice': 1}) is None
        assert not f.routing_prompts and not f.chat.states
    asyncio.run(run())


def test_unsupported_filters_are_clarified_without_listing_all_documents():
    async def run():
        f = fixture()
        async def forbidden(*args, **kwargs): raise AssertionError('Unsupported history must not read current inventory')
        f.repo.call = forbidden
        reply = await f.send('Only the files I uploaded last week', intent={'action': 'clarify'})
        assert 'clarify' in reply and not f.chat.states
    asyncio.run(run())


def test_plural_document_question_does_not_fall_back_when_discovery_has_no_match():
    async def run():
        f = fixture(); seed(f)
        async def no_match(prompt): return {'matches': []}
        reply = await f.send('Which documents mention the launch deadline?', synthesize=no_match)
        assert reply is not None and 'no convincing document match' in reply
    asyncio.run(run())


def test_exact_name_selection_followups_switch_clear_and_unrelated():
    async def run():
        f = fixture(); one = seed(f); two = seed(f, 'budget.md', 'Budget')
        assert 'Selected' in await f.send('Find planning.pdf')
        answer = await f.send('Who is responsible?')
        assert 'Mira' in answer and 'planning.pdf' in answer and 'stored text lines' in answer
        assert one not in answer and 'source:' not in answer
        payload = json.loads(f.prompts[-1].split('\n')[-1])
        assert payload['document'] == 'Launch plan (planning.pdf)' and not payload['history']
        await f.send('What deadline does it mention?')
        assert json.loads(f.prompts[-1].split('\n')[-1])['history'][0]['question'] == 'Who is responsible'
        assert 'Selected' in await f.send('use Budget')
        await f.send('Who is responsible?')
        assert json.loads(f.prompts[-1].split('\n')[-1])['document'] == 'Budget (budget.md)'
        assert not json.loads(f.prompts[-1].split('\n')[-1])['history']
        for unrelated in ('yes', 'save', 'I will finish my task', 'What are my tasks?', 'What about my tasks?', 'What is my preferred name?', 'What is the capital of France?'):
            assert await f.send(unrelated) is None
        assert 'cleared' in await f.send('clear selection')
        assert 'identify' in await f.send('Summarize it')
        assert one != two
    asyncio.run(run())


def test_semantic_discovery_groups_chunks_and_never_uses_scores_as_confidence():
    async def run():
        f = fixture(); one = seed(f, count=4); seed(f, 'budget.md', 'Budget')
        async def match(prompt):
            payload = json.loads(prompt.split('\n')[-1])
            assert len(payload['candidates']) == 2
            assert len({row['label'] for row in payload['candidates']}) == 2
            assert all('similarity' not in row for row in payload['candidates'])
            return {'matches': [next(row['number'] for row in payload['candidates'] if 'planning.pdf' in row['label'])]}
        assert 'planning.pdf' in await f.send('Find the presentation about our launch', synthesize=match)
        assert next(iter(f.chat.states.values())).selected == one
        async def no_match(prompt): return {'matches': []}
        assert 'no convincing' in await f.send('Find a document about volcanoes', synthesize=no_match)
    asyncio.run(run())


def test_ambiguous_number_title_and_stale_choice_replay():
    async def run():
        f = fixture(); seed(f); seed(f, 'launch.pptx', 'Launch deck')
        async def ambiguous(prompt): return {'matches': [1, 2]}
        assert 'Several' in await f.send('Find our launch presentation', synthesize=ambiguous)
        assert 'Selected' in await f.send('2', message='old-selection')
        assert 'Several' in await f.send('Find our launch presentation', synthesize=ambiguous)
        assert 'already handled' in await f.send('2', message='old-selection')
        assert next(iter(f.chat.states.values())).selected is None
        assert next(iter(f.chat.states.values())).choices
        assert 'planning.pdf' in await f.send('planning.pdf')
        assert not next(iter(f.chat.states.values())).choices
    asyncio.run(run())


def test_identity_scope_expiry_restart_and_unverified_inputs():
    async def run():
        f = fixture(); seed(f)
        await f.send('select planning.pdf')
        assert 'identify' in await f.send('Summarize it', sender='another')
        assert 'identify' in await f.send('Summarize it', room='other')
        other = f.settings.model_copy(update={'switch_tenant_id': uuid4()})
        assert 'identify' in await f.send('Summarize it', settings_override=other)
        f.now[0] += TTL+1
        assert 'identify' in await f.send('Summarize it')
        f.chat.states.clear()  # Restart discards only conversational state.
        assert 'identify' in await f.send('Summarize it')
        assert len(f.repo.documents) == 1
        for changes in ({'adapter_verified': False}, {'switch_author_id': None}, {'origin': 'viewer'}, {'room_id': 'blocked'}):
            meta = dict(origin='discord', durable=True, adapter_verified=True, switch_author_id='sender', switch_message_id=str(uuid4()), room_id='room')
            assert 'could not verify' in await f.chat.handle(f.settings, {**meta, **changes}, 'List documents', f.ai)
            assert await f.chat.handle(f.settings, {**meta, **changes}, 'What are my tasks?', f.ai) is None
    asyncio.run(run())


@pytest.mark.parametrize('change', ['archive', 'delete', 'unpublish', 'access'])
def test_current_access_and_publication_rechecked(change):
    async def run():
        f = fixture(); doc_id = seed(f)
        await f.send('select planning.pdf')
        if change == 'archive': f.repo.documents[doc_id]['active'] = False
        if change == 'delete': del f.repo.documents[doc_id]
        if change == 'unpublish': f.repo.documents[doc_id]['published_version_id'] = None
        if change == 'access': f.repo.documents[doc_id]['room_id'] = 'lost-room'
        assert 'identify' in await f.send('Summarize it')
        assert not f.chat.states and not f.prompts
    asyncio.run(run())


def test_publication_change_during_synthesis_never_returns_stale_answer():
    async def run():
        f = fixture(); doc_id = seed(f)
        async def replace(prompt):
            old = f.repo.versions[f.repo.documents[doc_id]['published_version_id']]
            version_id = str(uuid4())
            f.repo.versions[version_id] = dict(old, id=version_id, version_number=2)
            f.repo.documents[doc_id]['published_version_id'] = version_id
            return {'answer': 'Stale answer', 'supported': True, 'sources': ['D1']}
        reply = await f.send('What does planning.pdf say about the deadline?', synthesize=replace)
        assert 'changed' in reply and 'Stale answer' not in reply
    asyncio.run(run())


@pytest.mark.parametrize('response', [None, {}, {'supported': False, 'answer': 'Unknown', 'sources': []},
    {'supported': True, 'answer': 'Fake', 'sources': ['D999']},
    {'supported': True, 'answer': 'Fake [source:bad]', 'sources': ['D1']},
    {'supported': True, 'answer': 'Page 8 says Friday', 'sources': ['D1']}])
def test_no_evidence_and_invalid_citations_fail_closed(response):
    async def run():
        f = fixture(); seed(f)
        async def ai(prompt): return response
        answer = await f.send('What does planning.pdf say about the deadline?', synthesize=ai)
        assert 'reliable answer' in answer or 'validate' in answer
        assert 'Fake' not in answer and 'Page 8' not in answer
    asyncio.run(run())


def test_representative_summary_and_history_bounds():
    async def run():
        f = fixture(); seed(f, count=32)
        await f.send('select planning.pdf')
        reply = await f.send('Summarize it')
        payload = json.loads(f.prompts[-1].split('\n')[-1])
        assert len(payload['evidence']) == 8
        assert payload['evidence'][0]['start_line'] == 1 and payload['evidence'][-1]['end_line'] == 32
        assert sum(len(row['text']) for row in payload['evidence']) <= 16000
        assert 'Partial summary' in reply
        for _ in range(4): await f.send('Who is responsible?')
        assert len(json.loads(f.prompts[-1].split('\n')[-1])['history']) == 2
    asyncio.run(run())


def test_exact_references_beyond_first_metadata_page():
    async def run():
        f = fixture()
        for i in range(57): seed(f, name=f'file{i}.md', title=f'Title {i}', count=0)
        last = f.repo.documents[sorted(f.repo.documents)[-1]]
        last['title'] = 'Long launch plan'
        f.repo.versions[last['published_version_id']]['filename'] = 'Long Plan.pdf'
        assert 'Selected Long launch plan' in await f.send('select Long launch plan')
        await f.send('clear selection')
        assert 'Selected Long launch plan' in await f.send('find "Long Plan.pdf"')
        assert not f.prompts
    asyncio.run(run())


def test_filename_precedes_incidental_title_words_and_unicode_is_not_expanded():
    async def run():
        f = fixture(); chosen = seed(f); seed(f, 'other.md', 'deadline')
        async def ai(prompt):
            assert '\\u' not in prompt
            return await f.ai(prompt)
        answer = await f.send('What does planning.pdf say about the deadline? café', synthesize=ai)
        assert 'stored text lines' in answer
        assert next(iter(f.chat.states.values())).selected == chosen
    asyncio.run(run())


def test_no_retrieved_evidence_never_calls_synthesis_or_falls_back():
    async def run():
        f = fixture(); seed(f, count=0)
        reply = await f.send('What does planning.pdf say about the deadline?')
        assert 'no usable passages' in reply and not f.prompts
    asyncio.run(run())


def test_bounded_state_and_failed_request_does_not_commit_selection():
    async def run():
        f = fixture(); first = seed(f); seed(f, 'other.pdf', 'Other')
        await f.send('select planning.pdf')
        async def fail(prompt): raise HTTPException(503, 'fixture')
        with pytest.raises(HTTPException):
            await f.send('What does other.pdf say about the deadline?', synthesize=fail)
        assert next(iter(f.chat.states.values())).selected == first
        for i in range(205): await f.send('list documents', sender=str(i))
        assert len(f.chat.states) == 200
        f.now[0] += TTL+1
        await f.send('list documents', sender='fresh')
        assert len(f.chat.states) == 1
    asyncio.run(run())


@pytest.mark.parametrize('question', ['List our documents.', 'which document has been saved',
                                    'which documents are saved right now', "What's in the shared library?"])
def test_authenticated_message_adapter_reaches_document_chat(monkeypatch, question):
    from threading import Event
    from fastapi.testclient import TestClient
    from app.core.config import get_settings
    from app.main import app
    from app.services import ai_service
    from app.services.switch_api import SwitchAPI
    f = fixture(); seed(f)
    for name, value in dict(SWITCH_TENANT_ID=TENANT, SWITCH_ROOM_IDS='room', DOCUMENT_INGESTION_ENABLED='true',
        SWITCH_REQUESTER_AGENT_ID='fixture-client', SWITCH_REQUESTER_API_KEY='fixture-client-key',
        SWITCH_AI_ROOM_ID='private', SWITCH_AI_SENDER_ID='fixture-ai', SWITCH_ADAPTER_TOKEN='fixture-adapter-'+'x'*32).items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    delivered = Event(); posted = []
    async def ai(settings, prompt, **kwargs):
        assert prompt.startswith('DOCUMENT_INTENT_ROUTING')
        return json.dumps({'action': 'list'})
    async def post(self, room_id, body, thread_id=None):
        posted.append((room_id, body, thread_id))
        if body != 'Processing your request...': delivered.set()
    monkeypatch.setattr(ai_service, 'ask_switch', ai)
    monkeypatch.setattr(SwitchAPI, 'post_room_message', post)
    with TestClient(app) as client:
        app.state.ai_service.document_chat = f.chat
        payload = dict(tenant_id=TENANT, room_id='room', message_id='list-1', sender='opaque-unregistered-sender',
                       body='@bot '+question, addressed=True, thread_id='source-thread')
        assert client.post('/messages', json=payload).status_code == 401
        headers = {'X-Ingest-Token': 'test-ingest-token', 'X-Adapter-Token': 'fixture-adapter-'+'x'*32}
        response = client.post('/messages', headers=headers, json=payload)
        assert response.status_code == 200 and delivered.wait(3)
        assert posted[-1][0] == 'room' and posted[-1][2] is None and 'planning.pdf' in posted[-1][1]
        assert client.post('/messages', headers=headers, json=payload).json()['duplicate']
        assert len(posted) == 2  # Progress plus one listing; replay posts nothing.


def test_catalog_rest_filters_and_auth_failure_cannot_issue_read():
    from app.repositories.document_repository import SupabaseDocumentRepository
    async def run():
        f = fixture()
        repository = SupabaseDocumentRepository()
        settings = Settings(_env_file=None, supabase_url='https://fixture.invalid', supabase_service_role_key='fixture-only-key')
        repository.settings = lambda: settings
        calls = []
        def transport(request):
            calls.append(request)
            if request.method == 'POST':
                assert json.loads(request.content)['p_operation'] == 'documents'
                return httpx.Response(200, json=[])
            params = request.url.params
            assert params['tenant_id'] == 'eq.'+TENANT and params['room_id'] == 'in.('+ROOM+')'
            assert params['active'] == params['room.enabled'] == 'eq.true'
            assert params['published.status'] == 'eq.completed' and params['order'] == 'id.asc'
            assert 'source_text' not in params['select'] and 'documents_published_version_fk!inner' in params['select']
            return httpx.Response(200, json=[dict(id='doc', title='Title', published_version_id='version', published=dict(filename='file.pdf'))])
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            rows = await repository.call(DocumentScope(TENANT, (ROOM,)), 'catalog', dict(limit=6, offset=5), client=client)
            assert rows[0]['filename'] == 'file.pdf' and len(calls) == 2
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={'code': '42501'}))) as client:
            with pytest.raises(HTTPException) as error:
                await repository.call(DocumentScope(TENANT, (ROOM,)), 'catalog', dict(limit=6, offset=5), client=client)
            assert error.value.status_code == 403
    asyncio.run(run())


def test_ai_pipeline_uses_synthesis_preserves_citations_and_skips_memory(monkeypatch):
    from app.services import ai_service
    from app.services.ai_service import AIService, response_scope
    async def run():
        f = fixture(); doc_id = seed(f)
        service = AIService(); service.document_chat = f.chat
        async def ai(settings, prompt, **kwargs):
            assert kwargs == {'synthesis': True}
            assert 'untrusted data' in prompt
            return json.dumps(await f.ai(prompt))
        async def no_memory(*args, **kwargs): raise AssertionError('Document question must not query room memories')
        replies = []
        async def reply(*args, **kwargs): replies.append((args, kwargs))
        monkeypatch.setattr(ai_service, 'ask_switch', ai)
        monkeypatch.setattr(ai_service, 'query_team_memory', no_memory)
        monkeypatch.setattr(service, '_send_discord_reply', reply)
        scope = response_scope(f.settings)
        row = service.repository.create(scope, uuid4(), '@bot What does planning.pdf say about the deadline?', 'a'*24)
        meta = dict(origin='discord', durable=True, adapter_verified=True, addressed=True,
            switch_author_id='sender', switch_message_id='m1', room_id='room')
        await service._run(scope, row, f.settings, source_meta=meta)
        result = service.repository.get(scope, row.request_id)
        assert result.status == 'answered' and 'stored text lines' in result.answer
        assert doc_id not in result.answer and 'source:' not in result.answer
        assert replies[-1][1]['thread_id'] is None
        assert result.answer == replies[-1][0][2]
    asyncio.run(run())


@pytest.mark.parametrize('question,proposal', [('1', {'action': 'select', 'choice': 1}),
    ('1 can you explain whats in it', {'action': 'summarize', 'choice': 1})])
def test_unmentioned_choice_and_choice_with_question_reach_document_pipeline(monkeypatch, question, proposal):
    from app.services import ai_service
    from app.services.ai_service import AIService, response_scope
    async def run():
        f = fixture(); seed(f, 'summary.docx', 'Summary')
        await f.send('List our documents')
        service = AIService(); service.document_chat = f.chat
        calls = []
        async def ai(settings, prompt, **kwargs):
            calls.append(prompt)
            assert kwargs == {'synthesis': True}
            if prompt.startswith('DOCUMENT_INTENT_ROUTING'):
                context = json.loads(prompt.split('\n')[-1])['context']
                assert context['continuation_only'] is True and context['displayed_choices'] == [1]
                return json.dumps(proposal)
            return json.dumps({'answer': 'Mira owns the launch.', 'supported': True, 'sources': ['D1']})
        async def no_memory(*args, **kwargs): raise AssertionError('A document follow-up cannot query room memory')
        replies = []
        async def reply(*args, **kwargs): replies.append(args[2])
        monkeypatch.setattr(ai_service, 'ask_switch', ai)
        monkeypatch.setattr(ai_service, 'query_team_memory', no_memory)
        monkeypatch.setattr(service, '_send_discord_reply', reply)
        scope = response_scope(f.settings)
        row = service.repository.create(scope, uuid4(), question, 'c'*24)
        meta = dict(origin='discord', durable=True, adapter_verified=True, addressed=False,
            switch_author_id='sender', switch_message_id='followup-1', room_id='room')
        await service._run(scope, row, f.settings, source_meta=meta)
        result = service.repository.get(scope, row.request_id)
        assert result.status == 'answered' and len(replies) == 1
        if proposal['action'] == 'select':
            assert 'Selected Summary' in result.answer and len(calls) == 1
        else:
            assert 'Mira' in result.answer and 'stored text lines' in result.answer and len(calls) == 2
        assert 'Processing' not in replies[0]
    asyncio.run(run())


@pytest.mark.parametrize('change', ['sender', 'room', 'tenant', 'expired', 'unverified', 'restart'])
def test_unmentioned_message_without_own_active_context_cannot_route_or_reply(monkeypatch, change):
    from app.services import ai_service
    from app.services.ai_service import AIService, response_scope
    async def run():
        f = fixture(); seed(f)
        await f.send('list documents')
        service = AIService(); service.document_chat = f.chat
        meta = dict(origin='discord', durable=True, adapter_verified=True, addressed=False,
            switch_author_id='sender', switch_message_id='unaddressed', room_id='room')
        settings = f.settings
        if change == 'sender': meta['switch_author_id'] = 'another'
        if change == 'room': meta['room_id'] = 'other'
        if change == 'tenant': settings = settings.model_copy(update={'switch_tenant_id': uuid4()})
        if change == 'expired': f.now[0] += TTL+1
        if change == 'unverified': meta['adapter_verified'] = False
        if change == 'restart': service.document_chat = DocumentChat(f.repo, f.service, f.scope, lambda: f.now[0])
        async def ai(settings, prompt, **kwargs):
            assert not prompt.startswith('DOCUMENT_INTENT_ROUTING')
            assert not kwargs.get('synthesis')
            return json.dumps({'action': 'none', 'answer': 'No action'})
        async def forbidden(*args, **kwargs): raise AssertionError('No reply or private retrieval for unrelated unmentioned input')
        monkeypatch.setattr(ai_service, 'ask_switch', ai)
        monkeypatch.setattr(ai_service, 'query_team_memory', forbidden)
        monkeypatch.setattr(service, '_send_discord_reply', forbidden)
        scope = response_scope(settings)
        row = service.repository.create(scope, uuid4(), '1', 'd'*24)
        await service._run(scope, row, settings, source_meta=meta)
        assert service.repository.get(scope, row.request_id).status == 'answered'
    asyncio.run(run())


def test_active_context_does_not_make_unmentioned_chatter_public(monkeypatch):
    from app.services import ai_service
    from app.services.ai_service import AIService, response_scope
    async def run():
        f = fixture(); seed(f); await f.send('list documents')
        service = AIService(); service.document_chat = f.chat
        async def ai(settings, prompt, **kwargs):
            return json.dumps({'action': 'none'} if prompt.startswith('DOCUMENT_INTENT_ROUTING') else
                              {'action': 'none', 'answer': 'Ordinary chatter'})
        async def forbidden(*args, **kwargs): raise AssertionError('Unrelated chatter must not produce a channel reply')
        monkeypatch.setattr(ai_service, 'ask_switch', ai)
        monkeypatch.setattr(service, '_send_discord_reply', forbidden)
        scope = response_scope(f.settings)
        row = service.repository.create(scope, uuid4(), 'I am grabbing coffee', 'e'*24)
        await service._run(scope, row, f.settings, source_meta=dict(origin='discord', durable=True,
            adapter_verified=True, addressed=False, switch_author_id='sender', switch_message_id='chatter', room_id='room'))
        assert service.repository.get(scope, row.request_id).status == 'answered'
    asyncio.run(run())


@pytest.mark.parametrize('question', ['which document has been saved', 'which documents are saved right now',
                                    'which documents mention the launch deadline'])
def test_missing_adapter_does_not_send_inventory_to_general_memory(monkeypatch, question):
    from app.services import ai_service
    from app.services.ai_service import AIService, response_scope
    async def run():
        f = fixture(); service = AIService(); service.document_chat = f.chat
        async def forbidden(*args, **kwargs):
            raise AssertionError('Unverified document inventory cannot query memory or invoke AI')
        replies = []
        async def reply(*args, **kwargs): replies.append(args[2])
        async def classify(settings, prompt, **kwargs):
            assert prompt.startswith('DOCUMENT_INTENT_ROUTING')
            return json.dumps({'action': 'list'})
        monkeypatch.setattr(ai_service, 'ask_switch', classify)
        monkeypatch.setattr(ai_service, 'query_team_memory', forbidden)
        monkeypatch.setattr(service, '_send_discord_reply', reply)
        scope = response_scope(f.settings)
        row = service.repository.create(scope, uuid4(), '@bot '+question, 'b'*24)
        meta = dict(origin='discord', durable=True, adapter_verified=False, addressed=True,
                    switch_author_id='sender', switch_message_id='inventory-1', room_id='room')
        await service._run(scope, row, f.settings, source_meta=meta)
        result = service.repository.get(scope, row.request_id)
        assert result.status == 'answered' and 'could not verify' in result.answer
        assert 'could not verify' in replies[-1] and not f.chat.states
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get('RUN_LOCAL_MODEL_TESTS') != '1', reason='Opt-in real local embedding model')
def test_real_local_document_embedding_retrieval_then_fake_synthesis(tmp_path):
    from app.models.documents import ImportMetadata
    from app.services.document_worker import DocumentWorker
    async def run():
        f = fixture()
        service = DocumentService(f.repo)
        metadata = ImportMetadata(import_id=uuid4(), source_key='fixture', filename='otter.md', title='Otter', expected_version=0)
        scope = DocumentScope(TENANT, (ROOM,))
        await service.import_bytes(scope, metadata, b'The paper otter carries a green notebook.')
        await DocumentWorker(f.repo).process_once(scope)
        f.chat.service = service
        async def ai(prompt):
            evidence = json.loads(prompt.split('\n')[-1])['evidence']
            assert 'green notebook' in evidence[0]['text']
            return {'answer': 'A green notebook.', 'supported': True, 'sources': ['D1']}
        reply = await f.send('What does otter.md say the paper otter carries?', synthesize=ai)
        assert 'green notebook' in reply and 'stored text lines 1' in reply
    asyncio.run(run())
