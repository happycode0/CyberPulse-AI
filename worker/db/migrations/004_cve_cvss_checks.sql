-- When each CVE was last asked about, and what came back.
--
-- KEV and EPSS are bulk registers: one download answers for every CVE at once, so there is
-- nothing to remember between runs. CVSS is not. The severity chain of PLAN.md section 2.5
-- (CNA record -> CISA Vulnrichment ADP -> NVD) is per-record, so resolving it means one HTTP
-- request per CVE, and that only stays polite if the worker can tell which CVEs it has already
-- asked about.
--
-- The outcome is recorded, not just the timestamp, because "no CVSS" is a real and common answer
-- that the rest of the schema cannot hold: cve_scores' own check constraint forbids a cvss row
-- with a null score, exactly so an absent score can never be stored as a zero. Without this table
-- a CVE nobody has scored is indistinguishable from one never looked up, and it would be re-fetched
-- on every run forever.
--
-- This is bookkeeping about our own activity, not a fact about the vulnerability, which is why it
-- is not a column on `cves`: a reader of that table should not have to know which of its columns
-- describe the CVE and which describe the crawler.

create table cve_cvss_checks (
    cve_id     text primary key references cves (cve_id) on delete cascade,
    checked_at timestamptz not null default now(),
    -- 'scored'   a base score was found and written to cve_scores
    -- 'unscored' the record exists but carries no CVSS metric in any container
    -- 'absent'   the register has no record under this id at all (HTTP 404)
    -- 'error'    the lookup failed; says nothing about the CVE, so it is retried soonest
    outcome    text not null check (outcome in ('scored', 'unscored', 'absent', 'error')),
    detail     text
);

-- The sync picks the next batch with "order by checked_at nulls first", so this index is what
-- keeps that from scanning the whole table once the backfill is done.
create index ix_cve_cvss_checks_checked_at on cve_cvss_checks (checked_at);
