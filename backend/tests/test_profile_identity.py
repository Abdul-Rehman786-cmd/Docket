"""Sender identity must control personal lookups and save confirmations."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.core.http_clients import use_http_clients
from app.models.ai import LLMDecision
from app.repositories.message_receipts import MessageReceiptRepository
from app.repositories.sqlite_db import init_sqlite_db
from app.services import ai_service
from app.services.profile_service import answer_personal_profile, confirmation_key, personal_profile_question
from test_profiles import settings, save, rows, meta, proposal
from test_useful_storage import config, message


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.parametrize('text,kind', [
    ('@team-memory-client what was my prefered name?', 'preferred_name'),
    ('@team-memory-client what is my preferred name ?', 'preferred_name'),
    ('What do you call me?', 'preferred_name'),
    ('What should I be called?', 'preferred_name'),
    ('Who am I?', 'profile'),
    ('Tell me my preferences', 'preference'),
    ('What is my timezone?', 'timezone'),
    ('Show my skills', 'skill'),
    ('List my responsibilities', 'responsibility'),
    ('my profile', 'profile'),
    ('I prefer to be called Harry Potter.', None),
    ('I prefer to be called by my prefered name everytime I call u', None),
    ('What is the team name?', None),
    ("What is Alex's preferred name?", None),
    ('What is my project name?', None),
    ('What are my tasks?', None),
])
def test_original_question_routes_without_model_identity(text, kind):
    assert personal_profile_question(text) == kind


@pytest.mark.anyio
async def test_same_display_names_distinct_ids_and_unknown_sender(settings):
    await save(settings, message='a', sender='sender-a', body='I prefer to be called Harry Potter.',
               attribute='preference', value='to be called Harry Potter', name='Same display name')
    await save(settings, message='b', sender='sender-b', body='I prefer to be called Hermione.',
               attribute='preference', value='to be called Hermione', name='Same display name')
    async def own(sender, **updates):
        return await answer_personal_profile(settings, {**meta(sender), 'room_id': 'test-room', **updates}, 'preferred_name')
    a, a_sources = await own('sender-a', author_name='Renamed display label')
    b, b_sources = await own('sender-b')
    assert 'Harry Potter' in a and 'Hermione' not in a
    assert 'Hermione' in b and 'Harry Potter' not in b
    assert a_sources.isdisjoint(b_sources)
    for sender, updates in [('new-person', {}), ('sender-a', {'room_id': 'second-room'})]:
        answer, sources = await own(sender, **updates)
        assert 'do not have' in answer and 'Harry Potter' not in answer and not sources
    for metadata in ({}, {**meta('sender-a'), 'origin': 'viewer'}, {**meta('sender-a'), 'durable': False},
                     {**meta('sender-a'), 'switch_author_id': None}):
        answer, sources = await answer_personal_profile(settings, metadata, 'preferred_name')
        assert 'cannot identify' in answer and not sources
    with pytest.raises(HTTPException) as denied:
        await own('sender-a', room_id='not-allowed')
    assert denied.value.status_code == 403


@pytest.mark.anyio
async def test_conflicting_names_and_supersession(settings):
    for message_id, value in [('one', 'Harry Potter'), ('two', 'Ron Weasley')]:
        await save(settings, message=message_id, body=f'I prefer to be called {value}.',
                   attribute='preference', value=f'to be called {value}')
    metadata = {**meta(), 'room_id': 'test-room'}
    answer, _ = await answer_personal_profile(settings, metadata, 'preferred_name')
    assert 'conflicting' in answer and 'Harry Potter' in answer and 'Ron Weasley' in answer
    await save(settings, message='correction', body='My preference is now to be called Hermione.',
               attribute='preference', value='to be called Hermione', correction=True, when='2026-11-07T12:00:00Z')
    answer, _ = await answer_personal_profile(settings, metadata, 'preferred_name')
    assert 'Hermione' in answer and 'Harry Potter' not in answer and 'Ron Weasley' not in answer


@pytest.mark.anyio
async def test_supabase_lookup_filters_subject_before_evidence(settings):
    active = settings.model_copy(update={'database_engine': 'supabase', 'supabase_url': 'https://example.invalid',
                                        'supabase_service_role_key': 'fixture-placeholder'})
    tenant, room, profile, source = str(active.switch_tenant_id), str(uuid4()), str(uuid4()), str(uuid4())
    seen = []
    wrong_subject = False
    def handler(request):
        table = request.url.path.rsplit('/', 1)[-1]
        p = request.url.params
        seen.append(table)
        assert request.method == 'GET' and p['tenant_id'] == f'eq.{tenant}'
        if table == 'rooms':
            assert p['switch_room_id'] == 'eq.test-room' and p['enabled'] == 'eq.true'
            return httpx.Response(200, json=[dict(id=room, tenant_id=tenant, switch_room_id='test-room', enabled=True)])
        assert p['room_id'] == f'eq.{room}'
        if table == 'person_profiles':
            assert p['switch_author_id'] == 'eq.sender-1'
            return httpx.Response(200, json=[dict(id=profile, tenant_id=tenant, room_id=room,
                switch_author_id='another-person' if wrong_subject else 'sender-1')])
        assert table == 'person_profile_context'
        assert p['profile_id'] == f'eq.{profile}' and p['status'] == 'eq.active'
        return httpx.Response(200, json=[dict(tenant_id=tenant, room_id=room, profile_id=profile,
            status='active', attribute='preference', value='to be called Harry Potter', message_id=source)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with use_http_clients(SimpleNamespace(database=client)):
            answer, sources = await answer_personal_profile(active, {**meta(), 'room_id': 'test-room'}, 'preferred_name')
            assert 'Harry Potter' in answer and sources == {source}
            assert seen == ['rooms', 'person_profiles', 'person_profile_context']
            wrong_subject = True
            seen.clear()
            with pytest.raises(HTTPException) as rejected:
                await answer_personal_profile(active, {**meta(), 'room_id': 'test-room'}, 'preferred_name')
            assert rejected.value.status_code == 503
            assert seen == ['rooms', 'person_profiles']


@pytest.mark.anyio
async def test_worker_never_uses_shared_rag_or_model_for_my_name(settings, monkeypatch):
    active = config(switch_tenant_id=settings.switch_tenant_id, database_engine='sqlite', sqlite_db_path=settings.sqlite_db_path,
                    person_profiles_enabled=True, storage_retention_unit='indefinite', switch_reply_to_discord=False)
    await save(active, sender='sender-a', message='saved-a', body='I prefer to be called Harry Potter.',
               attribute='preference', value='to be called Harry Potter')
    await save(active, sender='sender-b', message='saved-b', body='I prefer to be called Hermione.',
               attribute='preference', value='to be called Hermione')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Personal reads must never enter model/shared-room retrieval')
    monkeypatch.setattr(ai_service, 'query_team_memory', forbidden)
    monkeypatch.setattr(ai_service, 'ask_switch', forbidden)
    service, receipts = ai_service.AIService(), MessageReceiptRepository()
    try:
        for index, (sender, expected, absent) in enumerate([
            ('sender-a', 'Harry Potter', 'Hermione'), ('sender-b', 'Hermione', 'Harry Potter'),
            ('new-person', 'do not have', 'Harry Potter'),
        ]):
            receipt = await service.ingest(message(active, '@team-memory-client what is my preferred name ?',
                sender=sender, sender_name='Same label', message_id=f'query-{index}'), active, receipts)
            await service.queue.join()
            result = service.repository.get_by_source(ai_service.response_scope(active), receipt.message_ref)
            assert result.status == 'answered', result.error
            assert expected in result.answer and absent not in result.answer
            if sender != 'new-person':
                assert '[source:' in result.answer
    finally:
        await service.close()


@pytest.mark.anyio
async def test_confirmation_drafts_belong_to_senders(settings, monkeypatch):
    active = config(switch_tenant_id=settings.switch_tenant_id, database_engine='sqlite', sqlite_db_path=settings.sqlite_db_path,
                    person_profiles_enabled=True, storage_retention_unit='indefinite', switch_reply_to_discord=False)
    init_sqlite_db(active)
    async def ask(_settings, question, **kwargs):
        if question == 'yes':
            return LLMDecision(action='none').model_dump_json()
        value = question.removeprefix('I prefer ').rstrip('.')
        return proposal(question, 'preference', value).model_dump_json()
    monkeypatch.setattr(ai_service, 'ask_switch', ask)
    service, receipts = ai_service.AIService(), MessageReceiptRepository()
    try:
        for index, (sender, name) in enumerate([('sender-a', 'Harry Potter'), ('sender-b', 'Hermione')]):
            await service.ingest(message(active, f'I prefer to be called {name}.', sender=sender,
                message_id=f'draft-{index}', addressed=True), active, receipts)
            await service.queue.join()
        key_a = confirmation_key(active, 'test-room', 'thread-1', 'sender-a')
        key_b = confirmation_key(active, 'test-room', 'thread-1', 'sender-b')
        assert key_a != key_b
        assert await service.pending_confirmations.get(key_a)
        assert await service.pending_confirmations.get(key_b)
        await service.ingest(message(active, 'yes', sender='third-person', message_id='wrong-confirm'), active, receipts)
        await service.queue.join()
        assert rows(active, 'person_profiles') == []
        await service.ingest(message(active, 'yes', sender='sender-a', message_id='right-confirm'), active, receipts)
        assert rows(active, 'person_profiles')[0]['switch_author_id'] == 'sender-a'
        assert await service.pending_confirmations.get(key_a) is None
        assert await service.pending_confirmations.get(key_b)
        await service.ingest(message(active, 'no', sender='sender-b', message_id='right-cancel'), active, receipts)
        assert await service.pending_confirmations.get(key_b) is None
        assert len(rows(active, 'person_profiles')) == 1
    finally:
        await service.close()
