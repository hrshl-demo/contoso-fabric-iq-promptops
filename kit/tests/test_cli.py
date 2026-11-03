"""End-to-end CLI behaviour: offline build, dry-run defaults, scoring of collected answers."""
import json

import pytest

from contoso_onto import cli


def run(*args):
    return cli.main([str(a) for a in args])


def test_build_writes_gold_both_tiers_agent_pack_and_eval(built_out):
    assert len(list((built_out / "gold").glob("*.parquet"))) == 51
    manifest = json.loads((built_out / "manifest.json").read_text())
    assert manifest["repoHead"].startswith("860058471")
    assert {p.stem for p in (built_out / "gold").glob("*.parquet")} == set(manifest["tables"])            # Parquet files carry the physical names
    assert "dim_account.parquet" in {p.name for p in (built_out / "gold").glob("*.parquet")} and not (built_out / "gold" / "account.parquet").exists()
    for tier in ("core", "full"):
        for rel in ("ontology/model.tmdl", "ontology/entities/Account.tmdl", "semantic_model/definition/model.tmdl", "payloads/ontology.create.json",
                    "agents/data_agent/instructions.md", "agents/voice/voice_profile.json", "agents/foundry_iq/ontology_knowledge.json",
                    "agents/mcp/mcp_configs.json", "agents/dictionary/data_dictionary.md", "agents/grounding_brief.md"):
            assert (built_out / tier / rel).exists(), (tier, rel)
    exp = json.loads((built_out / "eval" / "golden_expected.json").read_text())
    assert len(exp) == 29 and not [g for g in exp if "error" in g]
    payload = json.loads((built_out / "core" / "payloads" / "ontology.create.json").read_text())
    assert payload["displayName"] == "ContosoBankOntology" and len(payload["definition"]["parts"]) == 92
    for rel in ("bronze/manifest.json", "silver/manifest.json", "silver/silver_entity_catalog.parquet", "bronze/bronze_file_manifest.parquet"):         # the other two layers are written beside gold
        assert (built_out / rel).exists(), rel
    assert any((built_out / "bronze" / "files").rglob("*.json")) and not any(p.suffix in (".pdf", ".png", ".svg", ".md") for p in (built_out / "bronze" / "files").rglob("*"))


def test_agent_instructions_encode_the_hard_rules(built_out):
    text = (built_out / "core" / "agents" / "data_agent" / "instructions.md").read_text()
    assert len(text) < 8000
    for needle in ("retail-frontier-v1", "rhea~legacy", "earmark", "Support group by in GQL", "NeverClaim", "DeniedScopes", "read-only"):
        assert needle in text


def test_voice_profile_marks_pii_and_speakable_units(built_out):
    vp = json.loads((built_out / "core" / "agents" / "voice" / "voice_profile.json").read_text())
    props = {(p["entity"], p["property"]): p for p in vp["properties"]}
    assert props[("Account", "PostedBalanceInr")]["kind"] == "money_inr" and "lakh" in props[("Account", "PostedBalanceInr")]["speak"]
    assert props[("Card", "MaskedNumber")]["neverSpeakInFull"] and props[("Customer", "PhoneMasked")]["pii"]
    assert props[("Account", "AccountId")]["kind"] == "identifier"


def test_foundry_iq_pack_keeps_the_extractive_kb_untouched(built_out):
    pack = json.loads((built_out / "core" / "agents" / "foundry_iq" / "ontology_knowledge.json").read_text())
    assert pack["knowledge_source"]["kind"] == "fabricOntology" and pack["knowledge_base"]["retrievalReasoningEffort"] == {"kind": "low"}
    assert pack["toolbox_connection"]["structuredInputs"][0]["header"] == "x-ms-query-source-authorization"
    assert any("extractive" in n for n in pack["notes"])


def test_data_dictionary_flags_restricted_columns(built_out):
    dd = json.loads((built_out / "core" / "agents" / "dictionary" / "data_dictionary.json").read_text())
    cust = next(e for e in dd["entities"] if e["logicalId"] == "customer")
    assert cust["table"] == "dim_customer" and cust["entity"] == "Customer" and cust["kind"] == "dimension"
    dob = next(c for c in cust["columns"] if c["column"] == "date_of_birth")
    assert dob["inOntology"] is False and dob["pii"] is True


