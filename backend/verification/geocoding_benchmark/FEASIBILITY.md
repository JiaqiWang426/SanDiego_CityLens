# Phase A feasibility — 2026-11-02

Inspected the live official Address_Points layer before constructing queries.
Archived metadata: `sangis-layer-metadata.json`. Numeric `addrnmbr` is Double;
street/direction/suffix/ZIP fields are separate strings; `addrjur=SD` means San
Diego. Placement codes: S Structure, E Entrance, C Centroid, U Unknown, R Review.
ASOURCE: S SanGIS, M Assessor Master Property Record. Native CRS is State Plane
102646/latest 2230; query geometry explicitly requested WGS84 with outSR=4326.
The metadata describes SITUS property addresses and assessor points at parcel
centroids; neither unit completeness nor rooftop/entrance accuracy is assumed.

Before application changes, eight independently sourced real addresses were
queried once per provider (16 requests); raw JSON and exact URLs are under raw/.
Selection includes one ordinary single-family residence, four apartment/community
addresses, two libraries and UCSD's mailing address. It is exploratory, not random.

- Census: six validated unique matches; two no-match; no provider errors.
- SanGIS: five validated unique matches, one ambiguous address, two no-match;
  no provider errors. Mismatched ZIP/street/jurisdiction features were excluded.
- Casa Mira View, **9800 Mira Lee Way**, adds a unique SanGIS Structure-labelled
  point where Census had no match. This is useful incremental coverage.
- **9500 Gilman Dr, La Jolla, CA 92093:** Census had no match. SanGIS returned
  seven features for number/name, of which three passed full street/ZIP/SD checks.
  Those represent **two distinct coordinates**, including Structure and Centroid
  placements. Coincident duplicates are not a third location. SanGIS therefore
  offers exact-address candidates, **not a unique automatic resolution**. User
  choice remains necessary; no alternative campus property was substituted.
- SanGIS did not find the Costa Verde/Sydney apartment addresses that Census
  resolved. Its Review-labelled Ambrosia point is not claimed as a structure.

Decision before integration: proceed with a small **Census-primary, SanGIS-only-
on-legitimate-no-match fallback**. Preserve ambiguous Census confirmation and
provider-error states. Measure a larger independently selected sample before
making any primary-provider recommendation. No application/database change or
historical geocoding run was needed for this feasibility check.
