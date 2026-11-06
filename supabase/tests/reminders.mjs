// Disposable local PostgreSQL only; never loads deployment credentials.
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { pgcrypto } from '@electric-sql/pglite/contrib/pgcrypto';
import { readFile } from 'node:fs/promises';
const db=new PGlite({extensions:{vector,pgcrypto}});
let phase='roles';
try {
 await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
 for(const file of ['../migrations/20261104000100_initial_schema.sql',
 'commitment_lifecycle.sql','commitment_chat.sql','reminders.sql','../reminder_deployment_review.sql']) {
  phase=file;await db.exec(await readFile(new URL(file,import.meta.url),'utf8'));
 }
 console.log('Disposable PostgreSQL reminder outbox assertions passed');
}catch(error){console.error(phase+': '+error.message);process.exitCode=1;}
finally{await db.close();}
