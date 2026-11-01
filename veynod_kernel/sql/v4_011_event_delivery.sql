-- v4_011: event envelope + outbox dead-letter/backoff (Phase 3).
--
-- kernel_events stays the durable cursor log.  The extra columns let a
-- consumer correlate an event with a product/task attempt and replay it.
-- event_outbox gains a bounded retry path so a permanently failing
-- notification cannot spin forever without an operator-visible record.

ALTER TABLE kernel_events
    ADD COLUMN IF NOT EXISTS event_id UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS event_version INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS producer TEXT NOT NULL DEFAULT 'kernel',
    ADD COLUMN IF NOT EXISTS correlation_id TEXT,
    ADD COLUMN IF NOT EXISTS causation_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_kernel_events_event_id
    ON kernel_events (event_id);
CREATE INDEX IF NOT EXISTS idx_kernel_events_correlation
    ON kernel_events (correlation_id)
    WHERE correlation_id IS NOT NULL;

ALTER TABLE event_outbox
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_event_outbox_due
    ON event_outbox (available_at, outbox_id)
    WHERE published_at IS NULL AND dead_lettered_at IS NULL;

INSERT INTO schema_migrations (version)
VALUES ('v4_011_event_delivery')
ON CONFLICT (version) DO NOTHING;
