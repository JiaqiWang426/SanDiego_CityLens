# Bounded geocoding pilot

This directory records tool/permission preflight, a maximum-25-record Census
pilot, and independent geographic diagnostics. Credentials and full database
connection strings must not be written here. [REPORT.md](REPORT.md) distinguishes
provider status from independent validation and lists executed writes/requests.

Reproduce the CSV, summary and map points offline from the repository root:

```powershell
.\.venv\Scripts\python.exe -m backend.scripts.geocoding_pilot validate
```

This reads saved artifacts only. Do not delete `RUN_STARTED.json` or rerun the
worker CLI: the authorized 25-record pilot is complete.
