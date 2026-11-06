# Historical module migrations

These unchanged sources preserve earlier additive proposals/applied upgrades for
review and reconciliation. They are **not** an active fresh-install migration chain.

For a fresh database, run only `../20261104000100_initial_schema.sql`. It contains
their definitions and the final document formats in one transaction. Do not run
these module scripts afterward: their objects already exist.

Existing hosted databases retain their existing migration history. Never replay
the destructive consolidated baseline there. Any future hosted upgrade needs a
separate reviewed additive migration and explicit approval. No hosted schema or
migration-history records were modified by the local consolidation.
