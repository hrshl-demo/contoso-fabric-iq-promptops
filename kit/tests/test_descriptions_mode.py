"""The two description modes (A10): `full` (business meaning, standards sentence, architecture clause: today's text) and `compact` (business meaning and
standards sentence: v1's text, every line within 500 characters), applied the same way to the ontology entities, the semantic-model tables and the data
dictionary; and the way the command line chooses one: --descriptions, else CONTOSO_DESCRIPTIONS, else what the build state remembers, else full."""
import base64
import json

import pytest

from contoso_onto import agents, cli, oac, registry, spec as S, standards

TIERS = ("smoke", "core", "full")
COMPACT_BUDGET = 500


@pytest.fixture(scope="module", params=TIERS)
def tier(request):
    return request.param


def first_lines(parts: dict, folder: str) -> dict:
    """entity or table name -> the description line (without '/// ') of every part in `folder`."""
    out = {}
    for path, text in parts.items():
        if path.startswith(folder):
            line = text.split("\n", 1)[0]
            assert line.startswith("/// "), path
            out[path.rsplit("/", 1)[-1][:-5]] = line[4:]
    return out


# --------------------------------------------------------------------------------------------------
# the specification in each mode
# --------------------------------------------------------------------------------------------------
def test_full_is_the_default_and_the_explicit_full_is_the_same_specification(model, tier):
    assert S.make_spec(model, tier) == S.make_spec(model, tier, "full") and S.make_spec(model, tier)["descriptionsMode"] == "full"
    assert S.DEFAULT_DESCRIPTIONS == "full" and S.DESCRIPTION_MODES == ("full", "compact")


def test_compact_is_the_business_meaning_then_the_standards_sentence_and_nothing_else(model, tier):
    notes = standards.notes(model)
    rows = {r["table"]: r for r in registry.entity_rows(model)}
    own = {t["table"]: (t.get("description") or "").rstrip() for t in model["tables"]}
    compact, full = S.make_spec(model, tier, "compact"), S.make_spec(model, tier, "full")
    assert compact["descriptionsMode"] == "compact" and [t["table"] for t in compact["tables"]] == [t["table"] for t in full["tables"]]
    for c, f in zip(compact["tables"], full["tables"]):
        assert c["description"] == " ".join(p for p in (own[c["table"]], notes[c["entity"]]) if p), c["entity"]
        assert not any(w in c["description"] for w in ("Star role:", "Lakehouse:", "Official CDM name aliases")), c["entity"]
        clause = registry.architecture_clause(rows[c["table"]])
        assert f["description"] == " ".join(p for p in (c["description"], clause) if p) and f["description"].startswith(c["description"]), c["entity"]
        assert c["description"][:200] == f["description"][:200] or len(c["description"]) < 200                  # what Copilot reads is the same in both modes


def test_every_compact_description_line_is_within_500_characters_as_it_is_written_to_the_parts(model, tier):
    """The ontology's line carries its domain tag, so this measures what goes to Fabric, not the spec text: 509 was the longest line proven live (v1's Product)."""
    sp = S.make_spec(model, tier, "compact")
    ctx = oac.make_ctx()
    ontology, semantic = oac.build_ontology(sp, ctx), oac.build_semantic_model(sp, ctx)
    entities, tables = first_lines(ontology, "entities/"), first_lines(semantic, "definition/tables/")
    assert len(entities) == sum(1 for t in sp["tables"] if t["ontology"]) and len(tables) == sum(1 for t in sp["tables"] if t["semantic"])
    for name, line in {**{f"ontology {k}": v for k, v in entities.items()}, **{f"semantic model {k}": v for k, v in tables.items()}}.items():
        assert len(line) <= COMPACT_BUDGET, (name, len(line))
    assert S.ENTITY_DESCRIPTION_MAX_COMPACT == COMPACT_BUDGET and S.check_spec(sp).ok
    assert max(len(S.entity_doc(t)) for t in sp["tables"]) == max(len(v) for v in entities.values())                # entity_doc is what the ontology writes


