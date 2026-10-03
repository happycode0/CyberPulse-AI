# Failure-injection tests (PLAN.md §11)

Each test injects one failure from PLAN.md §11 "Failure model" and checks that the worker does
what the plan says. They use httpx.MockTransport, a database engine that refuses every
connection, git against local repositories in tmp_path, and in-memory stand-ins for the
`notifications` and `incidents` tables. `conftest.py` makes any request that reaches httpcore
fail the test, removes every credential from the environment, and builds the fake credentials
at run time.

Run them with `.venv/bin/python -m pytest -q -p no:cacheprovider tests/failure`.
`test_breaker_db.py` uses the integration tests' `db` fixture, so it skips unless
`DATABASE_URL` is set.

| §11 failure | Planned behaviour | Tests | What they prove |
|---|---|---|---|
| OpenRouter down/exhausted | Deterministic pipeline continues; `pending_enrichment: true`; enrich later | `test_openrouter_failures.py`: `test_a_new_event_is_built_with_no_model_and_starts_pending_enrichment`, `test_openrouter_down_leaves_the_task_waiting_for_a_later_pass[503,500,timeout,connect]`, `test_a_free_model_429_falls_through_to_tier_one`, `test_a_paid_429_pauses_paid_calls_and_editorial_work_waits`, `test_an_exhausted_budget_stops_or_narrows_spending`, `test_an_enrichment_pass_that_raises_is_recorded_as_failed`, plus the five §7.4 tests (`test_over_half_…`, `test_half_or_less_…`, `test_under_a_fifth_…`, `test_an_exhausted_budget_is_free_models_only`, `test_a_stale_budget_reading_falls_back_to_free_only`) | Events are built with no model and start pending. A 5xx, a timeout or a refused connection is tried once, bills nothing and leaves the task `Waiting` for the next pass. A free-model 429 steps up to tier 1 with no `:free` model. A paid 429 pauses paid calls. A 402 on credits stops AI; a 402 on the key limit gives FREE_ONLY. A pass that raises is recorded as not completed. The budget tiers route as §7.4 says. |
| Tavily down | Discovery skipped; collection unaffected | `test_source_failures.py`: `test_tavily_down_skips_discovery_without_raising[connect,503]`, `test_a_discovery_pass_that_raises_is_recorded_and_the_schedule_goes_on`, `test_collection_does_not_read_tavily` | Each query's failure is recorded and the next query is tried; nothing raises. A pass that raises is recorded, and after 50h the watchdog opens a medium `job-failing`. The pipeline module never calls Tavily. |
| One source fails | Others continue; health recorded; degradation counter increments | `test_source_failures.py`: `test_a_feed_answering_5xx_is_retried_then_recorded_as_an_error`, `test_a_feed_that_times_out_is_recorded_as_a_timeout`, `test_a_feed_that_hangs_past_its_deadline_is_cut_off`, `test_a_feed_answering_garbage_yields_nothing_and_its_body_is_kept`, `test_one_failing_source_does_not_stop_the_others`, `test_five_failures_in_a_row_degrade_a_source_and_open_a_finding` | A 5xx is retried 3 times, then recorded as ERROR. A timeout or a hang past the deadline is recorded as TIMEOUT. Garbage gives EMPTY or "parse failed", and the raw body stays in the cache. The other sources still return their items. Five failures in a row move a source from ACTIVE to DEGRADED and give a high `feed-failing` finding. |
| Paperclip down | Worker keeps collecting and publishing | `test_watchdog_failures.py`: `test_paperclip_down_is_found_on_the_third_probe_and_told_to_telegram_only`, `test_with_the_routine_unreachable_telegram_is_still_told_and_the_fire_retried`, `test_with_paperclip_down_the_worker_keeps_collecting_and_publishing` | Three failed probes open a high `paperclip-down` incident, which Telegram is told of and the crew is not. With the routine unreachable, Telegram still goes out, and the fire is retried until it lands, at most 3 attempts. A lane run collects, publishes and pushes without asking Paperclip anything. |
| Postgres down | Collection halts, raw cache retained, alert raised; no data loss | `test_watchdog_failures.py`: `test_a_refused_connection_is_a_failed_ping`, `test_postgres_down_is_alerted_once_after_three_passes_and_its_return_too`, `test_a_database_alert_that_fails_to_send_is_tried_on_the_next_pass`, `test_postgres_down_halts_collection_before_any_fetch_and_nothing_is_published`, `test_the_ops_api_answers_503_while_the_database_is_down` | The watchdog's ping fails. After 3 failed passes Telegram is told once. A failed alert is sent again on the next pass. When the database is back, a "back" notice is claimed. A lane run stops before it fetches anything, and nothing is published. The ops API answers 503 to reads, verdicts and reports, and asks for the write to be sent again. See Gaps for the raw cache. |
| GitHub push fails | Output retained locally, retried with backoff | `test_publish_failures.py`: `test_a_failed_push_keeps_the_data_and_the_next_publish_pushes_it`, `test_push_data_raises_rather_than_reporting_success`, `test_push_failures_past_45_minutes_open_a_push_failure_incident` | With the remote missing, the publish is recorded as done and the push as failed (`RuntimeError`). `data/` is unchanged, and the token is not in the log. Once the remote exists, the next publish pushes. Failed pushes open a high `push-failure` after 45 minutes, not before. See Gaps for the backoff. |
| Pages build fails | Previous site stays up; incident opened | `test_watchdog_failures.py`: `test_site_data_four_hours_old_opens_a_site_stale_incident`, `test_an_unreadable_site_opens_an_incident_only_on_the_third_failed_read[404,500]` | Public data older than 3h opens a high `site-stale` incident, and Telegram is told. A site that cannot be read opens one only on the third failed read in a row, with the HTTP status as evidence. |
| One agent fails | Others continue; task recoverable | `test_watchdog_failures.py`: `test_one_check_failing_does_not_stop_the_others`, `test_a_watchdog_pass_that_raises_is_recorded_and_the_schedule_goes_on`, `test_three_failed_verdicts_trip_the_breaker_and_then_only_a_person_is_told`, `test_a_verdict_holding_a_secret_is_refused`; `test_breaker_db.py`: `test_three_failed_verdicts_through_the_api_trip_the_breaker` (needs DB) | One of SERAPH's checks raising fails that check alone, by name: the wake answers 503, and the other three checks still answer. A watchdog pass that raises is recorded, and the schedule goes on. Three failed verdicts trip the breaker. The fourth is answered 409 "halted". The next pass sends only the breaker notice to Telegram and does not wake the crew again. A verdict holding a credential is refused with 400. The DB test proves the trip in the real SQL through the API. |
| Bad model output | Schema validation rejects; retry once; then `pending_enrichment` | `test_openrouter_failures.py`: `test_bad_output_is_retried_once_then_left_pending`, `test_bad_output_then_a_good_answer_is_done` | An answer that fails the schema is tried once more, then the task is `Failed`, with two `invalid_output` rows in the ledger, and `enrich_pending` leaves the event pending
with a backoff (not exercised here). A bad answer followed by a good one is `Done`. |
| Duplicate storm | Rate-limited, flagged as an anomaly, publication gated | `test_watchdog_failures.py`: `test_a_duplicate_storm_opens_a_high_incident_and_is_told` | A storm is flagged: a burst of fresh events far above the usual days opens a high `duplicate-explosion` incident, which Telegram is told of. See Gaps for the rate limit and the gate. |
| Secret in the output (§2.8, publisher) | Publisher fails closed | `test_publish_failures.py`: `test_a_secret_in_the_output_blocks_every_write_and_keeps_the_last_files`, `test_every_outbound_channel_withholds_a_secret_instead_of_sending_it` | A credential-shaped string in an event raises `ValidationFailure`, and the previous files stay byte for byte as they were. Telegram, the Incident routine and the ops API each withhold a message holding one, and nothing is sent. |
| Schema-invalid output | Publisher fails closed | `test_publish_failures.py`: `test_schema_invalid_output_blocks_every_write_and_keeps_the_last_files`, `test_a_blocked_publish_is_not_pushed_and_the_watchdog_calls_it_schema_drift` | An invalid payload raises `ValidationFailure`, and the previous files are kept. The blocked publish is recorded with the note `ValidationFailure`. It is not pushed. After 30 minutes the watchdog opens a high `schema-drift` incident. |

