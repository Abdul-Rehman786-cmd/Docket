// Entirely local disposable PostgreSQL + pgvector. Never loads env or connects remotely.
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { pgcrypto } from '@electric-sql/pglite/contrib/pgcrypto';
import { readFile } from 'node:fs/promises';

const read = async path => readFile(new URL(path, import.meta.url), 'utf8');
const db = new PGlite({ extensions: { vector, pgcrypto } });
try {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  const baseline = await read('../migrations/20261104000100_initial_schema.sql');
  for (let pass = 1; pass <= 2; pass++) {
    await db.exec(baseline); // fresh creation, then explicit dependency-safe local reset
    for (const file of ['database_audit.sql', 'useful_storage.sql', 'embedding_jobs.sql']) {
      const results = await db.exec(await read(file));
      console.log(JSON.stringify({ pass, file, results: results.at(-1).rows }));
    }
    const { rows } = await db.query('SELECT count(*)::int AS remaining_fixture_tenants FROM public.tenants');
    if (rows[0].remaining_fixture_tenants !== 0) throw new Error('Fixture cleanup failed');
  }
  console.log(JSON.stringify({ fresh_schema_and_reset_passed: true }));
} catch (error) {
  // Only synthetic fixture/schema diagnostics can appear in this offline runner.
  console.error(error.message);
  process.exitCode = 1;
} finally {
  await db.close();
}
