"""Portal-seed preservation and opt-in namespaces."""
import re
from pathlib import Path

import pytest

from contoso_onto import oac, spec as S


@pytest.fixture(scope="module")
def sp(model):
    return S.make_spec(model, "core")


def make_seed(tmp_path, with_metrics=True):
    seed = tmp_path / "seed"
    (seed / "tables").mkdir(parents=True)
    (seed / "metrics").mkdir()
    (seed / "expressions.tmdl").write_text("expression 'DirectLake - ContosoBankLH' =\n\t\tlet\n\t\t\tx = 1\n\t\tin\n\t\t\tx\n", encoding="utf-8")
    (seed / "tables" / "Account.tmdl").write_text("table Account\n\tpartition Account = entity\n\t\tmode: directLake\n\t\tsource\n\t\t\tentityName: account\n"
                                                  "\t\t\texpressionSource: 'DirectLake - ContosoBankLH'\n\n\t\tannotation ONT_ItemId = 123\n", encoding="utf-8")
    (seed / "model.tmdl").write_text("model Model\n\nref table Account\n\nref entity Account\n\nref namespace default\n\nref metric TotalBalance\n", encoding="utf-8")
    if with_metrics:
        (seed / "metrics" / "TotalBalance.tmdl").write_text("metric TotalBalance\n\tentity: Account\n\tsourceModel: abc\n\tsourceMeasure: 'Total Posted Balance (INR)'\n", encoding="utf-8")
    (seed / "entities").mkdir()
    (seed / "entities" / "Account.tmdl").write_text("entity Account\n\tkeyProperty: AccountId\n", encoding="utf-8")        # owned: must NOT be carried over
    return seed


def test_seed_keeps_portal_authored_parts_and_refs(sp, tmp_path):
    seed = oac.load_seed(str(make_seed(tmp_path)))
    assert set(seed.extra_parts) == {"metrics/TotalBalance.tmdl"} and seed.extra_refs == ["ref metric TotalBalance"]
    ont = oac.build_ontology(sp, oac.make_ctx(), seed)
    assert ont["metrics/TotalBalance.tmdl"].startswith("metric TotalBalance")
    assert "ref metric TotalBalance" in ont["model.tmdl"] and ont["model.tmdl"].index("ref rule") < ont["model.tmdl"].index("ref metric")
    assert "keyProperty: AccountId" in ont["entities/Account.tmdl"] and "sourceModel" not in ont["entities/Account.tmdl"]    # entity regenerated from the spec
    assert oac.validate_parts(ont).ok                                                                                    # unknown kinds are tolerated, not rejected


def test_without_a_seed_nothing_extra_is_emitted(sp):
    ont = oac.build_ontology(sp, oac.make_ctx())
    assert not [p for p in ont if p.startswith("metrics/")] and "ref metric" not in ont["model.tmdl"]


def test_namespaces_follow_the_official_sample_pattern(sp):
    ont = oac.build_ontology(sp, oac.make_ctx(), namespaces=True)
    assert "entities/party#Customer.tmdl" in ont and "entities/Customer.tmdl" not in ont
    assert ont["entities/party#Customer.tmdl"].splitlines()[1] == "entity party#Customer" or "entity party#Customer\n" in ont["entities/party#Customer.tmdl"]
    assert "ref entity party#Customer" in ont["model.tmdl"] and "ref namespace party" in ont["model.tmdl"] and "ref namespace default" in ont["model.tmdl"]
    assert ont["namespaces/party.tmdl"].startswith("namespace party\n\tlineageTag: ")
    assert "ref table Customer" in ont["model.tmdl"] and "tables/Customer.tmdl" in ont                                  # tables are never namespaced
    assert "backingTable: Customer" in ont["entities/party#Customer.tmdl"] and "valueColumn: Customer.CustomerId" in ont["entities/party#Customer.tmdl"]
    assert "fromEntity: holdings#Account" in ont["entityRelationships.tmdl"] and "toEntity: party#Customer" in ont["entityRelationships.tmdl"]
    assert "ruleReferencedEntity authority#RegisteredAgent" in ont["rules/NoSelfExpandingAgent.tmdl"]
    rep = oac.validate_parts(ont)
    assert rep.ok, rep.errors


def test_namespaces_cover_every_domain_exactly_once(sp):
    ont = oac.build_ontology(sp, oac.make_ctx(), namespaces=True)
    ns_files = {p[len("namespaces/"):-5] for p in ont if p.startswith("namespaces/")}
    assert ns_files == {"default", "party", "holdings", "money", "goals", "authority", "servicing", "catalogue", "policy"}
    entities = [p for p in ont if p.startswith("entities/")]
    assert len(entities) == 31 and all("#" in p for p in entities)                                                         # every core entity is namespaced
    refs = re.findall(r"^ref entity (\S+)$", ont["model.tmdl"], re.M)
    assert len(refs) == len(set(refs)) == 31


def test_default_build_is_unchanged_by_the_namespace_feature(sp):
    ont = oac.build_ontology(sp, oac.make_ctx())
    assert not [p for p in ont if "#" in p] and {p for p in ont if p.startswith("namespaces/")} == {"namespaces/default.tmdl"}


def test_namespace_names_are_validated(sp):
    import json
    bad = json.loads(json.dumps(sp))
    bad["tables"][0]["namespace"] = "Default"
    assert any(e.startswith("S070") for e in S.check_spec(bad).errors)
    bad["tables"][0]["namespace"] = "default"
    assert any(e.startswith("S070") for e in S.check_spec(bad).errors)
