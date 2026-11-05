"""Switch attachment consent and ingestion. Consent is deterministic, never an LLM decision."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import PurePosixPath
import re
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

from app.core.config import get_settings
from app.models.document_attachments import AttachmentReceipt, AttachmentStatus
from app.models.documents import DocumentScope, ImportMetadata
from app.repositories.document_attachment_jobs import AttachmentJobs
from app.repositories.document_repository import SupabaseDocumentRepository
from app.services.document_authorization import resolve_document_scope
from app.services.document_service import DocumentService
from app.services.message_service import message_reference, validate_message_scope
from app.services.switch_document_media import AttachmentFailure, SwitchDocumentTransport, valid_media_reference

logger = logging.getLogger("team_memory.documents")

COMMAND = re.compile(r"(save|skip) ([0-9a-f]{12})",re.IGNORECASE)


def reference(values):
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


def display_name(name):
    # Plain safe display metadata: no mentions, Markdown delimiters or controls.
    return re.sub(r"[^\w .()-]","_",name)[:80]


def attachment_error(attachment,settings):
    name=attachment["filename"]
    if (len(name)>200 or name!=PurePosixPath(name).name or any(c in name for c in ('\\','/',':','\x00'))
        or any(ord(c)<32 or ord(c)==127 for c in name)):
        return "unsafe_filename"
    if PurePosixPath(name).suffix.lower() not in (".md",".txt"):
        return "unsupported_format"
    if attachment.get("size") and attachment["size"]>settings.switch_document_max_bytes:
        return "attachment_too_large"
    if not valid_media_reference(attachment["mxc"]):
        return "unsafe_media_reference"
    return None


class DocumentAttachments:
    def __init__(self,jobs=None,repository=None,scope_resolver=resolve_document_scope,
                 transport_factory=SwitchDocumentTransport,service_factory=DocumentService,settings_provider=None):
        self.jobs=jobs or AttachmentJobs()
        self.repository=repository or SupabaseDocumentRepository()
        self.scope_resolver=scope_resolver
        self.transport_factory=transport_factory
        self.service_factory=service_factory
        self.settings_provider=settings_provider or get_settings
        self.task=None

    @staticmethod
    def configured(settings):
        return (settings.switch_document_attachments_enabled and settings.document_ingestion_enabled
                and bool(settings.document_discord_bridges)
                and bool(settings.switch_document_bot_sender_id)
                and bool(settings.switch_bridge_url and settings.listener_agent_id and settings.listener_api_key.get_secret_value())
                and settings.local_embeddings_enabled)

    @staticmethod
    def eligible_sender(sender,settings):
        # Trust the authenticated adapter sender ID without classifying human versus agent.
        # Known bot IDs remain excluded; uploader-only consent uses exact sender equality.
        return (isinstance(sender,str)
                and re.fullmatch(r"@[^\s:]+:[^\s:]+",sender) is not None
                and sender not in settings.switch_ai_senders)

    @staticmethod
    def log_configuration(settings):
        logger.info("document stage=api_start enabled=%s ready=%s document_worker=%s embeddings=%s bridge_configured=%s bot_configured=%s adapter_configured=%s",
            settings.switch_document_attachments_enabled,DocumentAttachments.configured(settings),
            settings.document_ingestion_enabled,settings.local_embeddings_enabled,bool(settings.document_discord_bridges),
            bool(settings.switch_document_bot_sender_id),bool(settings.switch_document_adapter_token.get_secret_value()))

    def receipt(self,message,row=None,duplicate=False,state="ignored"):
        logger.info("document stage=receipt ref=%s request=%s state=%s duplicate=%s",
            message_reference(message),row["ref"] if row else "none",row["state"] if row else state,duplicate)
        return AttachmentReceipt(message_ref=message_reference(message),
            received_at=datetime.fromtimestamp(self.jobs.clock(),timezone.utc).isoformat(),duplicate=duplicate,
            request_ref=row["ref"] if row else None,processing_state=row["state"] if row else state)

    def replies(self,ref,items,error=None):
        names=", ".join(display_name(i["filename"]) for i in items[:8]) or "this attachment group"
        if len(items)>8:
            names+=f" (and {len(items)-8} more)"
        failures={"unsupported_format":"Only UTF-8 Markdown (.md) and plain-text (.txt) files are supported.",
                  "attachment_too_large":"The file exceeds the configured size limit.",
                  "too_many_files":"Too many files were attached.",
                  "sender_identity_unverified":"The uploader identity is missing, invalid or a known bot. No file was imported.",
                  "media_unavailable":"The attachment is unavailable or expired. Please upload it again.",
                  "media_access_denied":"The Switch client cannot download this file. Ask an administrator to verify its room and media permissions.",
                  "unsafe_media_reference":"The attachment reference is unsupported. Please upload the file through Discord again.",
                  "unsafe_filename":"The filename is unsafe. Rename the file and upload again.",
                  "invalid_utf8":"The file must contain UTF-8 text. Please upload it again.",
                  "invalid_text":"The file is empty or contains binary data. Upload a UTF-8 text file.",
                  "publication_timeout":"Processing has not finished. Check the request status before uploading again."}
        failed=failures.get(error,"The import could not finish. Any files already accepted keep their own processing state; check this request before retrying.")
        return dict(prompt=f"Should I save this document to team memory? Files: {names}. Reply exactly save {ref} or skip {ref}. Only the uploader can confirm.",
            queued=f"Request {ref} queued for import.",processing=f"Request {ref} accepted; document processing is underway.",
            published=f"Saved to searchable team memory: {names}. Request {ref}.",
            declined=f"Request {ref} declined. No files will be imported.",
            expired=f"Request {ref} expired without confirmation. Please upload again if you want it saved.",
            failed=f"Could not finish request {ref} for {names}. {failed}")

    async def accept(self,message,settings):
        logger.info("document stage=api_received ref=%s files=%d",message_reference(message),len(message.attachments))
        try:
            validate_message_scope(message,settings)
        except HTTPException as error:
            logger.warning("document stage=scope_rejected ref=%s http_status=%d",message_reference(message),error.status_code)
            raise
        if not self.configured(settings):
            logger.warning("document stage=setup_rejected reason=configuration_incomplete")
            raise HTTPException(503,"document_attachment_setup_required")
        if message.sender in settings.switch_ai_senders:
            return self.receipt(message)
        if message.bridge_id not in settings.document_discord_bridges:
            logging.getLogger("team_memory").warning("Document attachment event reason=source_not_authorized")
            return self.receipt(message,state="not_authorized")
        if not self.eligible_sender(message.sender,settings):
            return self.receipt(message,state="not_authorized")
        try:
            await self.scope_resolver(settings,message.room_id)
        except HTTPException as error:
            logger.warning("document stage=scope_resolution http_status=%d",error.status_code)
            raise
        command=COMMAND.fullmatch(message.body.strip())
        if not message.attachments:
            if command:
                return await self.decide(message,settings,command.group(1).lower(),command.group(2).lower())
            return self.receipt(message)

        group=message.attachment_group
        event_key=reference([str(message.tenant_id),message.room_id,message.sender,
                             group.id if group else message.message_id,message.thread_id,message.bridge_id])
        ref=event_key[:12]
        part=str(group.index if group else 0)
        total=group.total if group else 1
        metadata={key:getattr(message,key) for key in ('message_id','thread_id','workspace_id','channel_id',
            'external_message_id','external_author_id','platform','bridge_id','sender_kind','timestamp_ms')}
        metadata['attachment_group']=group.model_dump() if group else None
        attachments=[a.model_dump() for a in message.attachments]
        for attachment in attachments:
            # Never persist an arbitrary/signed URL masquerading as a media pointer.
            if not valid_media_reference(attachment["mxc"]):
                attachment["mxc"]=""
            if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+",attachment["mimetype"]):
                attachment["mimetype"]=""
        incoming=dict(metadata=metadata,attachments=attachments)
        data=dict(parts={},total=total,ready=False,items=[],phase="importing",error=None,
                  reply_thread=message.thread_id or message.message_id,replies=self.replies(ref,[]))
        row,_=await self.jobs.insert(dict(ref=ref,event_key=event_key,tenant=str(message.tenant_id),
            room=message.room_id,sender=message.sender,state="confirmation_requested",data=data,
            expires=self.jobs.clock()+settings.switch_document_confirmation_seconds),settings.switch_document_max_pending,settings.switch_document_max_records)
        if row is None:
            raise HTTPException(429,"document_attachment_queue_full",headers={"Retry-After":"5"})
        for _ in range(8):
            if row["data"]["total"]!=total:
                raise HTTPException(409,"document_attachment_event_conflict")
            if part in row["data"]["parts"] and row["data"]["parts"][part]!=incoming:
                raise HTTPException(409,"document_attachment_event_conflict")
            if part in row["data"]["parts"]:
                return self.receipt(message,row,True)
            if row["state"]!="confirmation_requested" or row["data"]["ready"]:
                return self.receipt(message,row,True)
            data=deepcopy(row["data"])
            if data["total"]!=total or (part in data["parts"] and data["parts"][part]!=incoming):
                raise HTTPException(409,"document_attachment_event_conflict")
            data["parts"][part]=incoming
            all_items=[]
            for index in sorted(data["parts"],key=int):
                entry=data["parts"][index]
                for attachment in entry["attachments"]:
                    item=dict(attachment,source=entry["metadata"],result=None)
                    item["import_id"]=str(uuid5(NAMESPACE_URL,event_key+":"+item["mxc"]))
                    item["source_key"]="switch-discord:"+ref+":"+reference([entry["metadata"]["message_id"],item["mxc"]])[:24]
                    # A repeated MXC in a grouped event is one attachment identity.
                    if not any(i["import_id"]==item["import_id"] for i in all_items):
                        all_items.append(item)
            data["items"]=all_items
            error=None
            if len(all_items)>settings.switch_document_max_files or total>settings.switch_document_max_files:
                error="too_many_files"
            else:
                error=next((e for a in all_items if (e:=attachment_error(a,settings))),None)
            data["ready"]=bool(error) or len(data["parts"])==total
            data["error"]=error
            if error:
                logger.warning("document stage=metadata_rejected request=%s reason=%s",ref,error)
            data["replies"]=self.replies(ref,all_items,error)
            updated=await self.jobs.change(row,state="failed" if error else None,data=data,
                reply=("failed" if error else "prompt") if data["ready"] else None)
            if updated:
                return self.receipt(message,updated,False)
            row=await self.jobs.get(ref)
        raise HTTPException(503,"document_attachment_retry_delivery")

    async def decide(self,message,settings,decision,ref):
        for _ in range(8):
            row=await self.jobs.get(ref)
            if (not row or row["tenant"]!=str(message.tenant_id) or row["room"]!=message.room_id
                or row["sender"]!=message.sender
                or not self.eligible_sender(message.sender,settings) or not row["data"]["ready"]):
                return self.receipt(message,state="not_authorized")
            original=next(iter(row["data"]["parts"].values()))["metadata"]
            if message.bridge_id!=original["bridge_id"]:
                return self.receipt(message,state="not_authorized")
            if original["thread_id"] and message.thread_id!=original["thread_id"]:
                return self.receipt(message,state="not_authorized")
            if row["state"]!="confirmation_requested":
                return self.receipt(message,row,True)
            expired=row["expires"]<=self.jobs.clock()
            state="expired" if expired else ("queued" if decision=="save" else "declined")
            data=deepcopy(row["data"])
            data["confirmation_message_id"]=message.message_id
            data["confirmed_at"]=self.jobs.clock() if state=="queued" else None
            updated=await self.jobs.change(row,state=state,data=data,reply=state,release=True)
            if updated:
                return self.receipt(message,updated)
        raise HTTPException(503,"document_attachment_retry_delivery")

    async def fail(self,row,error):
        logger.warning("document stage=processing_failed request=%s reason=%s",row["ref"],error)
        data=deepcopy(row["data"])
        data["error"]=error
        data["replies"]=self.replies(row["ref"],data["items"],error)
        return await self.jobs.change(row,state="failed",data=data,reply="failed",release=True)

    async def work_once(self,settings):
        for expired in await self.jobs.due(settings.switch_tenant_id,settings.allowed_switch_rooms):
            await self.jobs.change(expired,state="expired",reply="expired",release=True)
        row=await self.jobs.claim(settings.switch_tenant_id,settings.allowed_switch_rooms)
        if row is None:
            return False
        logger.info("document stage=processing request=%s phase=%s attempt=%d",row["ref"],row["data"]["phase"],row["attempts"])
        try:
            if not self.eligible_sender(row["sender"],settings):
                raise AttachmentFailure("sender_identity_unverified")
            if any(part["metadata"]["bridge_id"] not in settings.document_discord_bridges for part in row["data"]["parts"].values()):
                raise AttachmentFailure("source_not_authorized")
            scope=await self.scope_resolver(settings,row["room"])
            scope=DocumentScope(scope.tenant_id,scope.room_ids,actor="switch:"+row["sender"])
            if row["data"]["phase"]=="importing":
                if row["attempts"]>3:
                    raise AttachmentFailure("retry_exhausted")
                data=deepcopy(row["data"])
                for item in data["items"]:
                    if item["result"]:
                        continue
                    if row["lease_until"]<=self.jobs.clock():
                        return False
                    try:
                        recovered=await self.repository.call(scope,"import_status",dict(import_id=item["import_id"]))
                    except HTTPException as error:
                        if error.status_code!=404:
                            raise
                        recovered=None
                    if recovered:
                        item["result"]=recovered
                    else:
                        raw=await self.transport_factory(settings).download(row["room"],item)
                        try:
                            if len(raw)>settings.switch_document_max_bytes:
                                raise AttachmentFailure("attachment_too_large")
                            if raw.removeprefix(b'\xef\xbb\xbf').lstrip().startswith((b'%PDF-',b'PK\x03\x04',b'\xd0\xcf\x11\xe0')):
                                raise AttachmentFailure("unsupported_format")
                            try:
                                text=raw.decode("utf-8")
                            except UnicodeError:
                                raise AttachmentFailure("invalid_utf8") from None
                            if len(text)>settings.switch_document_max_text_chars:
                                raise AttachmentFailure("attachment_too_large")
                            if not text.strip() or '\x00' in text:
                                raise AttachmentFailure("invalid_text")
                            del text
                            result=await self.service_factory(self.repository).import_bytes(scope,ImportMetadata(
                                import_id=item["import_id"],source_key=item["source_key"],filename=item["filename"],
                                title=item["filename"],expected_version=0),raw)
                            item["result"]=result.model_dump(mode="json")
                        finally:
                            del raw
                    updated=await self.jobs.change(row,data=data)
                    if not updated:
                        return False
                    row=updated
                data["phase"]="publication"
                await self.jobs.change(row,data=data,reply="processing",release=True,retry_at=self.jobs.clock()+2)
                return True
            if self.jobs.clock()-row["data"]["confirmed_at"]>settings.switch_document_processing_seconds:
                raise AttachmentFailure("publication_timeout")
            for item in row["data"]["items"]:
                result=item["result"]
                version=await self.repository.call(scope,"version",dict(document_id=result["document_id"],version_id=result["version_id"]))
                result["status"]=version["status"]
                if version["status"]=="failed":
                    raise AttachmentFailure("embedding_failed")
                if version["status"]!="completed":
                    await self.jobs.change(row,data=row["data"],release=True,retry_at=self.jobs.clock()+2)
                    return True
                doc=await self.repository.call(scope,"document",dict(document_id=result["document_id"]))
                if not doc["active"] or doc["published_version_id"]!=result["version_id"]:
                    raise AttachmentFailure("document_not_current")
            await self.jobs.change(row,state="published",data=row["data"],reply="published",release=True)
            logger.info("document stage=published request=%s",row["ref"])
            return True
        except AttachmentFailure as error:
            if error.retryable and row["attempts"]<3:
                await self.jobs.change(row,state="queued",release=True,retry_at=self.jobs.clock()+30)
            else:
                await self.fail(row,error.code)
        except HTTPException as error:
            reasons={"document_tokenizer_unavailable","document_database_unavailable","document_scope_denied",
                     "document_import_busy","document_invalid_import","document_version_conflict"}
            reason=error.detail if isinstance(error.detail,str) and error.detail in reasons else "ingestion_unavailable"
            logger.warning("document stage=import_error request=%s phase=%s attempt=%d http_status=%d reason=%s retry=%s",
                row["ref"],row["data"]["phase"],row["attempts"],error.status_code,reason,
                error.status_code>=500 and row["attempts"]<3)
            if error.status_code>=500 and row["attempts"]<3:
                await self.jobs.change(row,state="queued" if row["data"]["phase"]=="importing" else "processing",
                                       release=True,retry_at=self.jobs.clock()+30)
            else:
                await self.fail(row,"ingestion_unavailable")
        return True

    async def send_once(self,settings):
        row=await self.jobs.claim_reply(settings.switch_tenant_id,settings.allowed_switch_rooms)
        if row is None:
            return False
        success=False
        logger.info("document stage=reply_attempt request=%s kind=%s attempt=%d",row["ref"],row["data"]["kind"],row["attempts"])
        try:
            request=await self.jobs.get(row["ref"])
            # A delayed prompt must not invite consent for an already terminal request.
            stale=row["data"]["kind"]=="prompt" and (request["state"]!="confirmation_requested" or request["expires"]<=self.jobs.clock())
            stale=stale or (row["data"]["kind"] in ("queued","processing") and request["state"] in ("declined","expired","failed","published"))
            if not stale:
                if any(part["metadata"]["bridge_id"] not in settings.document_discord_bridges for part in request["data"]["parts"].values()):
                    raise AttachmentFailure("source_not_authorized")
                await self.scope_resolver(settings,row["room"])
                await self.transport_factory(settings).post(row["room"],row["data"]["body"],row["data"]["thread_id"])
            success="superseded" if stale else True
        except Exception as error:
            logger.warning("document stage=reply_failed request=%s kind=%s attempt=%d error_type=%s http_status=%s",
                row["ref"],row["data"]["kind"],row["attempts"],type(error).__name__,
                error.status_code if isinstance(error,HTTPException) else "unavailable")
        await self.jobs.finish_reply(row,success)
        if success:
            logger.info("document stage=reply_finished request=%s outcome=%s",row["ref"],"superseded" if success=="superseded" else "sent")
        return True

    async def status(self,ref,settings):
        row=await self.jobs.get(ref)
        if not row or row["tenant"]!=str(settings.switch_tenant_id) or row["room"] not in settings.allowed_switch_rooms:
            raise HTTPException(404,"document_attachment_not_found")
        await self.scope_resolver(settings,row["room"])
        sources=[]
        for item in row["data"]["items"]:
            source=dict(item["source"],source_key=item["source_key"],import_id=item["import_id"],
                        sender=row["sender"],tenant_id=row["tenant"],room_id=row["room"],platform_url=None,
                        attachment_ref=reference([item["mxc"]])[:24],filename=item["filename"])
            ids=[source.get(k) for k in ("workspace_id","channel_id","external_message_id")]
            if source.get("bridge_id") and all(isinstance(v,str) and re.fullmatch(r"[0-9]{5,25}",v) for v in ids):
                source["platform_url"]="https://discord.com/channels/"+"/".join(ids)
            sources.append(source)
        return AttachmentStatus(request_ref=ref,state=row["state"],error=row["data"]["error"],
            filenames=[i["filename"] for i in row["data"]["items"]],
            documents=[i["result"] for i in row["data"]["items"] if i["result"]],reply_states=await self.jobs.reply_states(ref),
            source_references=sources)

    def start(self):
        self.task=asyncio.create_task(self.run())

    async def run(self):
        # One bounded import loop and one independent sender keep a slow download
        # from blocking consent prompts or status delivery for other requests.
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.processing_loop())
            tasks.create_task(self.reply_loop())

    async def processing_loop(self):
        while True:
            settings=self.settings_provider()
            if self.configured(settings):
                try:
                    await self.work_once(settings)
                except Exception:
                    logging.getLogger("team_memory").warning("Document attachment worker reason=processing_unavailable")
            await asyncio.sleep(2)

    async def reply_loop(self):
        while True:
            settings=self.settings_provider()
            if self.configured(settings):
                try:
                    for _ in range(4):
                        if not await self.send_once(settings):
                            break
                except Exception:
                    logging.getLogger("team_memory").warning("Document attachment outbox reason=delivery_unavailable")
            await asyncio.sleep(2)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
