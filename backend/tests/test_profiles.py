"""Offline profile behavior: real SQLite transactions, HTTP authorization, and PostgreSQL."""

import json
import sqlite3
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import Settings, get_settings
from app.core.profile_identity import resolve_profile_actor
from app.main import app
from app.models.ai import LLMDecision
from app.models.commitments import Actor, Scope
from app.models.profiles import ExtractedProfileFact
from app.repositories import record_repository, sqlite_db
from app.services.extraction_service import storage_items, store_ai_memory
from app.services.profile_service import filter_profile_decision, profile_items, read_profile
from app.services.retrieval_service import query_team_memory


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_engine='sqlite', sqlite_db_path=str(tmp_path / 'profiles.db'),
        person_profiles_enabled=True, switch_tenant_id=uuid4(), switch_room_ids='test-room,second-room',
        storage_retention_unit='indefinite', sqlite_max_space_enabled=False, local_embeddings_enabled=False)


def proposal(body='I maintain the billing service.', attribute='responsibility', value='the billing service', correction=False):
    return LLMDecision(action='store_only', profile_facts=[ExtractedProfileFact(
        attribute=attribute, value=value, evidence=body, correction=correction)])


def meta(sender='sender-1'):
    return {'origin': 'discord', 'durable': True, 'switch_author_id': sender, 'author_name': 'Alex'}


async def save(settings, *, message='m1', body='I maintain the billing service.', attribute='responsibility',
               value='the billing service', correction=False, when='2026-11-06T12:00:00Z', sender='sender-1', room='test-room', name='Alex'):
    return await store_ai_memory(settings, tenant_id=settings.switch_tenant_id, switch_room_id=room,
        switch_message_id=message, author_name=name, body=body, occurred_at=when,
        decision=proposal(body, attribute, value, correction), source_metadata={**meta(sender), 'author_name': name})


def rows(settings, table):
    with sqlite_db.get_sqlite_connection(settings) as conn:
        return [dict(r) for r in conn.execute(f'SELECT * FROM {table}')]


def test_disabled_untrusted_and_unsupported_proposals(settings):
    decision = proposal()
    assert filter_profile_decision(decision, settings.model_copy(update={'person_profiles_enabled': False}), meta(), decision.profile_facts[0].evidence).action == 'none'
    for metadata in ({}, {**meta(), 'origin': 'viewer'}, {**meta(), 'durable': False}, meta('not an id')):
        with pytest.raises(HTTPException):
            profile_items(decision.profile_facts, settings, metadata, decision.profile_facts[0].evidence)
    for body in ('Alex maintains billing.', '"I maintain the billing service."', 'If I maintain billing.',
                 'I maintain the billing service?', 'I might maintain billing.', 'Please remember: I maintain billing.'):
        with pytest.raises(HTTPException):
            profile_items(proposal(body).profile_facts, settings, meta(), body)
    with pytest.raises(HTTPException):
        profile_items(proposal().profile_facts, settings, meta(), 'I maintain a different service.')


def test_existing_sqlite_upgrade_and_safe_citations(settings):
    from app.services.profile_service import redact_profile_answer
    # Upgrade an existing foundation database without replacing its source rows.
    with sqlite_db.get_sqlite_connection(settings) as conn:
        conn.executescript(sqlite_db.SCHEMA_SQL)
        conn.execute('INSERT INTO tenants(id,name) VALUES(?,?)', (str(settings.switch_tenant_id), 'Retained team'))
        conn.commit()
    sqlite_db.init_sqlite_db(settings)
    sqlite_db.init_sqlite_db(settings)
    assert rows(settings, 'tenants')[0]['name'] == 'Retained team'
    assert len(rows(settings, 'local_schema_migrations')) == 1
    source, invented = str(uuid4()), str(uuid4())
    settings = settings.model_copy(update={'admin_api_token': 'fictional-sensitive-value'})
    answer = redact_profile_answer(f'Reported UTC [source:{source}] [source:{invented}] fictional-sensitive-value', settings, {source})
    assert f'[source:{source}]' in answer
    assert invented not in answer and 'fictional-sensitive-value' not in answer


