-- D18: documents are uploaded with a user-chosen, non-unique name and keyed by
-- UUID; each file is identified by its sha256, and a file is indexed only once.
-- Text IDs cannot be converted to UUIDs, so this refuses to run on existing data.

DO $$
BEGIN
    IF EXISTS (SELECT FROM docs.papers) THEN
        RAISE EXCEPTION '003_documents needs empty docs.papers; run `make reset`';
    END IF;
END
$$;

ALTER TABLE docs.revisions DROP CONSTRAINT revisions_paper_id_fkey;
ALTER TABLE docs.revisions ALTER COLUMN paper_id TYPE uuid USING paper_id::uuid;
ALTER TABLE docs.papers ALTER COLUMN id TYPE uuid USING id::uuid;
ALTER TABLE docs.papers ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE docs.revisions
    ADD CONSTRAINT revisions_paper_id_fkey
    FOREIGN KEY (paper_id) REFERENCES docs.papers (id);

ALTER TABLE docs.papers ADD COLUMN name text NOT NULL CHECK (btrim(name) <> '');
ALTER TABLE docs.papers ALTER COLUMN title DROP NOT NULL;

ALTER TABLE docs.revisions DROP CONSTRAINT revisions_paper_id_pdf_sha256_key;
CREATE UNIQUE INDEX revisions_pdf_sha256 ON docs.revisions (pdf_sha256);
