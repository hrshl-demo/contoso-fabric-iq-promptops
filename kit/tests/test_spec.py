"""Spec generation, ontology/semantic-model TMDL, and the checks that protect against known Fabric IQ gotchas."""
import json
from pathlib import Path

import pytest

from contoso_onto import oac, spec as S

FIXTURE = Path(__file__).parent / "fixtures" / "windfarm_sample"


@pytest.fixture(scope="module", params=["smoke", "core", "full"])
def tier(request):
    return request.param


@pytest.fixture(scope="module")
def spec_for(model):
    return lambda tier: S.make_spec(model, tier)


def test_spec_passes_all_checks(spec_for, tier):
    rep = S.check_spec(spec_for(tier))
    assert rep.ok, rep.errors
    assert rep.warnings == []


def test_core_is_a_strict_subset_of_full(spec_for):
    smoke, core, full = spec_for("smoke"), spec_for("core"), spec_for("full")
    se = {t["entity"] for t in smoke["tables"]}
    ce = {t["entity"] for t in core["tables"]}
    fe = {t["entity"] for t in full["tables"]}
    assert se < ce < fe and len(se) == 13
    assert {r["name"] for r in core["relationships"]} < {r["name"] for r in full["relationships"]}
    assert core["rules"] and len(core["rules"]) == len(full["rules"])
    stats = S.check_spec(core).stats
    assert stats["entities"] == 31 and 400 < stats["properties"] < 450


def test_ontology_hides_restricted_and_paise_but_semantic_model_keeps_them(spec_for):
    sp = spec_for("core")
    ctx = oac.make_ctx()
    ont, sm = oac.build_ontology(sp, ctx), oac.build_semantic_model(sp, ctx)
    cust_ont = ont["entities/Customer.tmdl"] + ont["tables/Customer.tmdl"]
    for hidden in ("DateOfBirth", "KycAddress", "Email"):
        assert hidden not in cust_ont
    assert "sourceColumn: date_of_birth" in sm["definition/tables/Customer.tmdl"]
    assert "PostedBalancePaise" not in ont["entities/Account.tmdl"] and "PostedBalanceInr" in ont["entities/Account.tmdl"]
    assert "posted_balance_paise" in sm["definition/tables/Account.tmdl"] and "isHidden" in sm["definition/tables/Account.tmdl"]
    assert "PolicyNumber" not in ont["entities/ProtectionPolicy.tmdl"] and "KyaKeyFingerprint" not in ont["entities/RegisteredAgent.tmdl"]


def test_calendar_is_semantic_model_only(spec_for):
    ont, sm = oac.build_ontology(spec_for("core"), oac.make_ctx()), oac.build_semantic_model(spec_for("core"), oac.make_ctx())
    assert "entities/Calendar.tmdl" not in ont and "tables/Calendar.tmdl" not in ont
    assert "definition/tables/Calendar.tmdl" in sm
    assert "Transaction Posted On Date" not in ont["entityRelationships.tmdl"]
    assert "rel_transaction_posted_on_date" in sm["definition/relationships.tmdl"]


def test_generated_tmdl_validates(spec_for, tier):
    sp = spec_for(tier)
    for parts in (oac.build_ontology(sp, oac.make_ctx()), oac.build_semantic_model(sp, oac.make_ctx())):
        rep = oac.validate_parts(parts)
        assert rep.ok, rep.errors
        assert {w.split(":")[0] for w in rep.warnings} <= {"V004"}      # only unresolved placeholders


def test_no_description_comment_on_a_relationship(spec_for, tier):
    """Live finding: Fabric rejects a '///' comment before a TOM relationship ("Property 'description' is unknown")."""
    sp = spec_for(tier)
    sm = oac.build_semantic_model(sp, oac.make_ctx())
    for parts in (sm, oac.build_ontology(sp, oac.make_ctx())):
        for path, text in parts.items():
            if path.rsplit("/", 1)[-1] == "relationships.tmdl":
                assert "///" not in text, path
    bad = dict(sm)
    bad["definition/relationships.tmdl"] = "/// not allowed\n" + sm["definition/relationships.tmdl"]
    rep = oac.validate_parts(bad)
    assert not rep.ok and any(e.startswith("V005") for e in rep.errors)


def test_validator_rejects_an_ontology_item_name_fabric_would_refuse(spec_for):
    ont = oac.build_ontology(spec_for("smoke"), oac.make_ctx())
    assert oac.validate_parts(ont).ok
    for bad_name in ("Contoso-Bank-Ontology", "1Ontology", "Contoso Bank", "A" * 90):
        bad = dict(ont)
        bad[".platform"] = ont[".platform"].replace("ContosoBankOntology", bad_name)
        rep = oac.validate_parts(bad)
        assert not rep.ok and any(e.startswith("V006") for e in rep.errors), bad_name


def test_vendored_validator_still_accepts_the_official_sample():
    parts = oac.load_dir_parts(str(FIXTURE))
    rep = oac.validate_parts(parts)
    assert rep.ok, rep.errors


