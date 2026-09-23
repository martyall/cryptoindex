-- D27: a second embedding model's vectors beside the primary ones, for
-- comparing models during development; removed once the choice is made.

ALTER TABLE docs.paragraphs ADD COLUMN emb_alt halfvec(1024);
ALTER TABLE docs.units ADD COLUMN emb_gloss_alt halfvec(1024);
ALTER TABLE docs.unit_questions ADD COLUMN emb_alt halfvec(1024);

CREATE INDEX paragraphs_emb_alt
    ON docs.paragraphs USING hnsw (emb_alt halfvec_cosine_ops);
CREATE INDEX units_emb_gloss_alt
    ON docs.units USING hnsw (emb_gloss_alt halfvec_cosine_ops);
CREATE INDEX unit_questions_emb_alt
    ON docs.unit_questions USING hnsw (emb_alt halfvec_cosine_ops);
