-- v3.86 Federation delegations: a job id names one delegation per workspace.
--
-- The primary key is (workspace, peer, job id), so two peers could each send an
-- offer under the same job id; the local API addresses a delegation by job id
-- alone and then picked either row. Job ids are random (130 bits) and chosen
-- by the requester, so a repeat from a second peer is refused on arrival.

CREATE UNIQUE INDEX IF NOT EXISTS uq_federation_delegations_job
    ON federation_delegations (workspace_id, federation_job_id);
