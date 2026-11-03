# P1.2.2 Google evaluation: prepared, not live-executed

November 2, 2026. **Zero live Google requests were made.** The configured
environment does not contain `GOOGLE_MAPS_API_KEY`, and this session has no
explicit authorization for paid requests. Google coverage, success/failure cases,
coordinate distances and positional improvements are therefore **unknown**.
Mocked tests verify implementation behavior, not Google availability or accuracy.

## Frozen experiment

[selected_addresses.csv](selected_addresses.csv) reuses the original independently
verified 36-address sample and its source URLs. Selection is computed from the
existing saved results, not Google responses:

- All seven Census/SanGIS disagreements above 100 meters: IDs 15, 16, 17, 23,
  25, 32 and 34.
- All five addresses neither provider uniquely resolves: IDs 01, 19, 24, 26, 30.
- 9500 Gilman Dr is already ID 01, so it is not submitted twice.

The deduplicated sample is **12 addresses**. [plan.json](plan.json) records the
not-run state, credential-presence Boolean and cost estimate. Existing free
provider coordinates and every valid SanGIS candidate remain in the original
benchmark artifacts. They are not re-queried by the Google runner.

For 9500 Gilman Dr, Census remains no-match and SanGIS has two distinct valid
locations (Structure and Centroid). Google has **not** adjudicated them. A future
authorized run compares Google against both candidates without choosing the
nearest automatically.

## Adapter and review behavior

