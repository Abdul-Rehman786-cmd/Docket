// Disposable SQL test bridge, no network/.env. Never used by the application/AI.
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { pgcrypto } from '@electric-sql/pglite/contrib/pgcrypto';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
const db=new PGlite({extensions:{vector,pgcrypto}});
try {
 await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
 for(const file of ['../migrations/20261104000100_initial_schema.sql','../migrations/20261106000100_simple_commitment_capture.sql'])
   await db.exec(await readFile(new URL(file,import.meta.url),'utf8'));
 console.log(JSON.stringify({ready:true}));
 for await(const line of createInterface({input:process.stdin,crlfDelay:Infinity})) {
   try {const request=JSON.parse(line);const result=await db.query(request.sql,request.params||[]);
     console.log(JSON.stringify({rows:result.rows}));
   } catch(error) {console.log(JSON.stringify({error:error.message}));}
 }
} catch(error) {console.log(JSON.stringify({error:error.message}));process.exitCode=1;}
finally {await db.close();}
