-- Phase 2.

-- The stage a revision was in when it went to `failed`, so a retry can resume
-- there without reading it back out of last_error.
ALTER TABLE docs.revisions
    ADD COLUMN failed_stage text CHECK (failed_stage IN ('parse', 'segment', 'embed'));

-- Where each paragraph sits in the PDF, from the parser's layout: the page
-- (0-based) feeds citation locators, the box the evaluation review page.
ALTER TABLE docs.paragraphs
    ADD COLUMN page integer CHECK (page >= 0),
    ADD COLUMN bbox real[] CHECK (cardinality(bbox) = 4);

-- Near-duplicate warning: after parsing, the other document whose paragraphs
-- this revision shares most, and the shared fraction. A warning only.
ALTER TABLE docs.revisions
    ADD COLUMN similar_paper_id uuid REFERENCES docs.papers (id) ON DELETE SET NULL,
    ADD COLUMN similarity real CHECK (similarity BETWEEN 0 AND 1);

CREATE INDEX paragraphs_content_hash ON docs.paragraphs (content_hash);
