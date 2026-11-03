# Releasing

A release is **a reviewed changelog section plus one button press**. Publishing the release never needs Azure or Fabric. Deploying prod is an optional extra that stays off until the one-time setup in [SETUP.md](SETUP.md) is done.

## The four steps

1. **Prepare** (on a branch, through a pull request):

   ```powershell
   python -m tools.release prepare --version v1.0.0 --date 2026-11-04
   ```

   This turns `## Unreleased` in `CHANGELOG.md` into `## v1.0.0 — 2026-11-04` and leaves an empty `## Unreleased` on top. CI checks the structure (`python -m tools.release lint`). Merge it.
2. **Wait for CI on `main`** to go green. The release refuses a commit without a successful `ci` run.
3. **Dry run:** Actions, `release`, Run workflow, version `v1.0.0`, **Publish off**. It checks the rules, runs the full kit tests, builds the files and shows what it would publish. Nothing is created.
4. **Publish:** run it again with **Publish on**. It creates the tag `v1.0.0` on that commit and a GitHub release with the files below. With **Deploy to prod** also on, prod is deployed first and the release is published only if that succeeds (draft; needs [SETUP.md](SETUP.md)). Deploy to prod does nothing without Publish, so a dry run never touches prod.

## What a release carries

| File | What it is |
|---|---|
| `monthly-business-brief-vX.Y.Z.pdf` | The Monthly Business Brief (six pages), built from the seed data at this commit |
| `ontology-brief-vX.Y.Z.md` | Every entity, synonym and PII flag of the ontology, for review |
| `fabric-items-vX.Y.Z.zip` | The ontology and the semantic model as Fabric item definitions: exactly what a deployment sends |
| `prompts-vX.Y.Z.zip` | The prompt library with `skills.lock` and `manifest.lock.json` |
| `SHA256SUMS` | A checksum for every file above |

The release notes are the changelog section, a table of every prompt with its version and hash (from `manifest.lock.json`), the attached files, and the commit and run that built them. The two zips and the ontology brief are the same bytes on every platform (checked on Windows and Linux); the PDF is built on the runner, so the checksum in `SHA256SUMS` is the one of the attached file.

## The rules the gate enforces

- The run is on `main` and started by an approver (the repository variable `RELEASE_APPROVERS`, a comma separated list of GitHub user names; the repository owner when it is not set).
- The version looks like `v1.2.3`, the tag does not exist, and it is not older than the newest tag.
- `CHANGELOG.md` has a dated, non-empty section for it, nothing is left in `## Unreleased` (it would be missing from the notes), and the structure is valid.
- A successful `ci` run exists for this exact commit, the full kit test suite passes, and the generated Fabric items still match the spec.
- Deploy to prod only: the deployment pipelines are enabled, and the files a deployment sends (`fabric/`, `seed/`, `kit/model`, `kit/contoso_onto`, `kit/config`, `tools/cd.py`) equal those of the last successful `cd-dev` run (`python -m tools.release promotion`). Prod gets only what dev has had.

Every pull request also builds the same files as the `release-preview` artifact and prints the file list and the pending release notes in the CI job summary, so a broken release is found in the pull request, not on release day.

## Approval on this plan

Required reviewers on environments are not available on a personal private repository on the Free plan, so the approval is who may press the button (the check above) and the reviewed changelog pull request. On a plan with environment protection rules, add `environment: production` to the `publish` job and require reviewers there; nothing else changes.

## If something goes wrong

- **The gate fails:** the message says what to change; nothing was created.
- **Publish fails half way:** check Releases and Tags. Delete a half-created tag and release by hand, then run the workflow again (the gate refuses an existing tag on purpose).
- **Cost:** a dry run or a release is dominated by the full kit tests (about 12 to 15 minutes on a laptop; the first run shows the Linux time). The Free plan includes 2,000 Actions minutes a month.
