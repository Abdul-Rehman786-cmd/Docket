"""Bounded profile reads with tenant, configured room and membership checks."""

from uuid import UUID
import httpx
from fastapi import HTTPException

from app.core.http_clients import database_client
from app.repositories.record_repository import _rest_url, _supabase_headers
from app.repositories.sqlite_db import get_sqlite_connection, init_sqlite_db

TABLES = {
    'person_profiles': 'id,tenant_id,room_id,switch_author_id,display_name,created_at',
    'person_profile_claims': 'id,tenant_id,room_id,profile_id,extraction_key,attribute,value,status,created_at',
    'person_profile_evidence': 'tenant_id,room_id,claim_id,message_id,memory_id,quote,correction,occurred_at',
}


async def remote_rows(settings, client, table, **filters):
    try:
        response = await client.get(_rest_url(settings, table), headers=_supabase_headers(settings),
            params={'select': TABLES.get(table, '*'), 'limit': '201', **filters}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError('Invalid profile response')
        return rows
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(503, 'profile_storage_unavailable') from None


@database_client
async def read_profiles(settings, scope, actor, *, profile_id=None, limit=50, after=None, client):
    if not settings.person_profiles_enabled:
        raise HTTPException(503, 'Person profiles are disabled')
    if scope.tenant_id != settings.switch_tenant_id:
        raise HTTPException(403, 'Room is not authorized')
    tenant, room = str(scope.tenant_id), str(scope.room_id)
    if settings.effective_database_engine == 'sqlite':
        init_sqlite_db(settings)
        with get_sqlite_connection(settings) as conn:
            with conn:
                conn.execute('BEGIN')
                found = conn.execute('SELECT switch_room_id FROM rooms WHERE tenant_id=? AND id=?', (tenant, room)).fetchone()
                member = conn.execute('SELECT 1 FROM room_members WHERE tenant_id=? AND room_id=? AND switch_user_id=?',
                                      (tenant, room, actor.subject_id)).fetchone()
                if not found or found[0] not in settings.allowed_switch_rooms or not member:
                    raise HTTPException(403, 'Room is not authorized')
                params = [tenant, room]
                selector = ''
                if profile_id:
                    selector = ' AND id=?'
                    params.append(str(profile_id))
                elif after:
                    selector = ' AND id>?'
                    params.append(str(after))
                profiles = [dict(r) for r in conn.execute('SELECT * FROM person_profiles WHERE tenant_id=? AND room_id=?' +
                    selector + ' ORDER BY id LIMIT ?', (*params, limit + 1))]
                if not profile_id:
                    return {'items': profiles[:limit], 'next_cursor': profiles[limit-1]['id'] if len(profiles) > limit else None}
                if not profiles:
                    raise HTTPException(404, 'Profile not found')
                claims = [dict(r) for r in conn.execute('SELECT * FROM person_profile_claims WHERE tenant_id=? AND room_id=? '
                    'AND profile_id=? ORDER BY id LIMIT 201', (tenant, room, str(profile_id)))]
                evidence = [dict(r) for r in conn.execute('SELECT e.* FROM person_profile_evidence e '
                    'JOIN person_profile_claims c ON c.id=e.claim_id WHERE c.tenant_id=? AND c.room_id=? AND c.profile_id=? '
                    'ORDER BY e.occurred_at DESC,e.message_id LIMIT 201', (tenant, room, str(profile_id)))]
    else:
        filters = {'tenant_id': f'eq.{tenant}', 'room_id': f'eq.{room}'}
        found = await remote_rows(settings, client, 'rooms', id=f'eq.{room}', tenant_id=f'eq.{tenant}', enabled='eq.true')
        member = await remote_rows(settings, client, 'room_members', **filters, switch_user_id=f'eq.{actor.subject_id}')
        if (not found or found[0].get('switch_room_id') not in settings.allowed_switch_rooms
                or str(found[0].get('id')) != room or str(found[0].get('tenant_id')) != tenant
                or not any(str(m.get('tenant_id')) == tenant and str(m.get('room_id')) == room
                           and m.get('switch_user_id') == actor.subject_id for m in member)):
            raise HTTPException(403, 'Room is not authorized')
        profiles = await remote_rows(settings, client, 'person_profiles', **filters, order='id', limit=str(limit + 1),
                                     **({'id': f'eq.{profile_id}'} if profile_id else {'id': f'gt.{after}'} if after else {}))
        if not profile_id:
            return {'items': profiles[:limit], 'next_cursor': profiles[limit-1]['id'] if len(profiles) > limit else None}
        if not profiles:
            raise HTTPException(404, 'Profile not found')
        claims = await remote_rows(settings, client, 'person_profile_claims', **filters, profile_id=f'eq.{profile_id}', order='id')
        if claims:
            ids = ','.join(str(UUID(c['id'])) for c in claims[:200])
            evidence = await remote_rows(settings, client, 'person_profile_evidence', **filters,
                                         claim_id=f'in.({ids})', order='occurred_at.desc,message_id')
        else:
            evidence = []
    # Bounded, explicit truncation. A retained claim is never returned without evidence.
    shown = {e['claim_id'] for e in evidence[:200]}
    claims_shown = [c for c in claims[:200] if c['id'] in shown]
    valid_claims = {c['id'] for c in claims_shown}
    return {'profile': profiles[0], 'claims': claims_shown,
            'evidence': [e for e in evidence[:200] if e['claim_id'] in valid_claims],
            'truncated': len(claims) > 200 or len(evidence) > 200 or len(claims_shown) != len(claims)}


@database_client
async def retrieval_context(settings, records, *, switch_room_id=None, client):
    """Expand matched profiles to their retained reports, including contradictions."""
    from app.repositories.embedding_repository import authorized_room_ids
    memory_ids = [str(UUID(r['id'])) for r in records.get('memories', [])]
    message_ids = [str(UUID(r['id'])) for r in records.get('messages', [])]
    if not memory_ids and not message_ids:
        return []
    if settings.effective_database_engine == 'sqlite':
        from app.repositories.sqlite_embeddings import _authorized_room_ids
        init_sqlite_db(settings)
        with get_sqlite_connection(settings) as conn:
            rooms = _authorized_room_ids(settings, conn, switch_room_id)
            if not rooms:
                return []
            room_slots = ','.join('?' for _ in rooms)
            mem_slots = ','.join('?' for _ in memory_ids) or 'NULL'
            msg_slots = ','.join('?' for _ in message_ids) or 'NULL'
            scope_sql = f'tenant_id=? AND room_id IN ({room_slots})'
            seeds = conn.execute(f'SELECT DISTINCT profile_id FROM person_profile_context WHERE {scope_sql} '
                f'AND (memory_id IN ({mem_slots}) OR message_id IN ({msg_slots}))',
                (str(settings.switch_tenant_id), *rooms, *memory_ids, *message_ids)).fetchall()
            ids = [r[0] for r in seeds]
            if not ids:
                return []
            slots = ','.join('?' for _ in ids)
            return [dict(r) for r in conn.execute(f'SELECT * FROM person_profile_context WHERE {scope_sql} '
                f'AND profile_id IN ({slots}) ORDER BY occurred_at DESC LIMIT 201', (str(settings.switch_tenant_id), *rooms, *ids))]
    rooms = await authorized_room_ids(settings, client, switch_room_id)
    filters = {'tenant_id': f'eq.{settings.switch_tenant_id}', 'room_id': f"in.({','.join(rooms)})"}
    clauses = []
    if memory_ids:
        clauses.append(f"memory_id.in.({','.join(memory_ids)})")
    if message_ids:
        clauses.append(f"message_id.in.({','.join(message_ids)})")
    seeds = await remote_rows(settings, client, 'person_profile_context', **filters, **{'or': '(' + ','.join(clauses) + ')'})
    ids = sorted({str(UUID(r['profile_id'])) for r in seeds})
    if not ids:
        return []
    return await remote_rows(settings, client, 'person_profile_context', **filters,
                             profile_id=f"in.({','.join(ids)})", order='occurred_at.desc')


@database_client
async def sender_profile_reports(settings, *, switch_room_id, switch_author_id, client):
    """Exact subject lookup for trusted ingestion. Never perform room-wide fallback."""
    if settings.switch_tenant_id is None or switch_room_id not in settings.allowed_switch_rooms:
        raise HTTPException(403, 'Room is not authorized')
    tenant = str(settings.switch_tenant_id)
    if settings.effective_database_engine == 'sqlite':
        init_sqlite_db(settings)
        with get_sqlite_connection(settings) as conn, conn:
            conn.execute('BEGIN')
            return [dict(r) for r in conn.execute(
                'SELECT ctx.* FROM person_profile_context ctx '
                'JOIN person_profiles p ON p.id=ctx.profile_id AND p.tenant_id=ctx.tenant_id AND p.room_id=ctx.room_id '
                'JOIN rooms r ON r.id=p.room_id AND r.tenant_id=p.tenant_id '
                "WHERE p.tenant_id=? AND r.switch_room_id=? AND p.switch_author_id=? AND ctx.status='active' "
                'ORDER BY ctx.occurred_at DESC,ctx.message_id LIMIT 201',
                (tenant, switch_room_id, switch_author_id))]
    # Read-only room resolution: a personal query must not provision rooms.
    rooms = await remote_rows(settings, client, 'rooms', tenant_id=f'eq.{tenant}',
        switch_room_id=f'eq.{switch_room_id}', enabled='eq.true', select='id,tenant_id,switch_room_id,enabled', limit='2')
    if not rooms:
        raise HTTPException(403, 'Room is not authorized')
    if (len(rooms) != 1 or rooms[0].get('tenant_id') != tenant or not rooms[0].get('enabled')
            or rooms[0].get('switch_room_id') != switch_room_id):
        raise HTTPException(503, 'Invalid profile room result')
    try:
        room = str(UUID(rooms[0]['id']))
        filters = {'tenant_id': f'eq.{tenant}', 'room_id': f'eq.{room}'}
        profiles = await remote_rows(settings, client, 'person_profiles', **filters,
                                     switch_author_id=f'eq.{switch_author_id}', limit='2')
        if not profiles:
            return []
        if (len(profiles) != 1 or profiles[0].get('tenant_id') != tenant or profiles[0].get('room_id') != room
                or profiles[0].get('switch_author_id') != switch_author_id):
            raise ValueError('Invalid profile subject')
        profile_id = str(UUID(profiles[0]['id']))
        reports = await remote_rows(settings, client, 'person_profile_context', **filters,
            profile_id=f'eq.{profile_id}', status='eq.active', order='occurred_at.desc,message_id')
        if any(r.get('tenant_id') != tenant or r.get('room_id') != room or r.get('profile_id') != profile_id
               or r.get('status') != 'active' for r in reports):
            raise ValueError('Invalid profile scope')
        return reports
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, 'Invalid profile storage result') from None
