## Prompt(s) behind this change
<!-- id and version, for example `ontology.enrich` 1.1.0. A change to a prompt body needs a version bump and a CHANGELOG line. -->

## What changed
- [ ] Ontology (spec in `kit/model/`, generated parts in `fabric/`)
- [ ] Report(s) (`fabric/**/*.Report`)
- [ ] Prompt(s) only
- [ ] Pipeline / tooling

## Evidence
<!-- CI run, preview output, report PNGs (before and after), golden-question results -->

## Checklist
- [ ] `python -m tools.check` passes locally
- [ ] Prompt versions bumped and `CHANGELOG.md` updated
- [ ] `manifest.lock.json` refreshed (`python -m tools.manifest --write`)
- [ ] Preview reviewed; nothing deployed from this branch
- [ ] No secrets, no real personal data, no tenant-wide settings changed
- [ ] No billable action was taken (capacity, VM, Databricks, storage)