@pytest.mark.anyio
async def test_existing_ingestion_stores_profile_silently(settings, monkeypatch):
    from app.services import ai_service
    from app.repositories.message_receipts import MessageReceiptRepository
    from test_useful_storage import config, message
    active = config(database_engine='sqlite', sqlite_db_path=settings.sqlite_db_path,
                    person_profiles_enabled=True, storage_retention_unit='indefinite', switch_reply_to_discord=False)
    body = 'I maintain the billing service.'
    async def ask(*args, **kwargs):
        return proposal(body).model_dump_json()
    monkeypatch.setattr(ai_service, 'ask_switch', ask)
    service = ai_service.AIService()
    try:
        await service.ingest(message(active, body), active, MessageReceiptRepository())
        await service.queue.join()
        result = service.repository.recent(ai_service.response_scope(active))[0]
        assert result.status == 'answered'
        assert len(rows(active, 'person_profiles')) == len(rows(active, 'person_profile_evidence')) == 1
        assert rows(active, 'person_profiles')[0]['switch_author_id'] == 'human'
    finally:
        await service.close()


@pytest.mark.anyio
async def test_identity_replay_and_conflict(settings):
    first = await save(settings)
    assert await save(settings) == first
    assert len(rows(settings, 'messages')) == len(rows(settings, 'person_profile_evidence')) == 1
    profile = rows(settings, 'person_profiles')[0]
    UUID(profile['id'])
    assert profile['switch_author_id'] == 'sender-1'
    await save(settings, message='m2', name='Renamed Alex', when='2026-11-06T13:00:00Z')
    assert rows(settings, 'person_profiles')[0]['id'] == profile['id']
    assert rows(settings, 'person_profiles')[0]['display_name'] == 'Renamed Alex'
    assert len(rows(settings, 'person_profile_claims')) == 1
    await save(settings, message='m3', sender='sender-2')
    await save(settings, message='m4', room='second-room')
    assert len(rows(settings, 'person_profiles')) == 3
    with pytest.raises(HTTPException) as error:
        await save(settings, sender='other-sender')
    assert error.value.status_code == 409
    assert len(rows(settings, 'messages')) == 4


@pytest.mark.anyio
async def test_correction_order_retention_and_search(settings):
    await save(settings, body='My timezone is UTC.', attribute='timezone', value='UTC')
    await save(settings, message='correction', body='My timezone is now Asia/Karachi.', attribute='timezone',
               value='Asia/Karachi', correction=True, when='2026-11-07T12:00:00Z')
    await save(settings, message='delayed', body='My timezone is Europe/London.', attribute='timezone',
               value='Europe/London', when='2026-11-05T12:00:00Z')
    claims = {r['value']: r['status'] for r in rows(settings, 'person_profile_claims')}
    assert claims == {'UTC': 'superseded', 'Asia/Karachi': 'active', 'Europe/London': 'superseded'}
    result = await query_team_memory(settings, search_query='timezone', switch_room_id='test-room')
    assert len(result['memories']) == 1
    assert 'Asia/Karachi' in result['memories'][0]['content']
    assert any(r['status'] == 'superseded' for r in result['profile_reports'])
    with sqlite_db.get_sqlite_connection(settings) as conn, conn:
        # Source pruning also prunes claims/memories and recomputes retained evidence.
        conn.execute("DELETE FROM messages WHERE switch_message_id='correction'")
    assert {r['value'] for r in rows(settings, 'person_profile_claims')} == {'UTC', 'Europe/London'}
    assert all(r['status'] == 'active' for r in rows(settings, 'person_profile_claims'))
    with sqlite_db.get_sqlite_connection(settings) as conn, conn:
        conn.execute('DELETE FROM memories')
    assert rows(settings, 'person_profile_evidence') == []
    assert rows(settings, 'person_profile_claims') == []
    assert rows(settings, 'person_profiles') == []


@pytest.mark.anyio
async def test_rollback_and_cross_room_evidence(settings, monkeypatch):
    from app.repositories import sqlite_profiles
    original = sqlite_profiles.store
    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.IntegrityError('synthetic failure')
    monkeypatch.setattr(sqlite_profiles, 'store', fail_after_write)
    with pytest.raises(sqlite3.IntegrityError):
        await save(settings)
    assert rows(settings, 'messages') == rows(settings, 'person_profiles') == rows(settings, 'memories') == []
    monkeypatch.setattr(sqlite_profiles, 'store', original)
    await save(settings)
    await save(settings, message='other', room='second-room')
    evidence = rows(settings, 'person_profile_evidence')[0]
    other = rows(settings, 'person_profile_evidence')[1]
    with sqlite_db.get_sqlite_connection(settings) as conn, conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE person_profile_evidence SET message_id=? WHERE claim_id=?',
                         (other['message_id'], evidence['claim_id']))


