# Seed data provenance

Synthetic data only. This folder is a read-only subset of the source repo `hrshl-demo/contoso-retail-superapp4`, branch `hrshl-demo-expert-barnacle`, at the pinned commit in `.source-commit` (`860058471a78e0d5f3c285b4be08117d81a8573b`). The kit treats it as a checkout of that repository: `--repo seed`.

| Copied | Why |
|---|---|
| `data/**` except `*.pdf`, `*.png`, `*.svg`, `*.html`, `*.py` | The releases (`retail-frontier-v1`, `retail4-v1`), the banking model and the journeys that bronze, silver and gold read |
| `docs/Frontier-Retail-Strategy-PRD-Architecture-Handover.md` | The kit reads its laws, journeys and authority matrix as tables |
| `app/data_xray/projection.py`, `app/data_xray/contracts.py`, `app/frontier/contracts.py`, `app/frontier/data.py` | The silver conformance tests cite these files line by line |

Rules:

- Never edit anything here. The kit never writes into it, and the pin makes drift visible.
- `.source-commit` lets the kit identify this folder even though it sits inside another Git repository.
- To refresh, re-copy the same set from a newer commit of the source repo, update `.source-commit` and `REPO_COMMIT` in `kit/contoso_onto/__init__.py`, and run the full kit suite.
- `tools/pii_guard.py` scans this folder on every pull request.
