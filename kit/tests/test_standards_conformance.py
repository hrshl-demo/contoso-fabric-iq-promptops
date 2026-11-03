"""Standards checks of the ontology against the CDM profile and the BIAN registry it cites. (a) Every profile relationship between two entities the ontology
realises exists in the ontology, or is listed in model/standards_exceptions.yaml with a reason the silver spec backs. (b) Every ontology key is built from the
business key of its silver table, or the exception says why not. (c) Every entity description that realises a profile entity lists the BIAN domains of
model/standards.yaml with the registry's own status words, never an upgrade. (d) No text written for these standards claims more than the demo mapping: the
three words a claim would use do not appear, and certification appears only in "not ... certification"."""
import re
from pathlib import Path

import pytest

from contoso_onto import spec as S, standards
from contoso_onto.gold import TPL_RE, expand_columns

KIT = Path(__file__).resolve().parent.parent
ARROW = " -> "
BANNED = re.compile(r"\b(conformant|compliant|certified)\b", re.I)
STATUS_WORDS = {"custom", "provisional", "custom, partly provisional"}


@pytest.fixture(scope="module")
def full_spec(model) -> dict:
    return S.make_spec(model, "full")


@pytest.fixture(scope="module")
def exceptions(model) -> dict:
    return model["standards_exceptions"]


@pytest.fixture(scope="module")
def silver_by_name(model) -> dict:
    return {t["table"]: t for t in model["silver_tables"]}


def realised(model) -> dict:
    """profile entity -> the ontology entity that realises it, from model/standards.yaml."""
    return {n: e["ontology_entity"] for n, e in model["standards"]["entities"].items() if e["ontology_entity"]}


def ontology_pairs(sp: dict) -> set:
    entity = {t["table"]: t["entity"] for t in sp["tables"]}
    return {frozenset((entity[r["from"].split(".")[0]], entity[r["to"].split(".")[0]])) for r in sp["relationships"] if r["ontology"]}


def missing(relationships: list, mapped: dict, pairs: set) -> dict:
    """'A.x -> B.y' -> (ontology entity, ontology entity) for every profile relationship between two realised entities that no ontology relationship joins."""
    out = {}
    for rel in relationships:
        (fe, _), (te, _) = (side.split(".") for side in rel.split(ARROW))
        if fe in mapped and te in mapped and mapped[fe] != mapped[te] and frozenset((mapped[fe], mapped[te])) not in pairs:
            out[rel] = (mapped[fe], mapped[te])
    return out


def key_columns(table: dict, silver_table: dict) -> set:
    """The silver columns a gold table's key reads: its `from` columns and template placeholders, with `_owner_id` read as the silver table's owner column."""
    key = next(c for c in expand_columns(table) if c.get("key"))
    paths = list(key["from"] if isinstance(key.get("from"), list) else [key["from"]] if "from" in key else [])
    paths += [m.group(1) for m in TPL_RE.finditer(key.get("tpl") or "")]
    return {silver_table["owner"].lstrip("^.") if p == "_owner_id" else p for p in paths}


# --------------------------------------------------------------------------------------------------
# (a) the profile's relationships
# --------------------------------------------------------------------------------------------------
def test_every_profile_relationship_between_realised_entities_is_in_the_ontology_or_an_honest_exception(model, full_spec, exceptions, silver_by_name):
    mapped = realised(model)
    rels = model["standards"]["relationships"]
    both = [r for r in rels if all(side.split(".")[0] in mapped for side in r.split(ARROW))]
    assert len(rels) == 65 and len(both) == 17                                    # the profile's 65; 17 join two entities the ontology realises
    gaps = missing(rels, mapped, ontology_pairs(full_spec))
    listed = {f"{e['from']}{ARROW}{e['to']}": e for e in exceptions["profile_relationships"]}
    assert set(gaps) == set(listed), f"unlisted gaps {sorted(set(gaps) - set(listed))}, stale or unknown exceptions {sorted(set(listed) - set(gaps))}"
    assert len(gaps) == 2 and len(listed) == len(exceptions["profile_relationships"])
    for rel, e in listed.items():
        assert e["reason"].strip() and "\n" not in e["reason"].strip(), rel
        (fe, fa), _ = (side.split(".") for side in rel.split(ARROW))
        table = next(t for t in silver_by_name.values() if t.get("profile_entity") == fe)
        column = next(c for c in table["columns"] if c["col"] == fa)
        assert column.get("none"), f"{rel}: the silver column {table['table']}.{fa} reads a seed field, so the link has rows to model"


