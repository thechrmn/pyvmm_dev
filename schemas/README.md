# Bundled schema: BeStMeta

`bestmeta_schema.json` is an unmodified copy of the JSON Schema published by the
Helmholtz Metadata Collaboration project BeStMeta. It is the default framework for
`align.py`, bundled so that runs are offline and reproducible.

| | |
|---|---|
| Source | https://github.com/Helmholtz-UFZ/BeStMeta (`bestmeta_schema.json`) |
| Commit | `6bf77e27fe274481c06ee9e4b52e2befdcdb36d7` (2026-10-08, "Merge pull request #24 from MadhuKantharaju/missing-enum-values") |
| SHA-256 | `01e7fbaef08ee3f46babd2edd4363e700ccccd5a726c56b72644653b6c8cdd9c` |
| Dialect | JSON Schema 2019-09 |
| License | MIT, © 2026 Helmholtz Centre for Environmental Research – UFZ (see `LICENSE`) |

The hash is recorded in every output (`schema.sha256`) and checked by the test suite.

## Updating

1. Copy the new `bestmeta_schema.json` over the old one.
2. Update the commit and SHA-256 above, and `PINNED_BESTMETA_SHA256` in `tests/test_align.py`.
3. Run `python -m unittest`. A schema change that alters enums, required fields or
   conditional rules shows up as test failures to review, not as silent drift.

To validate against a different version or framework without replacing this file, use
`--schema PATH` (and, for a framework other than BeStMeta, a matching `FIELD_MAP` in `align.py`).
