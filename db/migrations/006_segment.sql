-- Phase 3: segmentation and glossing (docs/DATA_MODEL.md, docs.units and
-- docs.segment_chunks).

-- One validated LLM reply per chunk of a section, keyed by the chunk's input
-- hash. Written as each reply arrives, before the stage's own transaction,
-- so a retry after a later chunk fails makes no new call for this one.
CREATE TABLE docs.segment_chunks (
    revision_id    bigint NOT NULL REFERENCES docs.revisions (id) ON DELETE CASCADE,
    input_hash     text NOT NULL,
    gloss_model    text NOT NULL,
    prompt_version text NOT NULL,
    response       jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (revision_id, input_hash)
);

ALTER TABLE docs.units
    ADD COLUMN anchor_pos integer,
    ADD COLUMN anchor_kind text
        CHECK (anchor_kind IN ('theorem', 'definition', 'algorithm', 'game', 'example', 'other')),
    ADD COLUMN flags text[] NOT NULL DEFAULT '{}',
    ADD CONSTRAINT units_anchor_whole CHECK (
        (anchor_label IS NULL) = (anchor_pos IS NULL)
        AND (anchor_label IS NULL) = (anchor_kind IS NULL)),
    ADD CONSTRAINT units_anchor_in_span
        CHECK (anchor_pos IS NULL OR anchor_pos BETWEEN first_pos AND last_pos);
