# Verigence Analytics

Independent Verigence analytics service.

## Business Analytics source of truth

The target business-reporting state is baselined in:

`docs/VERIGENCE_BUSINESS_ANALYTICS_TO_BE_BASELINE_v1.0.md`

Current sample-data gaps do not reduce that target. Phase 1 uses the data already available through the controlled Analytics snapshot; DI/Audit Core changes are outside this repository and require separate approval.

## Runtime boundary

- Railway service: `analytics` in the shared Verigence Railway DEV project.
- Same Neon database as Audit Core, separate `analytics.*` schema.
- Analytics never writes to `auditcore.*`.
- Report requests read only `analytics.*`.
- Audit Core data is copied only by a controlled dump.
- V1 dump trigger is GitHub Actions `Refresh Analytics Dump` (`workflow_dispatch`).
- There is no real-time sync. The same dump command can later be scheduled without changing report APIs.
- Phase 1 implementation changes are confined to this repository.

## Local checks

```bash
python -m pip install -e '.[test,lint]'
ruff check src tests migrations
pytest -q
```

## Manual dump

With the Analytics Railway variables available in the environment:

```bash
python -m analytics.dump_cli --tenant <tenant-id>
```

Reports always use the latest `COMPLETED` dump and expose `data_as_of`.
