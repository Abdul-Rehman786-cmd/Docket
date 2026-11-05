"""Authenticated adapter metadata, never model output, establishes participation."""
import hashlib
import json
import re


def participant_id(tenant_id, room_id, sender, platform='switch'):
    # Switch puppet IDs are scoped to the bridge. Do not merge external identities.
    raw=json.dumps([str(tenant_id),str(room_id),platform,sender],ensure_ascii=False)
    return 'openchat:'+hashlib.sha256(raw.encode()).hexdigest()


def explicit_agent_mention(body, metadata, settings):
    if not metadata or not metadata.get('adapter_verified') or metadata.get('sender_is_bot') is not False:
        return False
    if metadata.get('registered_agent_id')!=settings.listener_agent_id:
        return False
    name=metadata.get('registered_agent_name')
    # The listener fetches this name from its authenticated registration. `addressed`
    # alone includes follow-ups and is deliberately insufficient.
    targets=metadata.get('mentioned_agent_ids')
    if targets is not None:
        return settings.listener_agent_id in targets
    if not isinstance(name,str) or not re.fullmatch(r'[\w.-]{1,128}',name):
        return False
    if any(c in body for c in ('`','"','\n')) or body.lstrip().startswith('>'):
        return False
    return bool(metadata.get('addressed') and re.search(r'(?<![\w@])@'+re.escape(name)+r'(?![\w.-])',body,re.I))


def without_agent_mention(body, name):
    # Command evidence must start with the actual registered agent mention.
    return re.sub(r'^\s*@'+re.escape(name)+r'(?![\w.-])[, :]\s*','',body,flags=re.I).strip()
