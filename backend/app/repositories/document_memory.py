"""In-memory contract implementation for independent domain tests; never the API default."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import time
from uuid import uuid4

from fastapi import HTTPException


class MemoryDocumentRepository:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.documents, self.versions, self.chunks, self.imports = {}, {}, {}, {}
        self.lock = asyncio.Lock()

    def _visible(self, scope, doc):
        return doc["tenant_id"] == scope.tenant_id and doc["room_id"] in scope.room_ids

    def _document(self, scope, doc_id):
        doc = self.documents.get(str(doc_id))
        if not doc or not self._visible(scope, doc):
            raise HTTPException(404, "document_not_found")
        return doc

    async def call(self, scope, operation, data):
        async with self.lock:
            return deepcopy(self._call(scope, operation, data))

    def _call(self, scope, operation, data):
        now = self.clock()
        if operation == 'catalog':
            rows = [dict(id=d['id'], title=d['title'], published_version_id=d['published_version_id'],
                         filename=self.versions[d['published_version_id']]['filename'])
                    for d in self.documents.values() if self._visible(scope, d) and d['active']
                    and d['published_version_id'] and self.versions[d['published_version_id']]['status'] == 'completed'
                    and (not data.get('filename') or self.versions[d['published_version_id']]['filename'] == data['filename'])
                    and (not data.get('title') or d['title'] == data['title'])]
            return sorted(rows, key=lambda d: d['id'])[data['offset']:data['offset']+data['limit']]
        if operation == "import_status":
            prior = self.imports.get((scope.tenant_id,data["import_id"]))
            if not prior:
                raise HTTPException(404,"document_not_found")
            doc = self._document(scope,prior["document_id"])
            return self._result(data["import_id"],doc,self.versions[prior["version_id"]],True)
        if operation == "import":
            identity = (scope.tenant_id, data["import_id"])
            prior = self.imports.get(identity)
            if prior:
                if prior["request_hash"] != data["request_hash"]:
                    raise HTTPException(409, "document_version_conflict")
                doc = self._document(scope, prior["document_id"])
                return self._result(data["import_id"], doc, self.versions[prior["version_id"]], True)
            if data["room_id"] not in scope.room_ids:
                raise HTTPException(403, "document_scope_denied")
            doc = next((d for d in self.documents.values() if self._visible(scope, d)
                        and d["room_id"] == data["room_id"] and d["source_key"] == data["source_key"]), None)
            if (doc["latest_version"] if doc else 0) != data["expected_version"] or (doc and (
                not doc["active"] or doc["source_type"] != data["source_type"])):
                raise HTTPException(409, "document_version_conflict")
            if not doc:
                doc = dict(id=str(uuid4()), tenant_id=scope.tenant_id, room_id=data["room_id"],
                           source_key=data["source_key"], source_type=data["source_type"], title=data["title"],
                           created_by=scope.actor, latest_version=0, published_version_id=None, active=True,
                           created_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
                self.documents[doc["id"]] = doc
            latest = next((v for v in self.versions.values() if v["document_id"] == doc["id"]
                           and v["version_number"] == doc["latest_version"]), None)
            duplicate = bool(latest and all(latest[k] == data[k] for k in ("content_hash","recipe","model_recipe","title")))
            if duplicate:
                version = latest
            else:
                for old in self.versions.values():
                    if old["document_id"] == doc["id"] and old["status"] != "completed":
                        old.update(status="failed",error="version_superseded")
                        for chunk in self.chunks.values():
                            if chunk["version_id"] == old["id"] and chunk["status"] != "completed":
                                chunk.update(status="failed",error="version_superseded",claim_token=None)
                doc["latest_version"] += 1
                version = dict(id=str(uuid4()), document_id=doc["id"], version_number=doc["latest_version"],
                               status="processing", error=None, created_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                               **{k:data[k] for k in ("source_text","content_hash","recipe","model_recipe","filename","title")})
                self.versions[version["id"]] = version
                for piece in data["chunks"]:
                    chunk = dict(piece, id=str(uuid4()), document_id=doc["id"], version_id=version["id"],
                                 status="pending", attempts=0, error=None, retry_at=now, lease_until=0,
                                 claim_token=None, embedding=None, embedding_content_hash=None,
                                 model_recipe=data["model_recipe"])
                    self.chunks[chunk["id"]] = chunk
            self.imports[identity] = dict(request_hash=data["request_hash"], document_id=doc["id"], version_id=version["id"])
            return self._result(data["import_id"], doc, version, duplicate)
        if operation == "documents":
            rows = sorted((d for d in self.documents.values() if self._visible(scope,d)),key=lambda d:d["id"])
            return rows[data["offset"]:data["offset"]+data["limit"]]
        if operation == "claim":
            jobs = []
            for chunk in self.chunks.values():
                doc = self.documents[chunk["document_id"]]
                version = self.versions[chunk["version_id"]]
                if not self._visible(scope,doc) or not doc["active"] or version["version_number"] != doc["latest_version"]:
                    continue
                if chunk["status"] == "processing" and chunk["lease_until"] <= now and chunk["attempts"] >= 3:
                    chunk.update(status="failed",error="lease_exhausted",claim_token=None)
                    version.update(status="failed",error="lease_exhausted")
                eligible = ((chunk["status"] in ("pending","failed") and chunk["retry_at"] <= now)
                            or (chunk["status"] == "processing" and chunk["lease_until"] <= now))
                if eligible and chunk["attempts"] < 3 and len(jobs) < 4:
                    chunk.update(status="processing", attempts=chunk["attempts"]+1, lease_until=now+300,
                                 claim_token=str(uuid4()), error=None)
                    version.update(status="processing",error=None)
                    jobs.append(chunk)
            return jobs
        if operation == "finish":
            chunk = self.chunks.get(data["id"])
            if not chunk:
                return False
            doc = self._document(scope, chunk["document_id"])
            version = self.versions[chunk["version_id"]]
            if (not doc["active"] or version["version_number"] != doc["latest_version"]
                or chunk["status"] != "processing" or chunk["claim_token"] != data["claim_token"]
                or chunk["content_hash"] != data["content_hash"] or chunk["model_recipe"] != data["model_recipe"]
                or chunk["lease_until"] <= now):
                return False
            error = data.get("error")
            chunk.update(status="failed" if error else "completed", error=error, retry_at=now+30,
                         claim_token=None, embedding=data.get("vector"),
                         embedding_content_hash=None if error else chunk["content_hash"])
            if error and chunk["attempts"] >= 3:
                version.update(status="failed",error=error)
            if all(c["status"] == "completed" for c in self.chunks.values() if c["version_id"] == version["id"]):
                version.update(status="completed",error=None)
                doc.update(published_version_id=version["id"], title=version["title"])
            return True
        if operation == "search":
            rows = []
            for chunk in self.chunks.values():
                doc = self.documents[chunk["document_id"]]
                if (not self._visible(scope,doc) or not doc["active"] or doc["published_version_id"] != chunk["version_id"]
                    or (data.get("document_id") and data["document_id"] != doc["id"])
                    or chunk["status"] != "completed" or chunk["embedding_content_hash"] != chunk["content_hash"]
                    or chunk["model_recipe"] != data["model_recipe"]):
                    continue
                version = self.versions[chunk["version_id"]]
                score = sum(a*b for a,b in zip(data["vector"],chunk["embedding"]))
                rows.append(dict(document_id=doc["id"],version_id=version["id"],version_number=version["version_number"],
                                 chunk_id=chunk["id"],title=version["title"],similarity=score,
                                 **{k:chunk[k] for k in ("text","section","start_line","end_line")}))
            return sorted(rows,key=lambda r:(-r["similarity"],r["chunk_id"]))[:data["limit"]]
        doc = self._document(scope, data["document_id"])
        if operation == "document":
            return doc
        if operation == "archive":
            if doc["latest_version"] != data["expected_version"]:
                raise HTTPException(409,"document_version_conflict")
            doc["active"] = False
            return doc
        versions = sorted((v for v in self.versions.values() if v["document_id"] == doc["id"]),key=lambda v:v["version_number"])
        if operation == "versions":
            return versions[data["offset"]:data["offset"]+data["limit"]]
        version = next((v for v in versions if v["id"] == data.get("version_id")),None)
        if not version:
            raise HTTPException(404,"document_not_found")
        if operation == "version":
            return version
        if operation == "chunk":
            chunk = self.chunks.get(data["chunk_id"])
            if not chunk or chunk["version_id"] != version["id"]:
                raise HTTPException(404,"document_not_found")
            return chunk
        if operation == "chunks":
            rows = sorted((c for c in self.chunks.values() if c["version_id"] == version["id"]),key=lambda c:c["position"])
            return rows[data["offset"]:data["offset"]+data["limit"]]
        raise HTTPException(422,"document_invalid_operation")

    @staticmethod
    def _result(import_id, doc, version, duplicate):
        return dict(import_id=import_id, document_id=doc["id"], version_id=version["id"],
                    version_number=version["version_number"],status=version["status"],duplicate=duplicate)