def test_full_lines_are_longer_than_anything_fabric_has_accepted_and_the_modes_say_so(model):
    stats = S.description_stats(model, "core", "full")
    assert stats["mode"] == "full" and stats["budget"] == {"full": 800, "compact": 500} and stats["longestAcceptedLive"] == S.PROVEN_LIVE_ENTITY_LINE == 509
    assert stats["longestLine"]["full"]["characters"] > 509 > stats["longestLine"]["compact"]["characters"] and stats["longestLine"]["compact"]["characters"] <= 500
    assert stats["longestLine"]["full"]["entity"] and stats["longestLine"]["compact"]["entity"]


def test_the_two_modes_differ_in_the_descriptions_only(model, tier):
    def stripped(sp: dict) -> dict:
        sp = json.loads(json.dumps(sp))
        sp.pop("descriptionsMode")
        for t in sp["tables"]:
            t.pop("description")
        return sp
    full, compact = S.make_spec(model, tier, "full"), S.make_spec(model, tier, "compact")
    assert stripped(full) == stripped(compact)                                                                  # entities, properties, relationships, measures, rules: the same
    assert S.check_spec(full).stats == S.check_spec(compact).stats


def test_the_entity_budget_belongs_to_the_mode_and_is_measured_on_the_line_that_is_written(model):
    sp = S.make_spec(model, "core", "compact")
    t = sp["tables"][0]
    tag = len(oac.ontology_doc({**t, "description": ""}))                                                         # the "[Domain] " tag the ontology puts in front
    assert tag > 0 and t["ontology"]
    t["description"] = "x" * (COMPACT_BUDGET - tag)
    assert S.check_spec(sp).ok                                                                                    # the line is exactly 500 characters
    t["description"] += "x"
    assert [e.split(":")[0] for e in S.check_spec(sp).errors] == ["S073"] and "compact mode" in S.check_spec(sp).errors[0]
    full = S.make_spec(model, "core", "full")
    full["tables"][0]["description"] = "x" * 700
    assert S.check_spec(full).ok                                                                                  # 700 is inside the full budget of 800, outside the compact one
    full["tables"][0]["description"] = "x" * (S.ENTITY_DESCRIPTION_MAX - tag + 1)
    assert [e.split(":")[0] for e in S.check_spec(full).errors] == ["S073"] and "full mode" in S.check_spec(full).errors[0]


def test_an_unknown_mode_is_refused(model):
    with pytest.raises(ValueError, match="descriptions must be one of full, compact"):
        S.make_spec(model, "core", "short")
    with pytest.raises(ValueError, match="descriptions must be one of full, compact"):
        S.check_descriptions(None)


def test_the_choice_is_the_flag_then_the_environment_then_the_state_then_full():
    r = S.resolve_descriptions
    assert r() == ("full", "default") and r(None, "", None) == ("full", "default")
    assert r(None, None, "compact") == ("compact", "the build state (descriptionsMode)")
    assert r(None, " Compact ", "full") == ("compact", "CONTOSO_DESCRIPTIONS")                                  # the environment beats the state, and is forgiving about case
    assert r("full", "compact", "compact") == ("full", "--descriptions")                                          # the flag beats both: it is how a build asks for the full text again
    for bad, where in (("tiny", "CONTOSO_DESCRIPTIONS"),):
        with pytest.raises(ValueError, match=f"{where} must be one of full, compact"):
            r(None, bad, None)
    with pytest.raises(ValueError, match="the build state \\(descriptionsMode\\) must be one of full, compact"):
        r(None, None, "tiny")


# --------------------------------------------------------------------------------------------------
# the data dictionary and the agent texts follow the mode
# --------------------------------------------------------------------------------------------------
def test_the_data_dictionary_shows_the_compact_text_and_keeps_the_architecture_as_data(model, tier):
    full_dd, compact_dd = (agents.data_dictionary(S.make_spec(model, tier, m)) for m in ("full", "compact"))
    assert full_dd["descriptionsMode"] == "full" and compact_dd["descriptionsMode"] == "compact"
    for f, c in zip(full_dd["entities"], compact_dd["entities"]):
        assert c["description"] != f["description"] and f["description"].startswith(c["description"]) and "Star role:" not in c["description"] and "Star role:" in f["description"]
        for key in ("kind", "grain", "silver", "bronzeFiles", "profileEntity", "extension", "family", "cdmReference", "bianDomains", "renamedFrom", "table", "columns"):
            assert c[key] == f[key], (c["table"], key)                                                           # the architecture facts stay in the dictionary
    md_full, md_compact = agents.data_dictionary_md(full_dd), agents.data_dictionary_md(compact_dd)
    assert "compact form" in md_compact and "compact form" not in md_full
    assert md_compact.count("**Star role:**") == md_full.count("**Star role:**") == len(full_dd["entities"])