## Gaps

These parts of the plan are not implemented as written, so no test pretends they are.

- **Duplicate storm: "rate-limited" and "publication gated".** Nothing limits the rate of
  ingest during a storm, and nothing stops publishing. What exists: the `duplicate-explosion`
  incident (tested above), url_hash dedupe in `_ingest`, a 500-event cap on the live view, at
  most 5 Telegram alerts per pass, a 32 MiB cap on each fetch, and 3 tries per fetch.
- **GitHub push: "retried with backoff".** There is no backoff. The retry is the next scheduled
  publish: the fast lane runs every 15 minutes. The watchdog opens `push-failure` after 45
  minutes of failed pushes.
- **Postgres down: "raw cache retained".** `run_lane` reads the database before any fetch, so
  with Postgres down at the start of a run nothing is fetched and nothing is cached. The raw
  cache only holds bodies fetched before the database was lost mid-run. "No data loss" holds
  another way: the fetch state (ETag and Last-Modified) is saved only after a successful
  store, so the next run fetches the same items again. The alert comes from the watchdog after
  3 failed passes, about 15 minutes, not from the collector.
- **Pages build fails: "previous site stays up".** That is GitHub Pages' behaviour, outside the
  worker. The worker's part is the `site-stale` incident, tested above. It cannot tell a failed
  Pages build from a failed push or a stopped worker, and the incident evidence does not say
  which one it was.
- **One agent fails: "task recoverable".** Agent runs are orchestrated in Paperclip, not in the
  worker. The worker's part is tested above: each ops request is isolated, a failed pass is
  recorded, and the breaker stops fixes after 3 failures. That a follow-up task stays due
  until it is reported is the worker's design, but no test here covers it.
- **Observation, not a bug.** With Paperclip down and the Incident routine still configured,
  Telegram notices for other incidents still say "The crew has been told", and that may not be
  true until the routine's fire is retried and lands.
- **Not verified locally.** `test_breaker_db.py` skips without `DATABASE_URL`. The tables are
  stood in for everywhere else, and `record_verdict`'s SQL is also covered by
  `tests/integration/test_watchdog_db.py`.
