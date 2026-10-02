"""Ground-truth persistence against a real Postgres.

These need a real database rather than a fake connection for three reasons that are the point of the
module: `distinct on` is what makes "the newest score" well defined, `make_interval` is what makes
the recheck intervals expressible in one query, and the `cve_scores` check constraint is what
actually enforces "an absent score is never stored as zero". A fake connection would let all three
be wrong.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from worker.db.groundtruth import (
    RECHECK_HOURS,
    apply_kev,
    cves_due_for_cvss,
    events_for_cves,
    known_cve_ids,
    latest_score,
    record_cvss,
    record_cvss_check,
    record_epss,
    set_event_severity,
)
from worker.db.migrate import run_migrations
from worker.groundtruth.kev import KevCatalogue
from worker.models import CvssScore, EpssScore, KevEntry
from worker.version import (
    ENRICHMENT_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    SCORING_VERSION,
)

NOW = datetime(2026, 10, 2, 2, 25, tzinfo=UTC)
LISTED = "CVE-2024-3400"
UNLISTED = "CVE-2023-1234"

# `events_event_id_check` requires `evt-<yyyy>-<nnnnnn>`, so these are ids `next_event_id` could
# actually have allocated rather than labels.
EVENT = "evt-2026-000001"
EVENT_2 = "evt-2026-000002"
EVENT_3 = "evt-2026-000003"


@pytest.fixture
def conn(pg_engine):
    """A connection in a transaction that is always rolled back, starting from empty CVE tables."""
    run_migrations(pg_engine)
    with pg_engine.connect() as c:
        trans = c.begin()
        c.execute(text("delete from events"))
        c.execute(text("delete from cves"))
        for cve_id in (LISTED, UNLISTED):
            c.execute(text("insert into cves (cve_id) values (:c)"), {"c": cve_id})
        try:
            yield c
        finally:
            trans.rollback()


def catalogue(*cve_ids, declared=None, skipped=0, **dates) -> KevCatalogue:
    entries = {
        cve_id: KevEntry(
            listed=True,
            date_added=dates.get("date_added", date(2024, 4, 12)),
            due_date=dates.get("due_date", date(2024, 4, 19)),
        )
        for cve_id in cve_ids
    }
    return KevCatalogue(
        version="2026.09.30",
        released=NOW,
        declared_count=len(entries) if declared is None else declared,
        entries=entries,
        skipped=skipped,
    )


def insert_event(
    conn, event_id, *cve_ids, severity="unknown", severity_source="unknown", first_seen=NOW
):
    conn.execute(
        text(
            "insert into events (event_id, schema_version, pipeline_version, scoring_version, "
            "enrichment_version, first_seen, last_seen, title, normalised_title, summary, "
            "severity, severity_source) values (:id, :schema, :pipeline, :scoring, :enrichment, "
            ":first_seen, :now, 't', 't', 's', :severity, :source)"
        ),
        {
            "id": event_id,
            "schema": SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "scoring": SCORING_VERSION,
            "enrichment": ENRICHMENT_VERSION,
            "now": NOW,
            "first_seen": first_seen,
            "severity": severity,
            "source": severity_source,
        },
    )
    for cve_id in cve_ids:
        conn.execute(
            text("insert into event_cves (event_id, cve_id) values (:e, :c)"),
            {"e": event_id, "c": cve_id},
        )


def severity_of(conn, event_id) -> tuple[str, str]:
    row = conn.execute(
        text("select severity, severity_source from events where event_id = :e"), {"e": event_id}
    ).one()
    return row[0], row[1]


def epss_rows(conn, cve_id) -> list[tuple]:
    return conn.execute(
        text(
            "select score, status from cve_scores where cve_id = :c and kind = 'epss' "
            "order by id"
        ),
        {"c": cve_id},
    ).all()


# --- KEV --------------------------------------------------------------------------------------


def test_a_listing_is_written_with_its_dates(conn):
    result = apply_kev(conn, catalogue(LISTED))
    assert (result.updated, result.unchanged) == (1, 1)
    row = conn.execute(
        text("select kev_listed, kev_date_added, kev_due_date from cves where cve_id = :c"),
        {"c": LISTED},
    ).one()
    assert row == (True, date(2024, 4, 12), date(2024, 4, 19))


def test_a_cve_the_catalogue_does_not_carry_stays_unlisted(conn):
    apply_kev(conn, catalogue(LISTED))
    listed = conn.execute(
        text("select kev_listed from cves where cve_id = :c"), {"c": UNLISTED}
    ).scalar()
    assert listed is False


def test_an_unchanged_catalogue_writes_nothing(conn):
    apply_kev(conn, catalogue(LISTED))
    before = conn.execute(
        text("select updated_at from cves where cve_id = :c"), {"c": LISTED}
    ).scalar()
    result = apply_kev(conn, catalogue(LISTED))
    after = conn.execute(
        text("select updated_at from cves where cve_id = :c"), {"c": LISTED}
    ).scalar()
    assert (result.updated, result.unchanged) == (0, 2)
    assert before == after


def test_a_withdrawn_listing_is_cleared_when_the_catalogue_looks_complete(conn):
    apply_kev(conn, catalogue(LISTED))
    result = apply_kev(conn, catalogue())
    assert result.delisted == 1
    assert conn.execute(
        text("select kev_listed, kev_date_added from cves where cve_id = :c"), {"c": LISTED}
    ).one() == (False, None)


def test_a_truncated_catalogue_adds_but_never_removes(conn):
    """The failure a short download looks exactly like, and the reason `is_complete` exists.

    A truncated KEV response is valid JSON with a short `vulnerabilities` array. Acting on its
    absences would report thousands of known-exploited CVEs as unexploited — the single worst thing
    this pipeline could publish — so absence is only trusted when CISA's own count agrees.
    """
    apply_kev(conn, catalogue(LISTED))
    # Declares 1700 entries, carries none: the shape of a download cut off after the header.
    result = apply_kev(conn, catalogue(declared=1700))
    assert (result.delisted, result.delist_withheld) == (0, 1)
    assert conn.execute(
        text("select kev_listed from cves where cve_id = :c"), {"c": LISTED}
    ).scalar() is True


def test_a_truncated_catalogue_still_records_the_listings_it_does_carry(conn):
    result = apply_kev(conn, catalogue(LISTED, declared=1700))
    assert result.updated == 1
    assert conn.execute(
        text("select kev_listed from cves where cve_id = :c"), {"c": LISTED}
    ).scalar() is True


def test_the_changed_cves_are_reported_so_their_events_can_be_rescored(conn):
    result = apply_kev(conn, catalogue(LISTED))
    assert result.changed_cves == (LISTED,)
    assert apply_kev(conn, catalogue(LISTED)).changed_cves == ()


def test_a_date_the_catalogue_revised_is_an_update(conn):
    apply_kev(conn, catalogue(LISTED))
    result = apply_kev(conn, catalogue(LISTED, due_date=date(2024, 5, 1)))
    assert result.updated == 1


# --- EPSS -------------------------------------------------------------------------------------


def test_a_score_is_recorded_once_and_not_again(conn):
    first = record_epss(conn, {LISTED: EpssScore(score=0.94, status="known")})
    second = record_epss(conn, {LISTED: EpssScore(score=0.94, status="known")})
    assert (first.recorded, second.recorded) == (2, 0)  # the scored CVE and the unknown one
    assert epss_rows(conn, LISTED) == [(pytest.approx(0.94), "known")]


def test_a_moved_score_appends_rather_than_replacing(conn):
    record_epss(conn, {LISTED: EpssScore(score=0.94, status="known")})
    record_epss(conn, {LISTED: EpssScore(score=0.97, status="known")})
    assert [r[0] for r in epss_rows(conn, LISTED)] == [
        pytest.approx(0.94),
        pytest.approx(0.97),
    ]


def test_a_cve_epss_has_not_modelled_gets_an_explicit_unknown_not_a_zero(conn):
    """The row that stops a gap being read as 0%.

    EPSS scores the CVEs its model covers, so absence from the snapshot means unscored. A NULL score
    with status 'unknown' says that; a 0.0 would say the probability of exploitation is nil.
    """
    record_epss(conn, {})
    assert epss_rows(conn, LISTED) == [(None, "unknown")]


def test_an_absence_is_not_re_asserted_on_every_pass(conn):
    record_epss(conn, {})
    assert record_epss(conn, {}).recorded == 0
    assert len(epss_rows(conn, LISTED)) == 1


def test_a_cve_that_gains_a_score_moves_off_unknown(conn):
    record_epss(conn, {})
    record_epss(conn, {LISTED: EpssScore(score=0.5, status="known")})
    assert epss_rows(conn, LISTED) == [(None, "unknown"), (pytest.approx(0.5), "known")]


def test_a_score_for_a_cve_nobody_mentioned_is_not_imported(conn):
    # The snapshot carries ~290,000 CVEs. `cves` is the few thousand some advisory mentioned, and
    # that is deliberately what gets annotated.
    record_epss(conn, {"CVE-1999-0001": EpssScore(score=0.1, status="known")})
    assert known_cve_ids(conn) == sorted([LISTED, UNLISTED])
    assert epss_rows(conn, "CVE-1999-0001") == []


# --- CVSS -------------------------------------------------------------------------------------


def test_a_cvss_row_is_written_once_per_change(conn):
    score = CvssScore(score=9.8, vector="CVSS:3.1/AV:N", source="cna")
    assert record_cvss(conn, LISTED, score) is True
    assert record_cvss(conn, LISTED, score) is False
    assert record_cvss(conn, LISTED, score.model_copy(update={"score": 10.0})) is True


def test_a_new_vector_at_the_same_score_is_still_a_change(conn):
    # Two vectors can compute to the same number, and the vector is the evidence a reader needs to
    # disagree with the band, so replacing one is a real change.
    base = CvssScore(score=9.8, vector="CVSS:3.1/AV:N", source="cna")
    record_cvss(conn, LISTED, base)
    assert record_cvss(conn, LISTED, base.model_copy(update={"vector": "CVSS:3.1/AV:A"})) is True


def test_the_newest_row_wins_even_when_rows_share_a_timestamp(conn):
    """`now()` is fixed for a whole transaction in Postgres, so a batch writes identical timestamps.

    Without the `id` tie-break in the `distinct on` ordering, "the newest score" would be an
    arbitrary row from the batch and the change check could rewrite the same value forever.
    """
    record_cvss(conn, LISTED, CvssScore(score=4.0, vector="v1", source="cna"))
    record_cvss(conn, LISTED, CvssScore(score=7.5, vector="v2", source="cna"))
    assert latest_score(conn, LISTED, "cvss").score == pytest.approx(7.5)


def test_an_unscored_cve_cannot_be_filed_as_a_zero(conn):
    """The constraint that makes the honest-data rule structural rather than a convention."""
    with pytest.raises(Exception, match="cve_scores_check|violates check constraint"):
        conn.execute(
            text("insert into cve_scores (cve_id, kind, score) values (:c, 'cvss', null)"),
            {"c": LISTED},
        )


# --- which CVEs are due -----------------------------------------------------------------------


def test_a_cve_never_checked_is_due(conn):
    assert cves_due_for_cvss(conn, now=NOW, limit=10) == sorted([LISTED, UNLISTED])


def test_never_checked_cves_come_first(conn):
    record_cvss_check(conn, LISTED, "error")
    assert cves_due_for_cvss(conn, now=NOW + timedelta(days=30), limit=10)[0] == UNLISTED


def test_the_cves_on_the_newest_events_are_resolved_first(conn):
    """The tie-break that decides whether a first pass bands the front page or the archive.

    `UNLISTED` sorts before `LISTED` by id, so ordering never-checked CVEs by `cve_id` resolves the
    older one first. On a real database that meant the first live pass spent all 400 lookups between
    CVE-2002 and CVE-2018 while every CVE on the published site stayed unscored.
    """
    insert_event(conn, EVENT, UNLISTED, first_seen=NOW - timedelta(days=900))
    insert_event(conn, EVENT_2, LISTED, first_seen=NOW - timedelta(hours=2))
    assert cves_due_for_cvss(conn, now=NOW, limit=1) == [LISTED]


def test_a_cve_no_event_mentions_is_resolved_last(conn):
    # It cannot be the reason anything on the site reads `unknown`, so it waits behind the ones that
    # can be.
    insert_event(conn, EVENT, UNLISTED, first_seen=NOW - timedelta(days=900))
    assert cves_due_for_cvss(conn, now=NOW, limit=2) == [UNLISTED, LISTED]


def test_the_limit_is_honoured(conn):
    assert len(cves_due_for_cvss(conn, now=NOW, limit=1)) == 1


@pytest.mark.parametrize("outcome,hours", sorted(RECHECK_HOURS.items()))
def test_a_checked_cve_is_not_due_again_until_its_interval_passes(conn, outcome, hours):
    record_cvss_check(conn, LISTED, outcome)
    record_cvss_check(conn, UNLISTED, outcome)
    # `record_cvss_check` stamps now(), which inside this transaction is the transaction's start and
    # not the fixture's NOW, so the window is measured from the timestamp actually stored.
    stored = conn.execute(
        text("select checked_at from cve_cvss_checks where cve_id = :c"), {"c": LISTED}
    ).scalar()
    assert cves_due_for_cvss(conn, now=stored + timedelta(hours=hours - 1), limit=10) == []
    assert LISTED in cves_due_for_cvss(conn, now=stored + timedelta(hours=hours + 1), limit=10)


def test_an_error_is_retried_sooner_than_an_absence(conn):
    # An error said nothing about the CVE; an absence said something unlikely to change.
    assert RECHECK_HOURS["error"] < RECHECK_HOURS["absent"] < RECHECK_HOURS["scored"]


def test_a_checked_cve_is_updated_in_place_rather_than_accumulating(conn):
    record_cvss_check(conn, LISTED, "error", "HTTP 503")
    record_cvss_check(conn, LISTED, "scored", "9.8 from cna")
    rows = conn.execute(
        text("select outcome, detail from cve_cvss_checks where cve_id = :c"), {"c": LISTED}
    ).all()
    assert rows == [("scored", "9.8 from cna")]


def test_an_outcome_the_schema_does_not_know_is_refused(conn):
    # The guard that keeps a typo from creating a fifth outcome with no recheck interval, which
    # would be looked up once and then never again.
    with pytest.raises(Exception, match="check constraint"):
        record_cvss_check(conn, LISTED, "maybe")


# --- severity ---------------------------------------------------------------------------------


def test_an_event_is_banded_from_its_cves_score(conn):
    insert_event(conn, EVENT, LISTED)
    record_cvss(conn, LISTED, CvssScore(score=9.8, vector="v", source="cna"))
    assert set_event_severity(conn) == [EVENT]
    assert severity_of(conn, EVENT) == ("critical", "cna")


def test_an_event_takes_the_worst_of_its_cves(conn):
    insert_event(conn, EVENT, LISTED, UNLISTED)
    record_cvss(conn, LISTED, CvssScore(score=5.0, vector="v", source="nvd"))
    record_cvss(conn, UNLISTED, CvssScore(score=9.1, vector="v", source="cisa_adp"))
    set_event_severity(conn)
    # The provenance follows the winning score, not some other CVE in the same advisory.
    assert severity_of(conn, EVENT) == ("critical", "cisa_adp")


def test_an_event_with_no_scored_cve_is_left_alone(conn):
    """A missing lookup is not evidence, so it must not overwrite someone else's answer.

    The column defaults to 'unknown' anyway, so an event nothing has assessed still reads as unknown
    without this function asserting it — and a vendor rating survives until a CVSS outranks it.
    """
    insert_event(conn, EVENT, LISTED, severity="high", severity_source="vendor")
    assert set_event_severity(conn) == []
    assert severity_of(conn, EVENT) == ("high", "vendor")


def test_a_measured_score_outranks_an_estimate(conn):
    insert_event(conn, EVENT, LISTED, severity="low", severity_source="ai_estimate")
    record_cvss(conn, LISTED, CvssScore(score=9.8, vector="v", source="cna"))
    set_event_severity(conn)
    assert severity_of(conn, EVENT) == ("critical", "cna")


def test_an_unchanged_band_is_not_rewritten(conn):
    insert_event(conn, EVENT, LISTED)
    record_cvss(conn, LISTED, CvssScore(score=9.8, vector="v", source="cna"))
    set_event_severity(conn)
    assert set_event_severity(conn) == []


def test_a_provider_the_enum_does_not_know_keeps_the_band_but_not_the_provenance(conn):
    insert_event(conn, EVENT, LISTED)
    record_cvss(conn, LISTED, CvssScore(score=9.8, vector="v", source="some-new-adp"))
    set_event_severity(conn)
    assert severity_of(conn, EVENT) == ("critical", "unknown")


def test_a_zero_score_bands_low_rather_than_unknown(conn):
    # `Severity` has no NONE member, so 0.0 lands in LOW: it overstates by one band and never
    # understates, and scoring.yaml weights `low: 1` below `unknown: 1.5` so a CVE known to have no
    # impact still ranks beneath an unrated one.
    insert_event(conn, EVENT, LISTED)
    record_cvss(conn, LISTED, CvssScore(score=0.0, vector="v", source="cna"))
    set_event_severity(conn)
    assert severity_of(conn, EVENT) == ("low", "cna")


# --- joining CVEs back to events --------------------------------------------------------------


def test_the_events_mentioning_a_cve_are_found(conn):
    insert_event(conn, EVENT, LISTED)
    insert_event(conn, EVENT_2, LISTED, UNLISTED)
    insert_event(conn, EVENT_3, UNLISTED)
    assert events_for_cves(conn, [LISTED]) == [EVENT, EVENT_2]


def test_an_event_mentioning_two_changed_cves_is_listed_once(conn):
    insert_event(conn, EVENT, LISTED, UNLISTED)
    assert events_for_cves(conn, [LISTED, UNLISTED]) == [EVENT]


def test_no_cves_means_no_query_and_no_events(conn):
    assert events_for_cves(conn, []) == []
