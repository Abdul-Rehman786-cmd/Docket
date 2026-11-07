"""SQLite reminder repository. Implements ReminderRepository protocol for embedded storage."""
from datetime import datetime, timedelta, timezone
import logging
import re
from uuid import UUID, uuid4
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.models.commitments import CommitmentError, CommitmentState
from app.models.reminders import (
    DispatchContext,
    ReminderDelivery,
    ReminderHistory,
    ReminderIntention,
    ReminderPolicy,
    ReminderWork,
)
from app.repositories.sqlite_db import get_sqlite_connection, init_sqlite_db

logger = logging.getLogger("uvicorn.error")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class SqliteReminderRepository:
    def __init__(self, settings: Settings, clock=lambda: datetime.now(timezone.utc)):
        self.settings = settings
        self.clock = clock
        init_sqlite_db(settings)

    def scope(self):
        if not self.settings.switch_tenant_id or not self.settings.allowed_switch_rooms:
            raise CommitmentError("reminder_scope_unavailable", 503)
        return str(self.settings.switch_tenant_id), sorted(self.settings.allowed_switch_rooms)

    def _ensure_policy(self, conn, tenant_id: str, room_id: str) -> dict:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM commitment_chat_policies WHERE tenant_id = ? AND room_id = ?",
            (tenant_id, room_id),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        lead = self.settings.reminder_lead_minutes
        enabled = 1 if self.settings.reminders_enabled else 0
        tz = self.settings.commitment_timezone or "UTC"
        cursor.execute(
            """
            INSERT INTO commitment_chat_policies (
                tenant_id, room_id, reminders_enabled, reminder_lead_minutes, timezone
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (tenant_id, room_id, enabled, lead, tz),
        )
        cursor.execute(
            "SELECT * FROM commitment_chat_policies WHERE tenant_id = ? AND room_id = ?",
            (tenant_id, room_id),
        )
        return dict(cursor.fetchone())

    async def work(self, limit: int, after: str | None = None) -> tuple[list[ReminderWork], str | None]:
        tenant_str, allowed_rooms = self.scope()
        if limit < 1 or limit > 100:
            raise CommitmentError("invalid_action_payload", 422)

        with get_sqlite_connection(self.settings) as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in allowed_rooms)
            query = f"""
                SELECT c.id, c.tenant_id, c.room_id, c.message_id, c.description,
                       COALESCE(c.owner_name, m.author_name) AS owner_name,
                       c.due_at, c.approval_status, c.task_status, c.created_at,
                       r.switch_room_id, 1 AS room_enabled,
                       m.switch_author_id, NULL AS thread_id,
                       COALESCE(r.platform, 'discord') AS platform
                FROM commitments c
                JOIN rooms r ON r.tenant_id = c.tenant_id AND r.id = c.room_id
                JOIN messages m ON m.id = c.message_id
                WHERE c.tenant_id = ?
                  AND r.switch_room_id IN ({placeholders})
                  AND c.task_status NOT IN ('completed', 'cancelled')
                  AND c.due_at IS NOT NULL
            """
            params: list = [tenant_str, *allowed_rooms]
            if after:
                query += " AND c.id > ?"
                params.append(after)
            query += " ORDER BY c.id ASC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            rows = cursor.fetchall()
            items: list[ReminderWork] = []

            for row in rows:
                c_dict = dict(row)
                policy_dict = self._ensure_policy(conn, c_dict["tenant_id"], c_dict["room_id"])
                due_dt = _parse_iso(c_dict["due_at"])
                created_dt = _parse_iso(c_dict["created_at"]) or _utc_now()
                owner_id = c_dict.get("switch_author_id") or "unassigned"

                # Check deliveries for this commitment to compute history
                cursor.execute(
                    """
                    SELECT status, reminder_type, commitment_version, policy_version,
                           sent_at, next_attempt_at, dispatch_started_at
                    FROM reminder_deliveries
                    WHERE commitment_id = ?
                    """,
                    (c_dict["id"],),
                )
                deliv_rows = cursor.fetchall()

                # Room last sent
                cursor.execute(
                    "SELECT MAX(sent_at) FROM reminder_deliveries WHERE room_id = ? AND status = 'sent'",
                    (c_dict["room_id"],),
                )
                last_room_sent_raw = cursor.fetchone()[0]
                last_room_sent_at = _parse_iso(last_room_sent_raw)

                upcoming_sent = False
                overdue_sent = 0
                last_sent_at = None
                last_overdue_at = None
                blocking = False
                now_dt = _utc_now()

                c_version = 1
                p_version = policy_dict.get("reminder_policy_version", 1)

                for d in deliv_rows:
                    d_status = d["status"]
                    d_type = d["reminder_type"]
                    d_c_ver = d["commitment_version"]
                    d_p_ver = d["policy_version"]
                    d_sent_at = _parse_iso(d["sent_at"])
                    d_dispatch = _parse_iso(d["dispatch_started_at"])

                    if d_status == "sent":
                        if d_c_ver == c_version and d_p_ver == p_version:
                            if d_type == "upcoming":
                                upcoming_sent = True
                            elif d_type == "overdue":
                                overdue_sent += 1
                                if d_sent_at and (last_overdue_at is None or d_sent_at > last_overdue_at):
                                    last_overdue_at = d_sent_at
                        if d_sent_at and (last_sent_at is None or d_sent_at > last_sent_at):
                            last_sent_at = d_sent_at

                    if d_c_ver == c_version and d_p_ver == p_version and d_status in ("pending", "sending", "failed", "ambiguous"):
                        if d_status == "ambiguous" or (d_status == "failed" and d["next_attempt_at"] is None) or d_type != "upcoming" or (due_dt and due_dt > now_dt) or d_dispatch is not None:
                            blocking = True

                policy = ReminderPolicy(
                    version=policy_dict.get("reminder_policy_version", 1),
                    enabled=bool(policy_dict.get("reminders_enabled", 0)),
                    timezone=policy_dict.get("timezone") or self.settings.commitment_timezone or "UTC",
                    lead_minutes=policy_dict.get("reminder_lead_minutes", self.settings.reminder_lead_minutes),
                    cadence_minutes=policy_dict.get("reminder_cadence_minutes", 1440),
                    max_followups=policy_dict.get("reminder_max_followups", 3),
                    minimum_gap_minutes=policy_dict.get("reminder_gap_minutes", 30),
                    room_gap_seconds=policy_dict.get("reminder_room_gap_seconds", 60),
                    quiet_start=policy_dict.get("reminder_quiet_start", 22),
                    quiet_end=policy_dict.get("reminder_quiet_end", 8),
                    delivery_mode=policy_dict.get("reminder_delivery_mode", "room"),
                )

                history = ReminderHistory(
                    upcoming_sent=upcoming_sent,
                    overdue_sent=overdue_sent,
                    last_sent_at=last_sent_at,
                    last_overdue_at=last_overdue_at,
                    last_room_sent_at=last_room_sent_at,
                    blocking_delivery=blocking,
                )

                # Commitment state
                comm = CommitmentState(
                    id=UUID(c_dict["id"]),
                    tenant_id=UUID(c_dict["tenant_id"]),
                    room_id=UUID(c_dict["room_id"]),
                    message_id=UUID(c_dict["message_id"]),
                    extraction_key=c_dict["id"],
                    description=c_dict["description"],
                    owner_name=c_dict["owner_name"],
                    owner_switch_user_id=owner_id,
                    due_at=due_dt,
                    approval_status="approved",
                    approval_by_switch_user_id="admin",
                    approval_at=created_dt,
                    task_status="not_started",
                    version=c_version,
                    created_at=created_dt,
                    updated_at=created_dt,
                )

                is_meeting = bool(re.search(r'\bmeeting\b', c_dict["description"], re.I))
                is_slack_meeting = is_meeting and (c_dict.get("platform") == "slack" or bool(re.search(r'\bslack\b', c_dict["description"], re.I)))
                switch_room = c_dict["switch_room_id"]
                work_platform = c_dict.get("platform") or "discord"
                if is_slack_meeting and work_platform == "discord":
                    cursor.execute("SELECT switch_room_id FROM rooms WHERE tenant_id = ? AND platform = 'slack' LIMIT 1", (c_dict["tenant_id"],))
                    slack_r = cursor.fetchone()
                    if slack_r and slack_r[0] in self.settings.allowed_switch_rooms:
                        switch_room = slack_r[0]
                        work_platform = "slack"

                items.append(
                    ReminderWork(
                        commitment=comm,
                        policy=policy,
                        history=history,
                        room_enabled=bool(c_dict.get("room_enabled", 1)),
                        assignee_is_member=True,
                        switch_room_id=switch_room,
                        platform=work_platform,
                        thread_id=c_dict.get("thread_id"),
                    )
                )

            next_cursor = items[-1].commitment.id.hex if len(items) == limit else None
            return items, next_cursor

    async def persist(self, intention: ReminderIntention) -> bool:
        if str(intention.tenant_id) != str(self.settings.switch_tenant_id):
            return False

        with get_sqlite_connection(self.settings) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id FROM reminder_deliveries WHERE occurrence_key = ?",
                    (str(intention.occurrence_key),),
                )
                if cursor.fetchone():
                    return False

                deliv_id = str(uuid4())
                cursor.execute(
                    """
                    INSERT INTO reminder_deliveries (
                        id, tenant_id, room_id, commitment_id, scheduled_for, status,
                        occurrence_key, recipient_id, commitment_version, policy_version,
                        reminder_type, ordinal, occurrence_base, next_attempt_at, attempts
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        deliv_id,
                        str(intention.tenant_id),
                        str(intention.room_id),
                        str(intention.commitment_id),
                        intention.scheduled_for.isoformat(),
                        str(intention.occurrence_key),
                        intention.recipient_id,
                        intention.commitment_version,
                        intention.policy_version,
                        intention.kind,
                        intention.ordinal,
                        intention.occurrence_base.isoformat(),
                        intention.scheduled_for.isoformat(),
                    ),
                )
                return True

    async def claim(self, limit: int) -> list[ReminderDelivery]:
        if limit < 1 or limit > 10:
            raise CommitmentError("invalid_action_payload", 422)

        tenant_str, allowed_rooms = self.scope()
        now_dt = self.clock()
        now_iso = now_dt.isoformat()
        lease_iso = (now_dt + timedelta(minutes=2)).isoformat()

        with get_sqlite_connection(self.settings) as conn:
            with conn:
                cursor = conn.cursor()
                # Recover expired claims
                cursor.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = CASE WHEN dispatch_started_at IS NOT NULL THEN 'ambiguous'
                                      WHEN attempts >= 3 THEN 'failed'
                                      ELSE 'pending' END,
                        error_code = CASE WHEN dispatch_started_at IS NOT NULL THEN 'send_outcome_unknown'
                                          WHEN attempts >= 3 THEN 'attempts_exhausted' END,
                        next_attempt_at = CASE WHEN dispatch_started_at IS NULL AND attempts < 3 THEN ? END,
                        claim_token = CASE WHEN dispatch_started_at IS NOT NULL THEN claim_token END,
                        lease_until = NULL
                    WHERE status = 'sending' AND lease_until <= ?
                    """,
                    (now_iso, now_iso),
                )

                placeholders = ",".join("?" for _ in allowed_rooms)
                cursor.execute(
                    f"""
                    SELECT d.* FROM reminder_deliveries d
                    JOIN rooms r ON r.tenant_id = d.tenant_id AND r.id = d.room_id
                    WHERE d.tenant_id = ?
                      AND r.switch_room_id IN ({placeholders})
                      AND d.status IN ('pending', 'failed')
                      AND d.next_attempt_at <= ?
                      AND d.scheduled_for <= ?
                      AND d.attempts < 3
                    ORDER BY d.next_attempt_at ASC, d.id ASC
                    LIMIT ?
                    """,
                    (tenant_str, *allowed_rooms, now_iso, now_iso, limit),
                )
                rows = cursor.fetchall()
                claimed: list[ReminderDelivery] = []

                for row in rows:
                    r_dict = dict(row)
                    token = str(uuid4())
                    cursor.execute(
                        """
                        UPDATE reminder_deliveries
                        SET status = 'sending', claim_token = ?, lease_until = ?
                        WHERE id = ?
                        """,
                        (token, lease_iso, r_dict["id"]),
                    )
                    r_dict["status"] = "sending"
                    r_dict["claim_token"] = token
                    r_dict["lease_until"] = lease_iso

                    # Build ReminderDelivery model
                    intention = ReminderIntention(
                        occurrence_key=UUID(r_dict["occurrence_key"]),
                        tenant_id=UUID(r_dict["tenant_id"]),
                        room_id=UUID(r_dict["room_id"]),
                        commitment_id=UUID(r_dict["commitment_id"]),
                        recipient_id=r_dict["recipient_id"],
                        commitment_version=r_dict["commitment_version"] or 1,
                        policy_version=r_dict["policy_version"] or 1,
                        kind=r_dict["reminder_type"],
                        ordinal=r_dict["ordinal"] or 0,
                        occurrence_base=_parse_iso(r_dict["occurrence_base"]) or now_dt,
                        scheduled_for=_parse_iso(r_dict["scheduled_for"]) or now_dt,
                    )
                    claimed.append(
                        ReminderDelivery(
                            id=UUID(r_dict["id"]),
                            intention=intention,
                            status="sending",
                            attempts=r_dict["attempts"] or 0,
                            claim_token=UUID(token),
                            lease_until=_parse_iso(lease_iso),
                            next_attempt_at=_parse_iso(r_dict["next_attempt_at"]),
                        )
                    )
                return claimed

    async def context(self, delivery: ReminderDelivery) -> DispatchContext | None:
        with get_sqlite_connection(self.settings) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT d.*, c.message_id, c.description,
                       COALESCE(c.owner_name, m.author_name) AS owner_name,
                       c.due_at, c.approval_status, c.task_status, c.created_at,
                       r.switch_room_id, 1 AS room_enabled,
                       m.switch_author_id, NULL AS thread_id,
                       COALESCE(r.platform, 'discord') AS platform
                FROM reminder_deliveries d
                JOIN commitments c ON c.id = d.commitment_id
                JOIN rooms r ON r.tenant_id = d.tenant_id AND r.id = d.room_id
                JOIN messages m ON m.id = c.message_id
                WHERE d.id = ? AND d.claim_token = ? AND d.status = 'sending'
                """,
                (str(delivery.id), str(delivery.claim_token)),
            )
            row = cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            policy_dict = self._ensure_policy(conn, data["tenant_id"], data["room_id"])
            due_dt = _parse_iso(data["due_at"])
            created_dt = _parse_iso(data["created_at"]) or _utc_now()
            now_dt = _utc_now()

            # Re-read delivery row
            cursor.execute("SELECT * FROM reminder_deliveries WHERE id = ?", (data["id"],))
            deliv_raw = dict(cursor.fetchone())

            cursor.execute(
                "SELECT status, reminder_type, commitment_version, policy_version, sent_at FROM reminder_deliveries WHERE commitment_id = ?",
                (data["commitment_id"],),
            )
            deliv_rows = cursor.fetchall()
            upcoming_sent = any(d["status"] == "sent" and d["reminder_type"] == "upcoming" for d in deliv_rows)
            overdue_sent = sum(1 for d in deliv_rows if d["status"] == "sent" and d["reminder_type"] == "overdue")
            sent_dates = [_parse_iso(d["sent_at"]) for d in deliv_rows if d["status"] == "sent" and d["sent_at"]]
            last_sent_at = max(sent_dates) if sent_dates else None

            policy = ReminderPolicy(
                version=policy_dict.get("reminder_policy_version", 1),
                enabled=bool(policy_dict.get("reminders_enabled", 0)),
                timezone=policy_dict.get("timezone") or self.settings.commitment_timezone or "UTC",
                lead_minutes=policy_dict.get("reminder_lead_minutes", self.settings.reminder_lead_minutes),
                cadence_minutes=policy_dict.get("reminder_cadence_minutes", 1440),
                max_followups=policy_dict.get("reminder_max_followups", 3),
                minimum_gap_minutes=policy_dict.get("reminder_gap_minutes", 30),
                room_gap_seconds=policy_dict.get("reminder_room_gap_seconds", 60),
                quiet_start=policy_dict.get("reminder_quiet_start", 22),
                quiet_end=policy_dict.get("reminder_quiet_end", 8),
                delivery_mode=policy_dict.get("reminder_delivery_mode", "room"),
            )

            history = ReminderHistory(
                upcoming_sent=upcoming_sent,
                overdue_sent=overdue_sent,
                last_sent_at=last_sent_at,
                last_room_sent_at=last_sent_at,
            )

            comm = CommitmentState(
                id=UUID(data["commitment_id"]),
                tenant_id=UUID(data["tenant_id"]),
                room_id=UUID(data["room_id"]),
                message_id=UUID(data["message_id"]),
                extraction_key=data["commitment_id"],
                description=data["description"],
                owner_name=data["owner_name"],
                owner_switch_user_id=data.get("switch_author_id") or "unassigned",
                due_at=due_dt,
                approval_status="approved",
                approval_by_switch_user_id="admin",
                approval_at=created_dt,
                task_status="not_started",
                version=data.get("commitment_version") or 1,
                created_at=created_dt,
                updated_at=created_dt,
            )

            is_meeting = bool(re.search(r'\bmeeting\b', data["description"], re.I))
            is_slack_meeting = is_meeting and (data.get("platform") == "slack" or bool(re.search(r'\bslack\b', data["description"], re.I)))
            switch_room = data["switch_room_id"]
            work_platform = data.get("platform") or "discord"
            if is_slack_meeting and work_platform == "discord":
                cursor.execute("SELECT switch_room_id FROM rooms WHERE tenant_id = ? AND platform = 'slack' LIMIT 1", (data["tenant_id"],))
                slack_r = cursor.fetchone()
                if slack_r and slack_r[0] in self.settings.allowed_switch_rooms:
                    switch_room = slack_r[0]
                    work_platform = "slack"

            work = ReminderWork(
                commitment=comm,
                policy=policy,
                history=history,
                room_enabled=bool(data.get("room_enabled", 1)),
                assignee_is_member=True,
                switch_room_id=switch_room,
                platform=work_platform,
                thread_id=data.get("thread_id"),
            )

            parsed_delivery = ReminderDelivery(
                id=UUID(deliv_raw["id"]),
                intention=delivery.intention,
                status=deliv_raw["status"],
                attempts=deliv_raw["attempts"] or 0,
                claim_token=UUID(deliv_raw["claim_token"]) if deliv_raw.get("claim_token") else None,
                lease_until=_parse_iso(deliv_raw.get("lease_until")),
                dispatch_started_at=_parse_iso(deliv_raw.get("dispatch_started_at")),
                sent_at=_parse_iso(deliv_raw.get("sent_at")),
                next_attempt_at=_parse_iso(deliv_raw.get("next_attempt_at")),
            )
            return DispatchContext(delivery=parsed_delivery, work=work)

    async def begin(self, delivery: ReminderDelivery, body: str) -> bool:
        now_iso = self.clock().isoformat()
        with get_sqlite_connection(self.settings) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE reminder_deliveries
                    SET dispatch_started_at = ?, notification_body = ?, attempts = attempts + 1
                    WHERE id = ? AND claim_token = ? AND status = 'sending'
                    """,
                    (now_iso, body, str(delivery.id), str(delivery.claim_token)),
                )
                return cursor.rowcount > 0

    async def defer(self, delivery: ReminderDelivery, until: datetime | None) -> bool:
        with get_sqlite_connection(self.settings) as conn:
            with conn:
                cursor = conn.cursor()
                new_status = "pending" if until else "superseded"
                cursor.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = ?, next_attempt_at = ?, claim_token = NULL, lease_until = NULL
                    WHERE id = ? AND claim_token = ? AND status = 'sending' AND dispatch_started_at IS NULL
                    """,
                    (new_status, until.isoformat() if until else None, str(delivery.id), str(delivery.claim_token)),
                )
                return cursor.rowcount > 0

    async def finish(self, delivery: ReminderDelivery, outcome: str, message_id: str | None = None, error: str | None = None) -> None:
        now_iso = self.clock().isoformat()
        with get_sqlite_connection(self.settings) as conn:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = ?, sent_at = CASE WHEN ? = 'sent' THEN ? END,
                        switch_reply_message_id = ?, error_code = ?,
                        claim_token = NULL, lease_until = NULL
                    WHERE id = ? AND claim_token = ?
                    """,
                    (outcome, outcome, now_iso, message_id, error, str(delivery.id), str(delivery.claim_token)),
                )
                if cursor.rowcount == 0:
                    raise CommitmentError("reminder_acknowledgement_not_saved", 503)
