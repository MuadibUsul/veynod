-- v3.62 Remove the owner's "try and accept" page (spec-remove-owner-acceptance).
--
-- The owner asked for the page and its verdicts to go: tasks, DONE and each
-- task's own acceptance items are unchanged. Its verdict table goes with it.

DROP TABLE IF EXISTS owner_acceptance;
