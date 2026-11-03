# Vendored kit

`kit/` is the `contoso_onto` kit, copied from the private repo `hrshl-demo/contoso-bank-ontology-demo`, branch `medallion-cdm`, commit `465d55c9b3cf541e52bb90d84d53b0dcc94780d0`. It was exported with `git archive`, so it carries no history. From this commit on, this repo owns the copy.

## Local changes

| Change | Why |
|---|---|
| `contoso_onto/snapshot.py`: `git_head()` reports a checkout's HEAD only when the folder is that checkout's top level, and otherwise reads a `.source-commit` marker | The seed is vendored inside this repository. Without this the kit would report this repository's HEAD as the source commit and refuse to build |
| `tests/conftest.py`, `pytest.ini`: tests in `test_runner.py`, `test_layer_load.py` and `test_scripts.py` carry a `slow` marker | The full suite takes about 15 minutes; `-m "not slow"` takes about 1.5 minutes. CI runs the full suite when the engine changes and on release |
| `tests/test_source_and_auth.py`: one test for the marker behaviour | Covers the `git_head()` change |

Everything else is unchanged. The ontology spec in `kit/model/` is now edited through prompts and pull requests.

## Updating from upstream

There is no automatic sync. To take a newer engine, export the new commit the same way, diff it against this folder, re-apply the three changes above, run the full suite, and record the new commit here.
