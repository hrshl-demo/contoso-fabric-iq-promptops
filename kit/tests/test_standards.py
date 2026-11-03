"""BIAN / Microsoft CDM alignment: derived from the source repo's registry, committed as model/standards.yaml, shown in entity descriptions."""
from pathlib import Path

import pytest
import yaml

from contoso_onto import oac, spec as S, standards as STD
from contoso_onto.gold import load_model

KIT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def model():
    return load_model(KIT / "model")


def test_committed_standards_file_matches_the_repo_registry(repo_path):
    """model/standards.yaml is generated; if the registry, the profile or ONTOLOGY_ENTITY change, regenerate it with `python -m contoso_onto standards --write`."""
    committed = yaml.safe_load((KIT / "model" / "standards.yaml").read_text(encoding="utf-8"))["standards"]
    assert committed == STD.generate(Path(repo_path))


def test_every_realised_entity_exists_in_the_model(model):
    entities = {t["entity"] for t in model["tables"]}
    realised = {e["ontology_entity"] for e in model["standards"]["entities"].values() if e["ontology_entity"]}
    assert realised and realised <= entities, sorted(realised - entities)
    assert set(STD.ONTOLOGY_ENTITY.values()) == realised


def test_the_summary_is_honest_about_coverage(model):
    ents = model["standards"]["entities"]
    assert len(ents) == 45 and sum(1 for e in ents.values() if e.get("cdm")) == 8
    assert model["standards"]["source"]["bianRelease"] == "13.0.0"
    assert "not Microsoft endorsement, BIAN certification" in model["standards"]["disclaimer"]
    # the agent-authority layer has no verified BIAN mapping and must stay unmapped, never upgraded
    for name in ("FrontierMandate", "HouseholdAccessGrant", "RegisteredAgent", "FrontierConsent", "MandateAction"):
        assert ents[name]["bian"] == [] and ents[name]["unmapped_attributes"] > 0, name
    deposit = next(b for b in ents["Deposit"]["bian"] if b["domain"] == "Term Deposit")
    assert deposit["provisional"] > 0                                                         # a partial analogy stays marked as one
    assert ents["Account"]["cdm"]["entity"] == "FHAccount" and ents["Customer"]["cdm"]["entity"] == "Contact"


def test_entity_descriptions_carry_the_alignment_and_the_caveat(model):
    sp = S.make_spec(model, "core")
    desc = {t["entity"]: t["description"] for t in sp["tables"]}
    assert "BIAN 13.0.0 Customer Product and Service Directory (custom), Savings Account (custom)" in desc["Account"]
    assert "Microsoft CDM FHAccount (semantic reference only)" in desc["Account"]
    assert "Term Deposit (custom, partly provisional)" in desc["Deposit"]
    assert "no verified BIAN mapping (custom semantics)" in desc["FrontierMandate"] and "no Microsoft CDM reference" in desc["FrontierMandate"]
    for e in ("Account", "FrontierMandate", "Consent", "Customer"):
        assert "Demo mapping, not certification. Star role: " in desc[e], e         # the caveat closes the standards sentence, the architecture clause follows
    original = next(t for t in model["tables"] if t["entity"] == "Account")["description"].rstrip()
    assert desc["Account"].startswith(original + " Standards: ") and desc["Account"] != original      # the original description is kept in front of the note


def test_an_entity_with_no_profile_entity_says_so_and_claims_no_alignment(model):
    """ScheduledObligation was once mapped to the profile's Commitment; that entity is a mandate or repayment-schedule row, so it is an extension entity."""
    sp = S.make_spec(model, "core")
    desc = {t["entity"]: t["description"] for t in sp["tables"]}
    assert "Commitment" not in STD.ONTOLOGY_ENTITY and "ScheduledObligation" not in STD.ONTOLOGY_ENTITY.values()
    for e in ("Household", "ScheduledObligation", "CrmCase", "Calendar", "Law"):
        assert STD.EXTENSION_NOTE in desc[e] and "Standards:" not in desc[e] and "Demo mapping" not in desc[e], e
    assert STD.EXTENSION_NOTE.startswith("Contoso extension entity: no CDM profile entity")
    full = {t["entity"]: t["description"] for t in S.make_spec(model, "full")["tables"]}
    extension = {t["entity"] for t in model["tables"]} - set(STD.ONTOLOGY_ENTITY.values())
    assert len(extension) == 33 and len(full) - len(extension) == 18                      # 24 silver extension entities + 9 reference tables; 18 realise a profile entity
    assert all(STD.EXTENSION_NOTE in full[e] for e in extension)
    assert not any(STD.EXTENSION_NOTE in full[e] for e in set(full) - extension)


def test_a_model_without_standards_is_unchanged():
    assert STD.notes({}) == {}
    assert STD.notes({"standards": {}}) == {}


def test_the_agent_is_told_not_to_claim_certification_only_when_the_alignment_is_there(model):
    from contoso_onto import agents
    text = agents.data_agent_instructions(S.make_spec(model, "core"))
    assert "STANDARDS" in text and "never say the data or the ontology is BIAN-compliant, BIAN-certified or endorsed by Microsoft" in text
    assert text.index("STANDARDS") < text.index("PRIVACY")
    bare = {k: v for k, v in model.items() if k != "standards"}
    assert "STANDARDS" not in agents.data_agent_instructions(S.make_spec(bare, "core"))


def test_alignment_notes_reach_the_ontology_and_the_semantic_model_and_still_validate(model):
    sp = S.make_spec(model, "core")
    ctx = oac.make_ctx()
    ont, sm = oac.build_ontology(sp, ctx), oac.build_semantic_model(sp, ctx)
    assert "BIAN 13.0.0" in ont["entities/Account.tmdl"] and "BIAN 13.0.0" in sm["definition/tables/Account.tmdl"]
    for parts in (ont, sm):
        rep = oac.validate_parts(parts)
        assert rep.ok, rep.errors
