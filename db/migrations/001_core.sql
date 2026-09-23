-- Core schema. See docs/DATA_MODEL.md for the meaning of each table.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS docs;

CREATE TABLE docs.meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

-- embed_model is recorded when the first embeddings are written (Phase 4);
-- embed_dims is fixed by the halfvec(1024) columns below.
INSERT INTO docs.meta (key, value) VALUES
    ('schema_version', '1'),
    ('embed_dims', '1024');

CREATE TABLE docs.papers (
    id                text PRIMARY KEY,               -- ePrint ID, e.g. '2024/1234'
    title             text NOT NULL,
    authors           text[] NOT NULL DEFAULT '{}',
    abstract          text,
    subjects          text[] NOT NULL DEFAULT '{}',
    license           text,
    arxiv_id          text,
    oai_datestamp     timestamptz,
    fetched_datestamp timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE docs.revisions (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    paper_id       text NOT NULL REFERENCES docs.papers (id),
    revision       integer NOT NULL CHECK (revision >= 1),
    source_kind    text NOT NULL CHECK (source_kind IN ('pdf', 'arxiv_latex', 'arxiv_html')),
    pdf_sha256     text,
    pdf_path       text,
    parser         text,
    parser_version text,
    stage          text NOT NULL DEFAULT 'parse'
                   CHECK (stage IN ('parse', 'segment', 'embed', 'ready', 'failed')),
    is_current     boolean NOT NULL DEFAULT false,
    attempts       integer NOT NULL DEFAULT 0,
    locked_at      timestamptz,
    last_error     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (paper_id, revision),
    UNIQUE (paper_id, pdf_sha256)
);

CREATE UNIQUE INDEX revisions_one_current
    ON docs.revisions (paper_id) WHERE is_current;

CREATE INDEX revisions_pending_stage
    ON docs.revisions (stage) WHERE stage NOT IN ('ready', 'failed');

CREATE TABLE docs.paragraphs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id  bigint NOT NULL REFERENCES docs.revisions (id) ON DELETE CASCADE,
    position     integer NOT NULL CHECK (position >= 0),
    section_path text NOT NULL DEFAULT '',
    text         text NOT NULL,
    content_hash text NOT NULL,
    block_kind   text,
    block_label  text,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,  -- D15
    latex_norm   text,
    emb          halfvec(1024),
    UNIQUE (revision_id, position)
);

CREATE TABLE docs.units (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id    bigint NOT NULL REFERENCES docs.revisions (id) ON DELETE CASCADE,
    first_pos      integer NOT NULL,
    last_pos       integer NOT NULL,
    anchor_label   text,
    gloss          text NOT NULL,
    terms          text[] NOT NULL DEFAULT '{}',
    gloss_model    text NOT NULL,
    prompt_version text NOT NULL,
    input_hash     text NOT NULL,
    emb_gloss      halfvec(1024),
    CHECK (0 <= first_pos AND first_pos <= last_pos),
    UNIQUE NULLS NOT DISTINCT (revision_id, first_pos, last_pos, anchor_label)
);

CREATE TABLE docs.unit_questions (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    unit_id  bigint NOT NULL REFERENCES docs.units (id) ON DELETE CASCADE,
    question text NOT NULL,
    emb      halfvec(1024)
);

CREATE INDEX unit_questions_unit ON docs.unit_questions (unit_id);

CREATE TABLE docs.events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       text NOT NULL
               CHECK (kind IN ('revision_ready', 'unit_changed', 'paper_revised', 'embed_model_changed')),
    payload    jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX paragraphs_emb ON docs.paragraphs USING hnsw (emb halfvec_cosine_ops);
CREATE INDEX units_emb_gloss ON docs.units USING hnsw (emb_gloss halfvec_cosine_ops);
CREATE INDEX unit_questions_emb ON docs.unit_questions USING hnsw (emb halfvec_cosine_ops);
CREATE INDEX paragraphs_tsv ON docs.paragraphs USING gin (tsv);
CREATE INDEX paragraphs_latex_trgm ON docs.paragraphs USING gin (latex_norm gin_trgm_ops);
