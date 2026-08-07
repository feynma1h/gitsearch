-- 0011: compact per-repo enrichment term set (ADR 0020 addendum).
--
-- The search lane never needs enrichment tsvectors' positions or
-- weights — only lexeme membership (@@ against single terms and the
-- full-query bar). The source rows are TOASTed kilobytes each (LLM
-- rows carry a description plus 5-8 queries), so any plan that
-- touches many rows pays a detoast per row; at 300K rows that is
-- seconds-to-minutes of IO on small compute. This table folds each
-- repo's rows into ONE stripped tsvector of distinct lexemes —
-- inline-small and cheap to fetch. Per-slot coverage flags are exact
-- (bool_or of single-term @@ over rows == single-term @@ over the
-- union). Arm two's membership bar changes from per-row to union
-- semantics: a repo whose sources JOINTLY cover the query now passes
-- where individually they would not. Wider by a hair, accepted — a
-- repo has at most two rows and the LLM row carries most vocabulary,
-- and admitted repos still rank through coverage and stars.
--
-- Refresh: batch-time only, alongside the writers (mine_awesome,
-- enrich_llm --collect). `make refresh-enrichment-terms` reruns the
-- INSERT below; there are no online writers to race.
--
-- Removal (with the rest of phase 2): DROP TABLE
-- repository_enrichment_terms;

CREATE TABLE IF NOT EXISTS repository_enrichment_terms (
    repo_id text PRIMARY KEY
        REFERENCES repositories(id) ON DELETE CASCADE,
    terms   tsvector NOT NULL
);

TRUNCATE repository_enrichment_terms;

INSERT INTO repository_enrichment_terms (repo_id, terms)
SELECT en.repo_id,
       array_to_tsvector(array_agg(DISTINCT lex.lexeme))
FROM repository_enrichment en,
     LATERAL unnest(tsvector_to_array(en.search_tsv)) AS lex(lexeme)
GROUP BY en.repo_id;

CREATE INDEX IF NOT EXISTS idx_repository_enrichment_terms_gin
    ON repository_enrichment_terms USING GIN (terms);

ANALYZE repository_enrichment_terms;

-- (The name lane's translate() arms are already covered by ADR
-- 0020's idx_repositories_{name,full_name}_norm; the lane's seq
-- scans came from the unguarded `name % $31` arm with $31 = '' — an
-- empty trigram pattern can't use the GIN index, and one unindexable
-- OR arm sinks the whole chain. Fixed in the statement itself, no
-- new index needed.)