def test_the_gap_check_notices_a_missing_relationship(model, full_spec):
    mapped, pairs = realised(model), ontology_pairs(full_spec)
    assert missing(["Loan.goalId -> Goal.goalId"], mapped, pairs) == {"Loan.goalId -> Goal.goalId": ("Loan", "Goal")}      # no Loan to Goal relationship
    assert missing(["Card.productId -> Product.productId", "Application.x -> Product.y", "Consent.a -> FrontierConsent.b"], mapped, pairs) == {}
    assert frozenset(("Account", "Customer")) in pairs and frozenset(("Customer", "Account")) in pairs


# --------------------------------------------------------------------------------------------------
# (b) the keys
# --------------------------------------------------------------------------------------------------
def test_every_ontology_key_is_the_business_key_of_its_silver_table_or_an_honest_exception(model, exceptions, silver_by_name):
    listed = {(e["entity"], e["silver_table"]): e for e in exceptions["ontology_keys"]}
    off, checked = {}, 0
    for t in model["tables"]:
        if not t.get("silver") or t.get("ontology") is False:
            continue                                                              # a curated reference table has no silver table: its key is unique and never null (test_gold_star)
        for s in t["silver"]:
            checked += 1
            if key_columns(t, silver_by_name[s]) != set(silver_by_name[s]["key"]):
                off[(t["entity"], s)] = (sorted(key_columns(t, silver_by_name[s])), silver_by_name[s]["key"])
    assert checked == 44                                                          # 42 silver-fed tables, two of them (Customer, Consent) fed by two silver tables
    assert set(off) == set(listed), f"keys that differ from the business key: {off}"
    assert all(e["reason"].strip() and "\n" not in e["reason"].strip() for e in exceptions["ontology_keys"])


def test_the_key_exceptions_are_true_of_the_data(silver_by_name, silver_rows):
    crm = silver_by_name["silver_crm_case"]
    assert crm["owner"] == "customerId" and crm["key"] == ["caseId"]                                # the customer id of a CRM-only party is the owner of the case
    assert {r["customerId"] for r in silver_rows["silver_crm_case"]} and all(r["customerId"] for r in silver_rows["silver_crm_case"])
    rows = silver_rows["silver_loan_installment"]
    assert rows and all(r["ordinal"] == r["installmentNumber"] for r in rows)                      # the key reads the position; the business key names the number


def test_the_key_check_sees_a_key_that_is_not_the_business_key(model, silver_by_name):
    t = dict(next(t for t in model["tables"] if t["table"] == "account"))
    assert key_columns(t, silver_by_name["silver_account"]) == {"accountId"} == set(silver_by_name["silver_account"]["key"])
    t["columns"] = [dict(c, **({"from": "productId"} if c.get("key") else {})) for c in t["columns"]]
    assert key_columns(t, silver_by_name["silver_account"]) != set(silver_by_name["silver_account"]["key"])


# --------------------------------------------------------------------------------------------------
# (c) the standards sentence
# --------------------------------------------------------------------------------------------------
def expected_bian(model, entity: str) -> dict:
    """domain -> status words for an ontology entity, from the registry counts alone (custom, provisional, or both)."""
    out = {}
    for name, e in model["standards"]["entities"].items():
        if e["ontology_entity"] == entity:
            for b in e["bian"]:
                out.setdefault(b["domain"], "custom, partly provisional" if b["custom"] and b["provisional"] else "provisional" if b["provisional"] else "custom")
    return out


def stated_bian(description: str) -> dict:
    """domain -> status words as the description's standards sentence states them."""
    m = re.search(r"Standards: (?P<bian>.*?); (?:Microsoft CDM .*?|no Microsoft CDM reference)\. Demo mapping, not certification\.", description)
    assert m, description
    if m["bian"].startswith("no verified BIAN mapping"):
        return {}
    assert m["bian"].startswith("BIAN 13.0.0 ")
    items = re.split(r"(?<=\)), ", m["bian"][len("BIAN 13.0.0 "):])
    return {d: s for d, s in (re.fullmatch(r"(.*) \((.*)\)", i).groups() for i in items)}


def test_a_description_lists_the_bian_domains_with_the_registry_status_words(model, full_spec):
    domains = {b["domain"] for e in model["standards"]["entities"].values() for b in e["bian"]}
    assert len(domains) == 17
    aligned = 0
    for t in full_spec["tables"]:
        want = expected_bian(model, t["entity"])
        if t["entity"] in set(realised(model).values()):
            aligned += 1
            got = stated_bian(t["description"])
            assert got == want, t["entity"]
            assert set(got.values()) <= STATUS_WORDS
            if not want:
                assert "no verified BIAN mapping (custom semantics)" in t["description"]
        else:
            assert standards.EXTENSION_NOTE in t["description"] and want == {}, t["entity"]
        assert {d for d in domains if d in t["description"]} == set(want), (t["entity"], "names a BIAN domain the registry does not give it")
    assert aligned == 18


