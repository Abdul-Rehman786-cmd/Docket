// Disposable local PostgreSQL only. Never reads .env or connects to hosted services.
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { pgcrypto } from '@electric-sql/pglite/contrib/pgcrypto';
import { readFile } from 'node:fs/promises';
const db = new PGlite({ extensions: { vector, pgcrypto } });
const read = path => readFile(new URL(path,import.meta.url),'utf8');
try {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  await db.exec(await read('../migrations/20261104000100_initial_schema.sql'));
  // Simulate hosted default grants; the additive migration must remove them.
  await db.exec('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role;');
  await db.exec('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO anon, authenticated;');
  await db.exec(await read('../migrations/20261105000100_commitment_lifecycle.sql'));
  await db.exec(await read('commitment_lifecycle.sql'));
  console.log('Disposable PostgreSQL commitment lifecycle assertions passed');
} catch(error) {
  console.error(error.message);
  process.exitCode=1;
} finally { await db.close(); }
