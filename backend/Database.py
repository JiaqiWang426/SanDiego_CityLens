'''
CityLens users should not have to upload CSV files themselves. 
The platform should maintain public data proactively, 
rather than fetching it from various government websites every
 time a user submits a query. README.md

Therefore, an independent data pipeline is required:
Raw Data Sources

Get It Done · Traffic Collisions · GTFS · Development Permits

ETL Pipeline

Download · Clean · Normalize · Deduplicate · Geocode

PostgreSQL + PostGIS

Structured records · Spatial indexes · Historical data

Agent's Read-only Query Tools

Why recommend PostGIS?

Because many of the questions posed to CityLens rely on geospatial
 computation—for example:

"Find all pothole complaints within 800 meters of a specific
 apartment complex over the past year."

PostGIS enables efficient execution of such queries using
 spatial indexes, eliminating the need for Python to iterate through the entire dataset for every request.

This also involves addressing issues such as duplicate
complaints, date formatting, missing data, and source updates.
This is particularly relevant for "Get It Done" data:
a single complaint does not necessarily equate to a distinct
, real-world issue, and a status of "Closed" does not always mean the problem has been resolved.

For an MVP, PostgreSQL + PostGIS is sufficient.
 There is no need to introduce vector or distributed databases
   at this early stage simply to add technical complexity.
'''