def test_no_status_is_upgraded(model, full_spec):
    desc = {t["entity"]: t["description"] for t in full_spec["tables"]}
    for name, e in model["standards"]["entities"].items():
        for b in e["bian"]:
            stated = stated_bian(desc[e["ontology_entity"]]).get(b["domain"]) if e["ontology_entity"] else None
            if e["ontology_entity"] and len({n for n, x in model["standards"]["entities"].items() if x["ontology_entity"] == e["ontology_entity"]}) == 1:
                assert ("provisional" in stated) == bool(b["provisional"]), (name, b["domain"], stated)      # provisional stays provisional
                assert ("custom" in stated) == bool(b["custom"]), (name, b["domain"], stated)
    assert stated_bian(desc["Deposit"])["Term Deposit"] == "custom, partly provisional" and stated_bian(desc["Goal"])["Customer Relationship Management"] == "provisional"
    assert stated_bian(desc["ServiceCase"]) == {"Customer Case": "custom", "Servicing Order": "provisional"}
    for entity in ("FrontierMandate", "HouseholdAccessGrant", "RegisteredAgent", "MandateAction", "ProactiveCandidate", "Consent"):
        assert stated_bian(desc[entity]) == {}                                                       # unmapped stays unmapped, with no service domain named


def test_the_status_check_notices_an_upgrade(model, full_spec):
    desc = next(t["description"] for t in full_spec["tables"] if t["entity"] == "Deposit")
    assert stated_bian(desc) != stated_bian(desc.replace("Term Deposit (custom, partly provisional)", "Term Deposit (custom)"))
    assert stated_bian(desc.replace("Term Deposit (custom, partly provisional)", "Term Deposit (custom)"))["Term Deposit"] != expected_bian(model, "Deposit")["Term Deposit"]


# --------------------------------------------------------------------------------------------------
# (d) no claim beyond the demo mapping
# --------------------------------------------------------------------------------------------------
def certification_is_negated(text: str) -> list:
    """The `certif...` words of a text that are not inside a sentence that says `not` or `never` before them."""
    return [m.group(0) for m in re.finditer(r"certif\w*", text, re.I) if not re.search(r"\b(not|never)\b[^.]*$", text[:m.start()], re.I)]


def my_texts(model, sp) -> dict:
    texts = {"ontology description": sp["ontology"]["description"], "standards disclaimer": model["standards"]["disclaimer"]}
    for t in sp["tables"]:
        texts[f"{t['entity']} description"] = t["description"]
        texts |= {f"{t['entity']}.{c['col']}": c["desc"] for c in t["columns"]}
    texts |= {f"relationship {r['name']}": r.get("desc", "") for r in sp["relationships"]}
    texts |= {f"measure {m['name']}": m.get("desc", "") for m in sp["measures"]}
    texts |= {f"rule {r['name']}": r["statement"] for r in sp["rules"]}
    for name in ("docs/NAMING_STANDARD.md", "docs/MEDALLION.md", "model/standards_exceptions.yaml"):
        texts[name] = (KIT / name).read_text(encoding="utf-8")
    design = (KIT / "docs" / "MEDALLION_DESIGN.md").read_text(encoding="utf-8")
    for heading in ("## Gold implementation notes", "## Ontology and naming implementation notes"):
        if heading in design:
            texts[heading] = design.split(heading, 1)[1].split("\n## ", 1)[0]
    return texts


def test_no_text_claims_conformance_compliance_or_certification(model, full_spec):
    texts = my_texts(model, full_spec)
    assert len(texts) > 500
    for name, text in texts.items():
        assert not BANNED.search(text), f"{name}: {BANNED.search(text).group(0)}"
        assert certification_is_negated(text) == [], f"{name}: certification without a 'not' before it"


def test_the_wording_check_sees_a_claim_and_allows_the_caveat():
    assert BANNED.search("The ontology is BIAN-compliant.") and BANNED.search("a conformant model") and BANNED.search("BIAN certified")
    assert certification_is_negated("Standards: BIAN 13.0.0 Savings Account (custom). Demo mapping, not certification.") == []
    assert certification_is_negated("not Microsoft endorsement, BIAN certification or regulatory compliance") == []
    assert certification_is_negated("This earns a BIAN certification.") == ["certification"]
    assert certification_is_negated("It is certified. Not a claim.") == ["certified"]
