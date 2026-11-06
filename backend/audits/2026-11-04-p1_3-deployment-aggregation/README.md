# P1.3 local deployment and aggregation evidence

These artifacts were produced on November 4, 2026 (America/Los_Angeles), using
the existing local database and explicitly identified audit conversations.
See the [report](../../../P1_3_DEPLOYMENT_AND_AGGREGATION_REPORT.md).

- `before.json` / `after.json`: counts, content-hash aggregates, ownership,
  existing function hashes and grants; no source records or credentials.
- `deployment.json`: initial session-role permissions and checkpoint migrations.
  Final tightened migration-table permissions are in `preservation-and-permissions.json`.
- `preservation-and-permissions.json`: unchanged source/reference data and
  existing roles/functions; exact final session privileges and zero checkpoint
  state remaining for this task's deleted test sessions.
- `session-verification.json` / `pre-restart-session.json`: exact application
  state comparison across a real backend process restart.
- `aggregation-before.json`: installed SQL definitions and actual service-label
  vocabulary, counts, observed date ranges and missing-value diagnostics.
- `live-category-catalog.json` / `live-road-clarification.json`: verified label
  discovery and ambiguity handling without analytical SQL or an LLM call.
- `direct-sql-verification.json`: 22 real-data tool/direct-SELECT comparisons and
  independently checked evidence references.
- `live-aggregation-verification.json`: the live multi-label follow-up preserving
  both pins, 800 meters and the original dates, with counts A=14 / B=12.
- `live-*-fixed.json`: successful live requests following the diagnosed fixes.
  The corresponding original failure and diagnostic artifacts are retained for
  transparency; they are not successful verification results.
- `frontend-and-cleanup-verification.json`: real Edge desktop/mobile checks,
  confirmed deletion and cleanup of only the task's three conversations.
- `frontend-*.png`: actual browser screenshots taken before test-session cleanup.

Only aggregate public-data outputs and synthetic query pins/labels are stored.
Passwords and keys remain in ignored local `.env`; backend PID/log files are
also ignored. Existing user conversations were not deleted (none existed at
the baseline). No ingestion or geocoding job ran.