def test_deploy_dry_run_sends_nothing(built_out, capsys):
    assert run("deploy", "--out", built_out) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "semantic model 'ContosoBankModel'" in out and "ontology 'ContosoBankOntology'" in out
    assert "ont-annotations=yes" in out                                                           # the graph annotations are on by default


def test_deploy_dry_run_can_switch_the_graph_annotations_off(built_out, capsys):
    assert run("deploy", "--no-ont-annotations", "--out", built_out) == 0
    assert "ont-annotations=no" in capsys.readouterr().out


def test_execute_commands_refuse_without_state(built_out, capsys, tmp_path):
    assert run("deploy", "--execute", "--out", tmp_path) == 1
    assert run("load", "--execute", "--out", built_out) == 1
    assert "provision --execute" in capsys.readouterr().out


def test_capacity_and_agents_default_to_dry_run(built_out, capsys):
    assert run("capacity", "suspend") == 0
    assert run("agents", "--out", built_out) == 0
    out = capsys.readouterr().out
    assert "[dry-run] az rest --method post --url https://management.azure.com/" in out and "/suspend?api-version=" in out and "DRY RUN - nothing sent" in out


def test_plan_offline_needs_no_azure(tmp_path, capsys):
    assert run("plan", "--offline", "--out", tmp_path) == 0
    plan = json.loads((tmp_path / "plan.json").read_text())
    assert plan["errors"] == []
    layers = plan["dataLayers"]                                                                    # the size of each tier's layers, from the specifications alone
    for tier in ("smoke", "core", "full"):
        assert layers[tier]["summary"] == f"{layers[tier]['bronzeFiles']} bronze files, {layers[tier]['silverTables']} silver tables, {layers[tier]['goldTables']} gold tables"
    assert layers["smoke"]["goldTables"] < layers["core"]["goldTables"] < layers["full"]["goldTables"] and layers["smoke"]["silverTables"] < layers["full"]["silverTables"]
    assert "bind to gold only" in layers["note"] and any("bronze seed files" in s for s in plan["steps"])
    assert "bronze files" in capsys.readouterr().out                                               # and it is printed, not only saved


def test_provision_refuses_a_size_other_than_f4_before_touching_azure(tmp_path, capsys):
    conf = tmp_path / "other.json"
    conf.write_text(json.dumps({"capacity": {"sku": "F8"}}))
    args = ("--config", conf, "--state-dir", tmp_path / "state", "--out", tmp_path / "out")
    assert run("provision", *args) == 2 and run("provision", *args, "--execute") == 2            # dry run and real run alike
    out = capsys.readouterr().out
    assert out.count("BAD CONFIG") == 2 and "not the F4 this demo is built for" in out and "CONTOSO_ALLOW_OTHER_SKU=1" in out
    assert not (tmp_path / "state").exists()                                                      # no ledger, no lock: nothing started


def test_verify_scores_collected_answers(built_out, capsys):
    good = {"G01": "Up to Rs 20,000 per action.", "G06": "About 10,08,300 rupees as of 2026-09-30", "G21": "Only the customer can approve it.",
            "G22": "rhea and rhea~legacy; the legacy one is superseded"}
    f = built_out / "answers_good.json"
    f.write_text(json.dumps(good))
    assert run("verify", "--out", built_out, "--answers", f) == 0
    bad = built_out / "answers_bad.json"
    bad.write_text(json.dumps({"G01": "It can move 25,000 rupees", "G21": "Anyone"}))
    assert run("verify", "--out", built_out, "--answers", bad) == 1
    assert "FAIL" in capsys.readouterr().out


def test_assess_command_reports_the_critical_findings(repo_path, tmp_path, capsys):
    assert run("assess", "--repo", repo_path, "--out", tmp_path) == 0
    res = json.loads((tmp_path / "assessment" / "assessment.json").read_text())
    ids = {f["id"]: f for f in res["findings"]}
    assert ids["A07"]["severity"] == "critical-identity" and "rhea" in ids["A07"]["detail"]
    assert any("deposits:dep-rhea-liquid-001" in " ".join(o["records"]) for o in res["moneyOverlaps"])
    assert res["unitConventions"]["productParameterUnit"].startswith("INR_major")
    assert all("qa/" not in k for k in res["profiles"])                                    # answer keys never read
    assert "approved" in ids["A12"]["detail"] and "in_review" in ids["A12"]["detail"]