@pytest.mark.anyio
async def test_authorized_read_conflicts_and_http_fail_closed(settings):
    await save(settings, body='My timezone is UTC.', attribute='timezone', value='UTC')
    await save(settings, message='different', body='My timezone is Asia/Karachi.', attribute='timezone', value='Asia/Karachi')
    profile = rows(settings, 'person_profiles')[0]
    scope = Scope(tenant_id=settings.switch_tenant_id, room_id=profile['room_id'])
    actor = Actor(subject_id='verified-person')
    with pytest.raises(HTTPException) as denied:
        await read_profile(settings, scope, actor, profile_id=UUID(profile['id']))
    assert denied.value.status_code == 403
    with sqlite_db.get_sqlite_connection(settings) as conn, conn:
        conn.execute('INSERT INTO room_members(tenant_id,room_id,switch_user_id) VALUES(?,?,?)',
                     (str(scope.tenant_id), str(scope.room_id), actor.subject_id))
    detail = await read_profile(settings, scope, actor, profile_id=UUID(profile['id']))
    assert detail['conflicting_attributes'] == ['timezone']
    assert len(detail['evidence']) == 2
    path = f'/api/v1/tenants/{scope.tenant_id}/rooms/{scope.room_id}/profiles'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for headers in ({}, {'X-Ingest-Token': 'test-ingest-token'}, {'X-User-Id': actor.subject_id}):
            response = await client.get(path, headers=headers)
            assert response.status_code == 503
            assert response.headers['cache-control'] == 'no-store'
        app.dependency_overrides[resolve_profile_actor] = lambda: actor
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            assert (await client.get(path)).status_code == 200
            assert (await client.get(path + '/' + profile['id'])).json()['conflicting_attributes'] == ['timezone']
            assert (await client.get(path.replace(str(scope.tenant_id), str(uuid4())))).status_code == 403
            assert (await client.get(path + '/' + str(uuid4()))).status_code == 404
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_profile_rpc_contract_and_safe_failure(settings, caplog):
    settings = settings.model_copy(update={'database_engine': 'supabase', 'supabase_url': 'https://example.invalid',
                                           'supabase_service_role_key': 'fixture-placeholder'})
    body = 'I maintain the billing service.'
    decision = proposal()
    items = storage_items(decision, settings, source_metadata=meta(), source_body=body)
    async def handler(request):
        assert request.url.path.endswith('/rpc/store_profile_message_extraction')
        payload = json.loads(request.content)
        assert payload['p_capture'] is False
        assert payload['p_source']['switch_author_id'] == 'sender-1'
        assert payload['p_items']['profile_facts'] == items['profile_facts']
        return httpx.Response(400, json={'message': 'DO_NOT_LOG_PRIVATE_EVIDENCE'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as unavailable:
            await record_repository.persist_extraction(settings, tenant_id=settings.switch_tenant_id,
                switch_room_id='test-room', switch_message_id='m1', author_name='Alex', body=body,
                occurred_at='2026-11-06T12:00:00Z', items=items, source_metadata=meta(), client=client)
    assert unavailable.value.status_code == 503
    assert 'DO_NOT_LOG_PRIVATE_EVIDENCE' not in caplog.text


def test_postgres_storage_replay_correction_and_deletion(settings):
    from test_capture_database import Database, SQL_AVAILABLE, seed
    if not SQL_AVAILABLE:
        pytest.skip('Existing disposable PostgreSQL dependencies unavailable')
    db = Database()
    try:
        tenant, room = seed(db)
        def store(message, body, value, correction=False, when='2026-11-06T12:00:00Z'):
            decision = proposal(body, 'timezone', value, correction)
            items = storage_items(decision, settings, source_metadata=meta(), source_body=body)
            source = dict(switch_message_id=message, switch_author_id='sender-1', author_name='Alex',
                          body=body, occurred_at=when)
            return db.query('SELECT public.store_profile_message_extraction($1::uuid,$2,$3::jsonb,$4::jsonb,false) result',
                            tenant, 'test-room', json.dumps(source), json.dumps(items))[0]['result']
        assert store('one', 'My timezone is UTC.', 'UTC') == store('one', 'My timezone is UTC.', 'UTC')
        db.query("SET TIME ZONE 'Asia/Karachi'")
        assert store('one', 'My timezone is UTC.', 'UTC')['messages'] == 1
        db.query("SET TIME ZONE 'UTC'")
        store('whitespace', '\nMy timezone is UTC.\n', 'UTC')
        store('two', 'My timezone is now Asia/Karachi.', 'Asia/Karachi', True, '2026-11-07T12:00:00Z')
        assert db.query("SELECT value,status FROM public.person_profile_claims ORDER BY value") == [
            {'value': 'Asia/Karachi', 'status': 'active'}, {'value': 'UTC', 'status': 'superseded'}]
        assert db.query('SELECT count(*)::int n FROM public.memories WHERE active')[0]['n'] == 1
        with pytest.raises(ValueError, match='profile_source_conflict'):
            store('one', 'My timezone is GMT.', 'GMT')
        db.query("DELETE FROM public.memories WHERE message_id IN (SELECT id FROM public.messages WHERE switch_message_id='two')")
        db.query("DELETE FROM public.messages WHERE switch_message_id='two'")
        assert db.query("SELECT value,status FROM public.person_profile_claims") == [{'value': 'UTC', 'status': 'active'}]
        db.query('DELETE FROM public.memories')
        assert db.query('SELECT count(*)::int n FROM public.person_profiles')[0]['n'] == 0
        assert db.query("SELECT has_function_privilege('anon','public.store_profile_message_extraction(uuid,text,jsonb,jsonb,boolean)','EXECUTE') allowed")[0]['allowed'] is False
        assert db.query("SELECT has_table_privilege('authenticated','public.person_profile_context','SELECT') allowed")[0]['allowed'] is False
        before = db.query('SELECT count(*)::int n FROM public.messages')[0]['n']
        # The existing extraction writes before the evidence check; a rejection
        # must roll back its message and memory along with the profile tables.
        good_body = 'My timezone is UTC.'
        items = storage_items(proposal(good_body, 'timezone', 'UTC'), settings, source_metadata=meta(), source_body=good_body)
        items['profile_facts'][0]['evidence'] = 'Unrelated source'
        source = dict(switch_message_id='rollback', switch_author_id='sender-1', author_name='Alex',
                      body=good_body, occurred_at='2026-11-06T12:00:00Z')
        with pytest.raises(ValueError, match='invalid_profile_evidence'):
            db.query('SELECT public.store_profile_message_extraction($1::uuid,$2,$3::jsonb,$4::jsonb,false)',
                     tenant, 'test-room', json.dumps(source), json.dumps(items))
        assert db.query('SELECT count(*)::int n FROM public.messages')[0]['n'] == before
    finally:
        db.close()


@pytest.mark.anyio
async def test_profile_migration_repeat_and_rollback(settings, tmp_path, monkeypatch):
    from app.services.migration_service import migrate_supabase_to_sqlite
    from app.repositories.profile_repository import TABLES
    await save(settings, body='My timezone is UTC.', attribute='timezone', value='UTC')
    await save(settings, message='other', body='My timezone is Asia/Karachi.', attribute='timezone', value='Asia/Karachi')
    tables = ['tenants', 'rooms', 'room_members', 'messages', 'commitments', 'decisions', 'memories', *TABLES]
    source = {table: rows(settings, table) for table in tables}
    broken = False
    async def handler(request):
        table = request.url.path.rsplit('/', 1)[-1]
        if broken and table == 'person_profile_evidence':
            return httpx.Response(500, json={'message': 'synthetic error'})
        if table in source:
            return httpx.Response(200, json=source[table])
        return httpx.Response(404, json=[])
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handler)))
    target = settings.model_copy(update={'sqlite_db_path': str(tmp_path / 'imported.db'),
        'supabase_url': 'https://example.invalid', 'supabase_service_role_key': 'fixture-placeholder'})
    await migrate_supabase_to_sqlite(target)
    await migrate_supabase_to_sqlite(target)
    assert len(rows(target, 'person_profile_claims')) == 2
    assert len(rows(target, 'person_profile_evidence')) == 2
    assert {r['id'] for r in rows(target, 'person_profiles')} == {r['id'] for r in source['person_profiles']}
    broken = True
    source['person_profiles'][0]['display_name'] = 'Must roll back'
    with pytest.raises(HTTPException):
        await migrate_supabase_to_sqlite(target)
    assert rows(target, 'person_profiles')[0]['display_name'] == 'Alex'
    with sqlite_db.get_sqlite_connection(target) as conn, conn:
        conn.execute('DELETE FROM memories')
    assert rows(target, 'person_profiles') == []
