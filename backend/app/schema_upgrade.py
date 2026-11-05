"""Print a reviewable non-destructive hosted delta derived from the sole baseline.

This command does not connect to a database, load credentials or apply SQL.
The destructive baseline is NEVER used as the hosted upgrade.
"""

import re
from app.core.config import ROOT_DIR


def build_upgrade_sql():
    core = (ROOT_DIR / "supabase/migrations/20261104000100_initial_schema.sql").read_text(encoding="utf-8")
    section = core.split("-- EMBEDDING_COLUMNS_BEGIN",1)[1].split("-- EMBEDDING_COLUMNS_END",1)[0]
    definitions = re.findall(r"^    ([a-z_]+ .*?)(?=^    [a-z_]+ |\Z)",section,re.M|re.S)
    statements = ["BEGIN;"]
    for definition in definitions:
        definition = definition.strip().rstrip(",")
        if definition.startswith("content_hash "):
            definition = definition.replace("NOT NULL", "")
        statements.append("ALTER TABLE public.memories ADD COLUMN IF NOT EXISTS " + definition + ";")
    statements.append("""UPDATE public.memories SET
    content_hash=pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(content,'UTF8')),'hex'),
    embedding=NULL, embedding_content_hash=NULL, embedding_status='pending',
    embedding_attempts=0, embedding_next_attempt_at=now(), embedding_lease_until=NULL,
    embedding_claim_token=NULL, embedding_completed_at=NULL, embedding_error_code=NULL
WHERE content_hash IS DISTINCT FROM pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(content,'UTF8')),'hex')
   OR (embedding IS NOT NULL AND embedding_content_hash IS NULL);
ALTER TABLE public.memories ALTER COLUMN content_hash SET NOT NULL;""")
    for name in ("memories_embedding_state_check","memories_embedding_content_check"):
        start = core.index("CONSTRAINT " + name + " CHECK")
        opening = core.index("(",start)
        depth = 1
        end = opening+1
        while depth:
            depth += (core[end]=="(")-(core[end]==")")
            end += 1
        clause = core[start:end]
        statements.append(f"""DO $upgrade$ BEGIN
IF NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid='public.memories'::regclass AND conname='{name}') THEN
ALTER TABLE public.memories ADD {clause};
END IF; END $upgrade$;""")
    for index in ("idx_memories_embedding_queue","idx_memories_embedding_lease"):
        found = re.search(r"CREATE INDEX " + index + r"\b.*?;",core,re.S)
        statements.append(found.group().replace("CREATE INDEX ","CREATE INDEX IF NOT EXISTS ",1))
    for name in ("clear_stale_memory_embedding","search_memories"):
        found = re.search(r"CREATE (?:OR REPLACE )?FUNCTION public\."+name+r"\(.*?\$\$;",core,re.S)
        statements.append(found.group().replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION",1))
    statements.append("DROP TRIGGER IF EXISTS memories_clear_stale_embedding ON public.memories;")
    statements.append(re.search(r"CREATE TRIGGER memories_clear_stale_embedding\b.*?;",core,re.S).group())
    for begin,end in (("ATOMIC_STORAGE_BEGIN","ATOMIC_STORAGE_END"),("EMBEDDING_WORK_BEGIN","EMBEDDING_WORK_END")):
        statements.append(core.split("-- "+begin,1)[1].split("-- "+end,1)[0].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
    statements.append("REVOKE ALL ON FUNCTION public.search_memories(UUID,UUID,extensions.vector,INTEGER) FROM PUBLIC,anon,authenticated;")
    statements.append("GRANT EXECUTE ON FUNCTION public.search_memories(UUID,UUID,extensions.vector,INTEGER) TO service_role;")
    statements.append("COMMIT;")
    sql = "\n\n".join(statements)
    if re.search(r"\b(?:DROP TABLE|TRUNCATE|DELETE FROM|CASCADE)\b",sql,re.I):
        raise ValueError("Refusing a destructive hosted proposal")
    return sql


if __name__ == "__main__":
    print(build_upgrade_sql())
