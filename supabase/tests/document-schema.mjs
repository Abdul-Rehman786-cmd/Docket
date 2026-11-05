// Disposable PostgreSQL + pgvector only. Never reads env or connects remotely.
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { pgcrypto } from '@electric-sql/pglite/contrib/pgcrypto';
import { readFile } from 'node:fs/promises';
const read = async path => readFile(new URL(path, import.meta.url), 'utf8');
const db = new PGlite({extensions:{vector,pgcrypto}});
try {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  await db.exec(await read('../migrations/20261104000100_initial_schema.sql'));
  await db.exec(await read('../migrations/20261105163303_versioned_documents.sql'));
  const result=await db.exec(await read('documents.sql'));
  console.log(JSON.stringify({document_schema_and_contracts_passed:true,result:result.at(-1).rows}));
  // Existing SQL suites must continue to pass with document tables installed.
  for (const file of ['useful_storage.sql','embedding_jobs.sql']) {
    await db.exec(await read(file));
    console.log(JSON.stringify({existing_suite_passed:file}));
  }
} catch(error) {
  console.error(error.message);
  process.exitCode=1;
} finally { await db.close(); }
