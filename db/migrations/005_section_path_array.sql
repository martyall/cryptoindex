-- A paragraph's section path as the list of headings it sits under, not a
-- joined string that readers would have to split (and that a heading
-- containing the separator would make ambiguous). Any existing value is kept
-- whole as a single element; nothing splits it.

ALTER TABLE docs.paragraphs ALTER COLUMN section_path DROP DEFAULT;
ALTER TABLE docs.paragraphs
    ALTER COLUMN section_path TYPE text[]
    USING CASE WHEN section_path = '' THEN '{}'::text[] ELSE ARRAY[section_path] END;
ALTER TABLE docs.paragraphs ALTER COLUMN section_path SET DEFAULT '{}';