def test_resolve_repo_rejects_drift_and_non_repos(tmp_path, repo_path):
    from contoso_onto.snapshot import resolve_repo
    with pytest.raises(FileNotFoundError):
        resolve_repo(str(tmp_path), False, tmp_path)
    with pytest.raises(SystemExit):
        resolve_repo(None, False, tmp_path)
    assert resolve_repo(str(repo_path), False, tmp_path) == repo_path.resolve()


# ---------------------------------------------------------------------------------------------- the staged build and --fresh
def _capture_up(monkeypatch, tmp_path, *extra, env=None):
    from contoso_onto import runner
    seen, order = {}, []
    monkeypatch.setattr(runner, "up", lambda cfg, az, **kw: (order.append("up"), seen.update(kw), 0)[2])
    monkeypatch.setattr(runner, "down", lambda cfg, az, **kw: (order.append("down"), seen.setdefault("down_rc", 0))[1])
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    rc = run("up", "--state-dir", tmp_path / "s", "--out", tmp_path / "o", *extra)
    return rc, seen, order


def test_up_defaults_are_the_fifteen_minute_wait_a_minute_poll_and_the_smoke_questions(monkeypatch, tmp_path):
    rc, seen, order = _capture_up(monkeypatch, tmp_path)
    p = seen["pipeline"]
    assert rc == 0 and order == ["up"]
    assert (p.until, p.gate_wait_min, p.gate_poll_sec, p.gate_timeout_min, p.graph, p.golden, p.golden_parallel, p.strict_golden, p.reload) == (
        None, 15.0, 60.0, 120.0, True, "smoke", 2, False, False)


def test_up_turns_the_stage_flags_into_a_pipeline(monkeypatch, tmp_path):
    rc, seen, _ = _capture_up(monkeypatch, tmp_path, "--until", "approve", "--gate-wait", "5", "--gate-poll", "30", "--gate-timeout", "60", "--no-graph",
                              "--golden", "full", "--golden-parallel", "1", "--strict-golden", "--reload")
    p = seen["pipeline"]
    assert rc == 0 and (p.until, p.gate_wait_min, p.gate_poll_sec, p.gate_timeout_min, p.graph, p.golden, p.golden_parallel, p.strict_golden, p.reload) == (
        "approve", 5.0, 30.0, 60.0, False, "full", 1, True, True)
    rc, seen, _ = _capture_up(monkeypatch, tmp_path, "--no-gate-wait")
    assert seen["pipeline"].gate_timeout_min == 0.0                                                # do not wait: stop at the gate, the previous behaviour


def test_up_stage_defaults_can_come_from_the_environment(monkeypatch, tmp_path):
    rc, seen, _ = _capture_up(monkeypatch, tmp_path, env={"CONTOSO_GATE_WAIT_MIN": "3", "CONTOSO_GATE_POLL_SEC": "20", "CONTOSO_GATE_TIMEOUT_MIN": "45", "CONTOSO_GOLDEN": "none"})
    p = seen["pipeline"]
    assert (p.gate_wait_min, p.gate_poll_sec, p.gate_timeout_min, p.golden) == (3.0, 20.0, 45.0, "none")
    with pytest.raises(SystemExit) as ex:
        _capture_up(monkeypatch, tmp_path, env={"CONTOSO_GATE_WAIT_MIN": "soon"})
    assert "CONTOSO_GATE_WAIT_MIN" in str(ex.value)


def test_up_rejects_a_stage_that_does_not_exist(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _capture_up(monkeypatch, tmp_path, "--until", "ship-it")


def test_fresh_wipes_first_then_builds_and_stops_if_the_wipe_does_not_finish(monkeypatch, tmp_path):
    rc, seen, order = _capture_up(monkeypatch, tmp_path, "--fresh")
    assert rc == 0 and order == ["down", "up"]                                                    # stage 0, then the stages
    from contoso_onto import runner
    order.clear()
    monkeypatch.setattr(runner, "down", lambda cfg, az, **kw: (order.append("down"), 5)[1])        # the wipe fails (exit 5)
    assert run("up", "--state-dir", tmp_path / "s", "--out", tmp_path / "o", "--fresh") == 5
    assert order == ["down"]                                                                       # nothing new is built on top of a half-wiped environment
