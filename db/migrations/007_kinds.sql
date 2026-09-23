-- Searching by kind (D25): a unit's anchor also records the document's own
-- word for it ("lemma", "protocol"), an open vocabulary, and both the
-- paragraph's structural kind and the unit's kinds are indexed.

ALTER TABLE docs.units
    ADD COLUMN anchor_term text,
    ADD CONSTRAINT units_anchor_term_pair
        CHECK ((anchor_label IS NULL) = (anchor_term IS NULL));

CREATE INDEX paragraphs_block_kind ON docs.paragraphs (block_kind)
    WHERE block_kind IS NOT NULL;
CREATE INDEX units_anchor_kind ON docs.units (anchor_kind)
    WHERE anchor_kind IS NOT NULL;
CREATE INDEX units_anchor_term ON docs.units (anchor_term)
    WHERE anchor_term IS NOT NULL;