def test_the_data_agent_instructions_do_not_promise_the_clause_in_the_compact_mode(model):
    full, compact = (agents.data_agent_instructions(S.make_spec(model, "core", m)) for m in ("full", "compact"))
    assert "then their star role and lakehouse lineage" in full and "star role and lineage are in the data dictionary" not in full
    assert "then their star role and lakehouse lineage" not in compact and "(their star role and lineage are in the data dictionary)" in compact
    assert "Entity descriptions carry their BIAN 13 and Microsoft CDM alignment" in compact and "no verified BIAN mapping (" in compact       # the standards block is intact


# --------------------------------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------------------------------
@pytest.fixture()
def seen(monkeypatch):
    """Replace the commands that take --descriptions by recorders, so that only the choice is under test."""
    calls = []

    def recorder(name):
        def fn(a):
            calls.append({"cmd": name, "mode": a.descriptions, "source": a.descriptions_source})
            return 0
        return fn
    for name in ("build", "plan", "deploy", "up", "preflight", "agents", "verify"):
        monkeypatch.setattr(cli, f"cmd_{name}", recorder(name))
    monkeypatch.delenv("CONTOSO_DESCRIPTIONS", raising=False)
    return calls


@pytest.mark.parametrize("cmd", ["build", "plan", "deploy", "up", "preflight", "agents", "verify"])
def test_every_command_that_writes_descriptions_takes_the_option(cmd, seen, tmp_path):
    assert cli.main([cmd, "--out", str(tmp_path)]) == 0 and seen[-1] == {"cmd": cmd, "mode": "full", "source": "default"}
    assert cli.main([cmd, "--descriptions", "compact", "--out", str(tmp_path)]) == 0 and seen[-1] == {"cmd": cmd, "mode": "compact", "source": "--descriptions"}


def test_the_option_environment_and_state_are_resolved_in_that_order(seen, tmp_path, monkeypatch):
    out = ["--out", str(tmp_path)]
    (tmp_path / ".state.json").write_text(json.dumps({"descriptionsMode": "compact"}))                          # an earlier build fell back
    assert cli.main(["up", *out]) == 0 and seen[-1]["mode"] == "compact" and seen[-1]["source"].startswith("the build state")
    assert cli.main(["up", "--descriptions", "full", *out]) == 0 and seen[-1] == {"cmd": "up", "mode": "full", "source": "--descriptions"}
    monkeypatch.setenv("CONTOSO_DESCRIPTIONS", "full")
    assert cli.main(["up", *out]) == 0 and seen[-1] == {"cmd": "up", "mode": "full", "source": "CONTOSO_DESCRIPTIONS"}      # an explicit environment beats the remembered mode
    monkeypatch.setenv("CONTOSO_DESCRIPTIONS", "compact")
    assert cli.main(["up", "--descriptions", "full", *out]) == 0 and seen[-1]["mode"] == "full"                  # the flag beats the environment
    n = len(seen)
    monkeypatch.setenv("CONTOSO_DESCRIPTIONS", "tiny")
    assert cli.main(["up", *out]) == 2 and len(seen) == n                                                         # a bad value stops before anything runs


def test_a_bad_flag_is_refused_by_the_parser(seen, tmp_path):
    with pytest.raises(SystemExit) as stop:
        cli.main(["up", "--descriptions", "tiny", "--out", str(tmp_path)])
    assert stop.value.code == 2 and not seen