`agent/google_geocoding.py` implements the official
[Geocoding API v3](https://developers.google.com/maps/documentation/geocoding/guides-v3/requests-geocoding).
The parser checks street number, full route/suffix/pre/post-direction, country,
state, supported locality, supplied ZIP and finite in-coverage WGS84 geometry.
It checks all results. Conflicting addresses are rejected; exact duplicate
results are deduplicated, while distinct candidates remain explicit choices.

The parser retains `formatted_address`, components, `place_id`, `partial_match`
and `location_type` **only in transient candidate objects**. ROOFTOP is an
address-level estimate, not independent proof of a building or entrance.
RANGE_INTERPOLATED, GEOMETRIC_CENTER, APPROXIMATE and partial results require
confirmation. Quota/billing problems, denied/invalid requests, malformed output,
HTTP errors, timeouts and genuine zero-results remain distinguishable.

The isolated `SelectiveReviewGeocoder` prototype has `enabled=False` by default
and is **not installed in the production Agent**. When explicitly used in an
experiment, it compares both free providers, retains adequate single-provider
results, and reviews disagreements above 100 meters or questionable/no unique
results. Third-provider proximity never selects a free candidate. Conflicting
valid estimates are returned through the existing candidate-confirmation API.
This policy is tested with mocked Google responses and has no production flag
that silently authorizes paid HTTP.

The production Census-primary/SanGIS-on-no-match workflow remains unchanged.
Phase B deployment is deferred because its required empirical usefulness gate
has not been met. Deploying the prototype will also require account-policy review
and a durable shared quota design for multiple workers; its experimental counters
are process-local, not a fleet-wide billing control.

## Cost and live controls

The [official price list](https://developers.google.com/maps/billing-and-pricing/pricing)
lists Geocoding at **USD 5 per 1,000** in the first paid tier and a 10,000-event
monthly free usage cap. Available free allowance on this account is unknown.
The planned 12 requests have **USD 0.060 list-price exposure** before any free
allowance; the hard 15-attempt cap has **USD 0.075 list-price exposure**, excluding
taxes/account-specific terms. Pricing was checked November 2, 2026.

Safeguards:

- No network call without a key **and** explicit `--live-google` authorization.
- Fixed 12-address selection; no automatic sample expansion.
- Configurable experiment cap of 1–15 HTTP attempts, including failed attempts
  and retries; benchmark defaults to one attempt per address.
- A shared thread-safe budget, a rolling per-minute rate limit, and at most two
  attempts in the adapter. Redirect following is disabled so hidden extra HTTP
  requests cannot bypass the attempt count.
- Quota/HTTP 429 stops subsequent Google requests on that budget. OVER_DAILY_LIMIT
  remains a limit-or-billing error, not a confirmed quota-only diagnosis.
- A durable exclusive `live-run.lock` prevents repeating a paid experiment after
  restart/interruption. Inspect Cloud usage and obtain fresh authorization before
  deliberately preparing another run; the runner never removes its own marker.
- No keys, complete request URLs or arbitrary Google error messages are logged.

Configure Google Cloud quotas and billing alerts separately. A billing alert is
not a hard spending cap; a process counter is not an account-wide guarantee.

## Retention, attribution and maps

The [Geocoding policies](https://developers.google.com/maps/documentation/geocoding/policies)
require attribution and restrict storage/display. The
[service-specific terms, section 6](https://cloud.google.com/maps-platform/terms/maps-service-terms)
permit use without a corresponding map, prohibit use with a non-Google map and
describe latitude/longitude caching allowances. Applicable agreements can depend
on billing region; verify the actual account terms before enabling production.

This experiment takes a narrower approach: **no Google response, returned
address, component list, place ID or coordinate is written to disk or PostgreSQL**.
Google content exists in RAM for the run and expires at process exit. There is
no persistent cache to clean up and no permanent shared reference database.
Committed outputs contain operational status/precision categories, candidate
counts, validation reasons and aggregate distance statistics only. Per-address
Google distances are not serialized, because multiple reference distances can
reveal a coordinate. Retained summaries do not provide reusable Google locations.
Do not add raw-response fixtures from live Google content to this repository.

The current app has no map. Any later Google display must identify Google Maps
and follow applicable attribution rules. The proposed open-map manual selection
must not display Google content or derive a supposedly independent manual pin
from it; see [the bounded map plan](../../../MANUAL_LOCATION_SELECTION.md).

## Reproduction and outputs

Network-free preparation:

```powershell
.\.venv\Scripts\python.exe -m backend.verification.geocoding_benchmark.google_evaluation
```

After configuring the key privately and explicitly approving the planned cost,
the operator may invoke **one controlled live run**:

```powershell
.\.venv\Scripts\python.exe -m backend.verification.geocoding_benchmark.google_evaluation --live-google --max-requests 15
```

The flag authorizes paid requests; do not use it for a dry run. Key presence alone
does not activate Google. No credential is accepted as a command-line argument.
Recheck the linked pricing and applicable account policies before that live run;
the saved estimate is dated, not an automatically refreshed price quotation.
`plan.json` is separate from a live `summary.json`, so dry preparation cannot
overwrite completed experiment results.

An authorized run records precision counts, component-validation outcomes,
additional candidate coverage, provider errors and aggregate Google-to-Census/
Google-to-all-valid-SanGIS distances. Requested outcome codes are preserved,
including `google_corrobates_census` and `google_corrobates_sangis` (the supplied
spelling). These are proximity diagnostics after component checks, not accuracy
claims. Codes may overlap; a new Google candidate can coexist with unresolved
free-provider ambiguity. No closest candidate is accepted automatically.
Distance statistics include all component-consistent candidate pairs, including
ambiguous candidates; pair counts are not address counts. Precision counts cover
returned candidates, while unique-match rates separately identify fully resolved
addresses and distinguish evaluated from unattempted cases.

Automated verification: **305 passed, 36 opt-in database cases skipped, zero
failures** across the existing Agent suite, including the new offline cases.
The tests exercise all four precisions, mismatches, multiple/partial results,
timeouts/HTTP/quota failures, concurrent caps, rate limits, default-off behavior,
both free-provider candidates, confirmation, mixed-provider symmetric analysis,
no SQL before resolution and saved-output retention. They do not measure live
Google match rates. See [P1_2_2.md](../../../P1_2_2.md) for scope and commands.

## Recommendation pending evidence

**Keep Google disabled.** Run and review the 12-address experiment before deciding
whether a paid third provider is useful. Retain 100 meters as an experimental
review threshold; no new accuracy evidence supports changing it. A manual map is
useful for campus ambiguity but is a separate bounded frontend/API increment.
Keep Census as the historical block-address baseline while reviewing the open-
data opportunities in the [historical audit](../../../audits/2026-11-02-p1_2_2/REPORT.md).
Do not geocode historical collisions with Google.
