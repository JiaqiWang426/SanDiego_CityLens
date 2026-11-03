# Bounded manual-location selection plan

P1.2.2 design only. The current plain JavaScript frontend has candidate dropdowns
and coordinate-entry mode, but no map, tile provider or click-to-pin API action.
Implementing a useful map also requires a typed manual-selection confirmation
transition, provenance checks and browser tests. It is a separate bounded
increment rather than an unrelated frontend framework change.

## Proposed interaction

1. After address confirmation, show existing Census/SanGIS candidates in a small
   map panel, using the same server-issued IDs as the dropdown. Show placement
   labels and the current analysis radius; do not preselect a candidate.
2. Let the user select an existing candidate or enter manual mode, click a point,
   and drag its marker. Update a clearly labelled provisional latitude/longitude
   and radius circle. Do not run analysis or reverse geocoding on movement.
3. Require a separate **Confirm selected location** action for every unresolved
   location. Mark a manual pin `user_selected`, distinct from provider-confirmed
   addresses; do not claim the chosen address exists at that point.
4. Validate finite coordinates, existing admission envelope and conversation
   ID/revision server-side. Preserve the saved analysis plan/filters. Resume the
   current LangGraph only after all locations are resolved or explicitly selected.
5. Keep numeric coordinate entry and keyboard-accessible dropdowns available.

Add one typed `select_manual_locations` chat action to the existing state machine,
accepting only pending location IDs and explicit confirmed coordinates. Reject
stale revisions, unknown IDs and incomplete two-location submissions. Do not
create an independent conversation workflow or a database table.

## Map stack and provider separation

Use a pinned stable [Leaflet release](https://leafletjs.com/reference-1.9.4.html)
with locally served JS/CSS, one tile layer, markers and a radius circle. Add no
frontend framework, build system, routing system or automatic address lookup.
Make tile URL/attribution configurable. For limited interactive development,
follow the [OpenStreetMap tile policy](https://operations.osmfoundation.org/policies/tiles/):
visible attribution, normal browser Referer/caching, no bulk/offline tile fetches.
Select a suitable tile provider before production volume; public OSM tile service
has no availability guarantee. No tiles were requested for this design.

This open-map panel must accept **only independently sourced Census/SanGIS
candidates and independently user-selected pins**. Enforce provider provenance
before rendering, not merely a hidden marker style. Do not plot Google positions,
viewports, returned addresses or derived markers/circles on a non-Google map.
Do not silently turn Google coordinates into a manual pin to bypass that rule.

If Google review is later activated, keep its content in a separate attributed
text-only confirmation mode with no open map displayed in that mode. A Google
map alternative requires a separate licensing and cost decision. The applicable
[Google service terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)
and [attribution policies](https://developers.google.com/maps/documentation/geocoding/policies)
must be reviewed before shipping that mode.

## Implementation boundary and acceptance checks

Expected files: existing `frontend/index.html`, `app.js`, `styles.css`, chat
contracts/controller and their current test modules, plus vendored map assets.
No SQL/tool/evidence arithmetic or historical geocoding changes are needed.
Extend resolved-location provenance additively to identify `user_selected` pins.

Tests must cover map/dropdown synchronization, provisional movement without SQL,
explicit confirmation, stale/unknown selections, envelope boundaries, cancelled
flows, identical two-location analytical filters, provider-provenance rejection
and keyboard/manual-coordinate use. Complete a real browser check for marker
placement, radius scaling, attribution and mobile layout. These checks and the
map implementation have not been performed in P1.2.2.