def test_a_bad_environment_value_is_reported_before_anything_runs(seen, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTOSO_DESCRIPTIONS", "tiny")
    assert cli.main(["preflight", "--out", str(tmp_path)]) == 2 and not seen
    assert "CONTOSO_DESCRIPTIONS must be one of full, compact (got 'tiny')" in capsys.readouterr().out


def test_commands_without_descriptions_do_not_take_the_option(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["diagnose", "--descriptions", "compact", "--out", str(tmp_path)])


# --------------------------------------------------------------------------------------------------
# the commands themselves (offline)
# --------------------------------------------------------------------------------------------------
def test_plan_reports_the_mode_the_longest_lines_and_what_a_refusal_does(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CONTOSO_DESCRIPTIONS", raising=False)
    assert cli.main(["plan", "--offline", "--out", str(tmp_path)]) == 0
    plan = json.loads((tmp_path / "plan.json").read_text())
    d = plan["descriptions"]
    assert d["mode"] == "full" and d["source"] == "default" and d["longestAcceptedLive"] == 509 and d["tier"] == "core"
    assert d["longestLine"]["full"]["characters"] > 509 > d["longestLine"]["compact"]["characters"]
    step = next(s for s in plan["steps"] if s.startswith("ontology and semantic model:"))
    assert "full mode" in step and "HTTP 400" in step and "retries once in compact mode" in step
    assert cli.main(["plan", "--offline", "--descriptions", "compact", "--out", str(tmp_path)]) == 0
    d = json.loads((tmp_path / "plan.json").read_text())["descriptions"]
    assert d["mode"] == "compact" and d["source"] == "--descriptions"


def test_a_deploy_dry_run_builds_and_reports_the_chosen_mode(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CONTOSO_DESCRIPTIONS", raising=False)
    assert cli.main(["deploy", "--descriptions", "compact", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "descriptions: compact (--descriptions)" in out and "descriptions=compact" in out and "DRY RUN" in out and "errors=0" in out
    monkeypatch.setenv("CONTOSO_DESCRIPTIONS", "compact")
    assert cli.main(["deploy", "--out", str(tmp_path)]) == 0
    assert "descriptions: compact (CONTOSO_DESCRIPTIONS)" in capsys.readouterr().out


def test_a_compact_build_writes_compact_descriptions_to_every_part_and_the_agent_pack(model, tables, tmp_path):
    full_out, compact_out = tmp_path / "full", tmp_path / "compact"
    assert cli.build_tier(model, "core", tables, full_out) == 0
    assert cli.build_tier(model, "core", tables, compact_out, descriptions="compact") == 0
    for folder, prefix in (("ontology", "entities/"), ("semantic_model", "definition/tables/")):
        files = lambda root: {str(p.relative_to(root / "core" / folder)).replace("\\", "/"): p.read_text(encoding="utf-8") for p in (root / "core" / folder).rglob("*") if p.is_file()}  # noqa: E731
        f, c = files(full_out), files(compact_out)
        assert set(f) == set(c)
        for path in f:
            if path.startswith(prefix):
                assert f[path].split("\n", 1)[1] == c[path].split("\n", 1)[1], path                               # only the description line differs
                assert len(c[path].split("\n", 1)[0]) - 4 <= COMPACT_BUDGET and c[path].split("\n", 1)[0] != f[path].split("\n", 1)[0], path
            else:
                assert f[path] == c[path], path
    for root, mode in ((full_out, "full"), (compact_out, "compact")):                                            # the payload that is sent carries the same text
        payload = json.loads((root / "core" / "payloads" / "ontology.create.json").read_text())
        lines = [base64.b64decode(p["payload"]).decode().split("\n", 1)[0] for p in payload["definition"]["parts"] if p["path"].startswith("entities/")]
        assert (max(map(len, lines)) - 4 <= COMPACT_BUDGET) == (mode == "compact")
        dd = json.loads((root / "core" / "agents" / "dictionary" / "data_dictionary.json").read_text())
        assert dd["descriptionsMode"] == mode
        brief = (root / "core" / "agents" / "grounding_brief.md").read_text(encoding="utf-8")
        assert ("Star role:" in brief) == (mode == "full")                                                         # the grounding brief repeats the entity descriptions
