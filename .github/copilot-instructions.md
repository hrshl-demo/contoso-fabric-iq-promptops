# Copilot instructions for this repository

[AGENTS.md](../AGENTS.md) is authoritative; read it first. In short:

- **Propose, never deploy.** Work on a `copilot/<intent>` branch and open a pull request. Never push to `main`; never run the deploy, release, capacity or wipe workflows.
- **Plan first**, and preview anything that would change Fabric or Azure before asking for a go.
- **Synthetic data only.** No secrets, tokens, keys, or tenant and subscription IDs in files.
- **Prompts are versioned.** They live in `prompts/`. Change the body, bump `version`, add a `CHANGELOG.md` line, then run `python -m tools.wrappers --write` and `python -m tools.manifest --write`.
- **The ontology spec is `kit/model/`.** Files in `fabric/` are generated; never hand-edit them.
- **Before a pull request:** `python -m tools.check`.