def test_ontology_names_follow_the_known_constraints(spec_for, tier):
    sp = spec_for(tier)
    ont_tables = [t for t in sp["tables"] if t["ontology"]]
    types = {}
    for t in ont_tables:
        keys = [c for c in t["columns"] if c["key"]]
        assert len(keys) == 1 and keys[0]["type"] in ("string", "int")
        for c in t["columns"]:
            assert c["type"] != "decimal" and " " not in c["col"] and "," not in c["col"]
            if c["ontology"]:
                assert types.setdefault(c["prop"], c["type"]) == c["type"], c["prop"]      # same property name => same type
    names = [r["name"] for r in sp["relationships"] if r["ontology"]]
    assert len(names) == len(set(names))


def test_semantic_relationships_have_no_ambiguous_filter_paths(spec_for, tier):
    sp = spec_for(tier)
    edges = {}
    for r in sp["relationships"]:
        if r["semantic"]:
            one, many = r["to"].split(".")[0], r["from"].split(".")[0]
            edges.setdefault(one, []).append((many, r["name"]))
            edges.setdefault(many, [])
    counts, cycles = S._paths(edges)
    assert not cycles and all(v == 1 for c in counts.values() for v in c.values())


def test_ambiguity_and_cycle_detector_catch_bad_models():
    counts, cycles = S._paths({"a": [("b", "r1"), ("c", "r2")], "b": [("c", "r3")], "c": []})
    assert counts["a"]["c"] == 2                       # a->c directly and a->b->c: ambiguous
    _, cycles = S._paths({"a": [("b", "r1")], "b": [("a", "r2")]})
    assert cycles and "directed cycle" in cycles[0]


def test_check_spec_rejects_decimal_special_chars_and_type_clash(spec_for):
    sp = json.loads(json.dumps(spec_for("core")))
    acc = next(t for t in sp["tables"] if t["table"] == "account")
    acc["columns"][3]["type"] = "decimal"
    acc["columns"][4]["col"] = "bad col"
    cust = next(t for t in sp["tables"] if t["table"] == "customer")
    cust["columns"][5]["prop"] = "AccountId"            # AccountId is a string elsewhere; make it an int here
    cust["columns"][5]["type"] = "int"
    codes = {e.split(":")[0] for e in S.check_spec(sp).errors}
    assert {"S014", "S013", "S020"} <= codes


def test_labels_are_agent_friendly(spec_for):
    sp = spec_for("core")
    labels = {c["prop"]: c["label"] for t in sp["tables"] for c in t["columns"]}
    assert labels["PostedBalanceInr"] == "Posted Balance (INR)" and labels["InterestRatePct"] == "Interest Rate (%)"
    assert labels["CustomerId"] == "Customer ID" and labels["KyaVerifiedAt"] == "KYA Verified At" and labels["EmiInr"] == "EMI (INR)"


def test_rules_reference_only_ontology_objects_and_carry_the_laws(spec_for):
    sp = spec_for("core")
    names = {r["name"] for r in sp["rules"]}
    assert {"NoFigureWithoutAuthority", "NoSelfExpandingAgent", "NoSalesDuringHarm", "DefaultToFrontierRelease", "UnitsAndCurrency"} <= names
    props = {f"{t['entity']}.{c['prop']}" for t in sp["tables"] if t["ontology"] for c in t["columns"] if c["ontology"]}
    assert all(p in props for r in sp["rules"] for p in r["properties"])
    assert all(r["statement"] and "\n" not in r["statement"] for r in sp["rules"])


def test_payload_sizes_are_reasonable(spec_for, tier):
    sp = spec_for(tier)
    for parts in (oac.build_ontology(sp, oac.make_ctx()), oac.build_semantic_model(sp, oac.make_ctx())):
        assert len(json.dumps(oac.to_payload(parts))) < 1_000_000


def test_ontology_tables_name_the_dbo_schema_but_the_semantic_model_does_not(spec_for):
    sp = spec_for("smoke")
    ont, sm = oac.build_ontology(sp, oac.make_ctx()), oac.build_semantic_model(sp, oac.make_ctx())
    tables = [p for p in ont if p.startswith("tables/")]
    assert tables and all("\t\t\tschemaName: dbo\n\t\t\texpressionSource: DatabaseQuery" in ont[p] for p in tables)
    assert not any("schemaName" in text for path, text in sm.items() if path.startswith("definition/tables/"))


def test_seed_round_trip_reuses_portal_binding(spec_for, tmp_path):
    sp = spec_for("core")
    seed_dir = tmp_path / "seed"
    (seed_dir / "tables").mkdir(parents=True)
    (seed_dir / "expressions.tmdl").write_text("expression 'DirectLake - ContosoBankLH' =\n\t\tlet\n\t\t\tx = 1\n\t\tin\n\t\t\tx\n", encoding="utf-8")
    (seed_dir / "tables" / "A.tmdl").write_text("table A\n\tpartition A = entity\n\t\tmode: directLake\n\t\tsource\n\t\t\tentityName: a\n"
                                                "\t\t\texpressionSource: 'DirectLake - ContosoBankLH'\n\n\t\tannotation ONT_ItemId = 123\n", encoding="utf-8")
    ont = oac.build_ontology(sp, oac.make_ctx(), oac.load_seed(str(seed_dir)))
    assert "expressionSource: 'DirectLake - ContosoBankLH'" in ont["tables/Customer.tmdl"]
    assert "annotation ONT_ItemId = 123" in ont["tables/Customer.tmdl"]
