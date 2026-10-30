-- v3.64 One approval, every identity a computer needs (up-hub-p1-up-hub,
-- docs/WAKE_AND_DISPATCH.md §14.5).
--
-- `npx veynod up --run-tasks` needs two agents on one computer: the computer's
-- own (EXECUTOR) and a `-review` one (AUDITOR), so the Loop can review there
-- what was built there. The terminal now says which identities it wants
-- (`identities`: name, role, why); the console lists them and the owner
-- unticks any. One approval mints the first as before (sealed_key) and each
-- ticked extra, sealed to the same terminal key (`sealed_keys`). The poll
-- returns them all once and clears the sealed copies.

ALTER TABLE cli_sessions ADD COLUMN IF NOT EXISTS identities JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE cli_sessions ADD COLUMN IF NOT EXISTS sealed_keys JSONB;
