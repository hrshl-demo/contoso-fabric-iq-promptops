data/releases/ - APPROVED SEED RELEASES ONLY

This directory is intentionally EMPTY after Phase 0.

Phase 0 provisions infrastructure and an honest application shell. It does not
author customer data, products, documents or knowledge. There is deliberately
no release.json, no personas.json and no placeholder file with empty arrays
here, because an empty structure that looks like a release is a lie the seed
gate cannot distinguish from a real one.

Phase 1 authors the seed with the retained gpt-5.4-buildagent deployment,
freezes it, obtains explicit human approval of the exact content manifest and
commits the result as:

    data/releases/retail4-v1/release.json
    data/releases/retail4-v1/personas.json
    data/releases/retail4-v1/products.json
    data/releases/retail4-v1/accounts.json
    data/releases/retail4-v1/cards.json
    data/releases/retail4-v1/deposits.json
    data/releases/retail4-v1/transactions.json
    data/releases/retail4-v1/goals.json
    data/releases/retail4-v1/preferences.json
    data/releases/retail4-v1/consents.json
    data/releases/retail4-v1/documents.json
    data/releases/retail4-v1/social-profiles.json
    data/releases/retail4-v1/documents/      (asset directory)
    data/releases/retail4-v1/knowledge/      (asset directory)
    data/releases/retail4-v1/social/         (asset directory)
    data/releases/retail4-v1/qa/             (asset directory)

TWO DIFFERENT HASHES - do not confuse them
------------------------------------------
1. approval.contentManifestSha256
   SHA-256 of the canonical UTF-8, sorted-key, compact JSON of the `files`
   object INSIDE release.json. `files` excludes release.json itself. This is
   the digest a human reviewer approves.

2. seed.expectedManifestSha256 (deployment configuration, NOT in release.json)
   SHA-256 of the finalised release.json BYTES. It is recorded in the operator
   deployment configuration, outside the release, so the release never contains
   a hash of itself.

Until an approved release exists and both hashes verify, the application runs
in `phase0-shell` mode: it reports `seed_required` for demo endpoints and
prominently discloses that no approved seed is present. A normal rebuild never
calls the authoring model to create or repair this data.
