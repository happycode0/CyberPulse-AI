-- More ground truth (PLAN.md section 9, Stage 2 item 5): OSV and GitHub advisories per CVE, and
-- the cached MITRE ATT&CK and ATLAS catalogues that technique suggestions must choose from.

-- ─── Advisories ─────────────────────────────────────────────────────────────────────────────────
--
-- One row per advisory that names a CVE the pipeline has seen. OSV carries both its own record
-- of the CVE and GitHub's reviewed advisories (GHSA), so one register answers for both. What is
-- kept is what a reader can check: which packages are affected and which versions fix them.
create table cve_advisories (
    cve_id       text not null references cves (cve_id) on delete cascade,
    advisory_id  text not null,
    -- 'osv' for OSV's own record of the CVE, 'ghsa' for a GitHub advisory
    source       text not null check (source in ('osv', 'ghsa')),
    summary      text,
    -- GitHub's own rating where it has reviewed the advisory; null otherwise. Not part of the
    -- severity chain of section 2.5, which stays CNA -> CISA ADP -> NVD.
    severity     text check (severity in ('critical', 'high', 'medium', 'low')),
    reviewed     boolean not null default false,
    -- [{"ecosystem": "npm", "name": "pkg", "fixed": ["1.2.3"]}]; "fixed" empty means no fix yet
    packages     jsonb not null default '[]'::jsonb,
    published    timestamptz,
    modified     timestamptz,
    url          text not null,
    observed_at  timestamptz not null default now(),
    primary key (cve_id, advisory_id)
);

-- When each CVE was last asked about, as cve_cvss_checks (004) does for the CVSS chain.
create table cve_advisory_checks (
    cve_id     text primary key references cves (cve_id) on delete cascade,
    checked_at timestamptz not null default now(),
    -- 'found'  OSV has a record, and its advisories were written
    -- 'absent' OSV has no record under this id (HTTP 404)
    -- 'error'  the lookup failed; says nothing about the CVE, so it is retried soonest
    outcome    text not null check (outcome in ('found', 'absent', 'error')),
    detail     text
);
create index ix_cve_advisory_checks_checked_at on cve_advisory_checks (checked_at);

-- ─── MITRE catalogues ───────────────────────────────────────────────────────────────────────────
--
-- `mitre_dataset_versions` (001) gets one row per loaded release: 'ATT&CK v19.2' for enterprise,
-- 'ATLAS 2026.09' for ATLAS. The newest loaded release of a matrix is the one suggestions use.
alter table mitre_dataset_versions
    add column techniques integer not null default 0,
    add column source_url text;
alter table mitre_dataset_versions
    add constraint mitre_dataset_versions_matrix_check check (matrix in ('enterprise', 'atlas'));

create table mitre_catalogue (
    dataset_version text not null references mitre_dataset_versions (version) on delete cascade,
    technique_id    text not null,
    name            text not null,
    -- tactic short names ('initial-access') for ATT&CK, tactic ids ('AML.TA0004') for ATLAS
    tactics         text[] not null default '{}',
    parent_id       text,
    primary key (dataset_version, technique_id)
);

-- Technique suggestions are bookkeeping like the other enrichment tasks (006).
alter table event_enrichment drop constraint event_enrichment_task_check;
alter table event_enrichment add constraint event_enrichment_task_check
    check (task in ('triage', 'brief', 'severity', 'mitre'));
