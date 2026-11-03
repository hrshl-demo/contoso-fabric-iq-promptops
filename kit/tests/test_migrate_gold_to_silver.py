"""tools/migrate_gold_to_silver.py, the one-off gold migration, on a small v1 gold file and a small silver spec: sources, column and transform
mapping, the star header, untouched comments and descriptions, idempotence, the lineage rows and the command line. The real migration is
judged by tests/test_gold_equivalence.py (values) and tests/test_lineage.py (lineage)."""
import csv
import io
import re

import pytest
import yaml


def col(name: str, typ: str, src: str, **extra) -> dict:
    return {"col": name, "origin": "extension", "type": typ, "from": src, **extra}


SILVER = {"tables": [
    {"table": "silver_household", "serves": ["household"], "owner": "ownerId", "sources": [{"release": "r1", "file": "personas.json"}],
     "columns": [col("householdId", "string", "householdId"), col("primaryOwnerName", "string", "name"), col("xCity", "string", "city")]},
    {"table": "silver_party", "serves": ["customer"], "owner": "ownerId",
     "sources": [{"release": "r1", "file": "personas.json"}, {"release": "r2", "file": "personas.json"}],
     "columns": [col("cohort", "string", "cohort")]},
    {"table": "silver_account", "serves": ["account"], "owner": "ownerId",
     "sources": [{"release": "r1", "file": "bank/accounts.json", "list": "accounts"}],
     "columns": [col("accountId", "string", "accountId"), col("postedBalanceMinor", "minor", "postedBalance"),
                 col("xInterestRatePct", "decimal", "interestRatePct"), col("xTags", "string", "tags", t="join"),
                 col("xTagCount", "integer", "tags", t="len")]},
]}

V1 = '''tables:
  # a comment before the first table survives
  - table: household
    entity: Household
    tier: core
    domain: Party
    description: "A household."
    sources:
      - {release: r1, file: personas.json, set: {unused: z}}
    columns:
      - {col: household_id, type: string, key: true, from: householdId, desc: "Household id, e.g. {x}, 'y'."}
      - {col: display_name, type: string, default_label: true, tpl: "{name} household", desc: "Derived."}
      # a comment between columns survives
      - {col: primary_customer_id, type: string, from: ownerId, t: owner, desc: "Owner."}
      - {col: release_id, type: string, from: "$release", desc: "Release."}
      - {col: city, type: string, from: city, desc: "City."}

  - table: customer
    entity: Customer
    tier: core
    domain: Party
    allow_duplicates: true   # a trailing comment survives
    description: "A customer."
    sources:
      - {release: r1, file: personas.json, set: {segment: primary, source_kind: persona}}
      - {release: r2, file: personas.json, set: {source_kind: persona}}
    columns:
      - {col: customer_id, type: string, key: true, from: [ownerId, customerId], t: owner, desc: "Id."}
      - {col: segment, type: string, from: [cohort, "$segment"], desc: "Cohort."}
      - {col: source_kind, type: string, from: "$source_kind", desc: "Kind."}

  - table: account
    entity: Account
    tier: core
    domain: Money
    description: "An account."
    sources:
      - {release: r1, file: bank/accounts.json, list: accounts}
    columns:
      - {col: account_id, type: string, key: true, from: accountId, desc: "Id."}
      - {money: posted_balance, label: Posted Balance, from: postedBalance, desc: "Balance."}
      - {rate_bps: interest_rate, from: interestRatePct, desc: "Rate."}
      - {col: tags, type: string, from: tags, t: join, desc: "Tags."}
      - {col: tag_count, type: int, from: tags, t: len, desc: "Count."}
      - {col: opened_by, type: string, from: openedBy, t: upper, desc: "Nobody carries it."}

  - table: law
    entity: Law
    tier: core
    domain: Governance
    description: "A law."
    sources:
      - {doc: docs/laws.md, custom: laws}
    columns:
      - {col: law_id, type: string, key: true, from: id, desc: "Law."}
'''


@pytest.fixture(scope="module")
def migrated(migration_tool):
    report: list[str] = []
    text = migration_tool.migrate_text(V1, SILVER, report)
    return text, {t["table"]: t for t in yaml.safe_load(text)["tables"]}, report


def column(table: dict, name: str) -> dict:
    return next(c for c in table["columns"] if name in (c.get("col"), c.get("money"), c.get("rate_bps")))


def keep(text: str) -> list[str]:
    """The lines a migration must leave alone: everything but the source and column mappings and the header lines it inserts."""
    return [ln for ln in text.split("\n") if not ln.startswith("      - {") and not re.match(r"^    (kind|grain|physical|silver|reference):", ln)]


# --------------------------------------------------------------------------------------------------
# the text helpers
# --------------------------------------------------------------------------------------------------
def test_split_flow_keeps_quoted_commas_braces_and_doubled_quotes(migration_tool):
    line = "      - {col: a, desc: \"x, y} z\", t: 'it''s', from: [p, q], set: {k: v}}   # tail"
    prefix, items, suffix = migration_tool.split_flow(line)
    assert prefix == "      - {" and suffix == "}   # tail"
    assert items == ["col: a", 'desc: "x, y} z"', "t: 'it''s'", "from: [p, q]", "set: {k: v}"]
    with pytest.raises(ValueError):
        migration_tool.split_flow("      - {a: 1")


def test_render_writes_bare_words_and_quotes_the_rest(migration_tool):
    r = migration_tool.render
    assert [r("abc"), r("_release_id"), r("hello-world"), r("a b"), r("$release"), r("yes"), r("")] == \
        ["abc", "_release_id", "hello-world", '"a b"', '"$release"', '"yes"', '""']
    assert r(["a", "b c"]) == '[a, "b c"]' and r({"k": "v w"}) == '{k: "v w"}'
    assert [r(None), r(True), r(False), r(5), r(1.5)] == ["null", "true", "false", "5", "1.5"]
    assert r('say "hi"') == '"say \\"hi\\""' and r("Rupee ₹") == '"Rupee ₹"'
    assert all(yaml.safe_load("v: " + r(s))["v"] == s for s in ("abc", "a b", "yes", "$x", "x: y", "a, b", '"q"', "it's", "₹"))


def test_set_item_replaces_in_place_inserts_after_an_anchor_or_appends_and_drop_item_removes(migration_tool):
    items = ["col: a", "from: b", 'desc: "d"']
    assert migration_tool.set_item(items, "from", "c") == ["col: a", "from: c", 'desc: "d"']
    assert migration_tool.set_item(items, "t", "owner", after="from") == ["col: a", "from: b", "t: owner", 'desc: "d"']
    assert migration_tool.set_item(items, "unit", "pct_text") == items + ["unit: pct_text"]
    assert migration_tool.drop_item(items, "from") == ["col: a", 'desc: "d"'] and items == ["col: a", "from: b", 'desc: "d"']


# --------------------------------------------------------------------------------------------------
# the rewrite
# --------------------------------------------------------------------------------------------------
def test_every_table_gets_the_star_header_from_the_design_table(migrated):
    _, tables, _ = migrated
    assert {n: (t["kind"], t["physical"], t["grain"]) for n, t in tables.items()} == {
        "household": ("dimension", "dim_household", "One household."), "customer": ("dimension", "dim_customer", "One customer identity per release."),
        "account": ("dimension", "dim_account", "One account, with its balances as at the release business date."),
        "law": ("reference", "ref_law", "One design law.")}
    assert {n: t.get("silver") for n, t in tables.items()} == {"household": ["silver_household"], "customer": ["silver_party"],
                                                              "account": ["silver_account"], "law": None}
    assert {n: t.get("reference") for n, t in tables.items()} == {"household": None, "customer": None, "account": None, "law": True}


def test_sources_become_silver_sources_with_a_release_filter_only_where_several_sources_differ(migrated):
    _, tables, _ = migrated
    assert tables["household"]["sources"] == [{"silver": "silver_household"}]                    # the unread `set: {unused: z}` is dropped
    assert tables["customer"]["sources"] == [
        {"silver": "silver_party", "where": {"_release_id": "r1"}, "set": {"segment": "primary", "source_kind": "persona"}},
        {"silver": "silver_party", "where": {"_release_id": "r2"}, "set": {"source_kind": "persona"}}]
    assert tables["account"]["sources"] == [{"silver": "silver_account"}]
    assert tables["law"]["sources"] == [{"doc": "docs/laws.md", "custom": "laws"}]                # a reference table keeps its curated source


def test_columns_name_silver_columns_and_the_release_and_owner_columns(migrated):
    _, tables, _ = migrated
    h = tables["household"]
    assert column(h, "household_id")["from"] == "householdId" and column(h, "city")["from"] == "xCity"
    assert column(h, "display_name")["tpl"] == "{primaryOwnerName} household" and "from" not in column(h, "display_name")
    assert (column(h, "primary_customer_id")["from"], column(h, "primary_customer_id")["t"]) == ("_owner_id", "owner")
    assert column(h, "release_id")["from"] == "_release_id"
    c = tables["customer"]
    assert column(c, "customer_id")["from"] == "_owner_id"                                        # the `customerId` fallback has no silver column
    assert column(c, "segment")["from"] == ["cohort", "$segment"] and column(c, "source_kind")["from"] == "$source_kind"      # context constants stay
    assert tables["law"]["columns"][0]["from"] == "id"


def test_transforms_follow_the_silver_representation(migrated, migration_tool):
    _, tables, _ = migrated
    a = tables["account"]
    assert column(a, "posted_balance")["from"] == "postedBalanceMinor" and "t" not in column(a, "posted_balance")      # the money macro picks the minor transforms
    assert (column(a, "interest_rate")["from"], column(a, "interest_rate")["unit"]) == ("xInterestRatePct", "pct_text")
    assert (column(a, "tags")["from"], "t" in column(a, "tags")) == ("xTags", False)               # silver already joined the list
    assert (column(a, "tag_count")["from"], "t" in column(a, "tag_count")) == ("xTagCount", False)  # ... and already counted it
    expanded = {c["col"]: c["t"] for c in migration_tool.gold.expand_columns(a) if c["col"].startswith(("posted_balance", "interest_rate"))}
    assert expanded == {"posted_balance_inr": "minor_to_inr", "posted_balance_paise": "minor_to_paise",
                        "interest_rate_pct": "dec_to_float", "interest_rate_bps": "dec_to_bps"}


def test_a_column_no_silver_column_carries_becomes_none_and_is_reported(migrated):
    _, tables, report = migrated
    c = column(tables["account"], "opened_by")
    assert "from" not in c and "t" not in c and c["none"].startswith("no silver column carries the seed field openedBy")
    text = "\n".join(report)
    assert "NONE    opened_by: no silver column carries the seed field openedBy" in text
    assert "WARNING customer_id: dropped fallback path(s) ['customerId']" in text
    assert "law: reference table, sources kept" in report


def test_comments_descriptions_and_every_other_line_survive_verbatim(migrated):
    text, tables, _ = migrated
    assert keep(text) == keep(V1)
    assert "  # a comment before the first table survives" in text and "      # a comment between columns survives" in text
    assert "    allow_duplicates: true   # a trailing comment survives" in text
    before = {(t["table"], c.get("col") or c.get("money") or c.get("rate_bps")): c["desc"] for t in yaml.safe_load(V1)["tables"] for c in t["columns"]}
    after = {(n, c.get("col") or c.get("money") or c.get("rate_bps")): c["desc"] for n, t in tables.items() for c in t["columns"]}
    assert before == after and before[("household", "household_id")] == "Household id, e.g. {x}, 'y'."


def test_migrating_a_migrated_file_changes_nothing(migrated, migration_tool):
    text, _, _ = migrated
    again: list[str] = []
    assert migration_tool.migrate_text(text, SILVER, again) == text
    assert again == [f"{n}: already migrated" for n in ("household", "customer", "account", "law")]


def test_a_gold_source_no_silver_source_reads_is_reported_not_guessed(migration_tool):
    report: list[str] = []
    migration_tool.migrate_text(V1.replace("bank/accounts.json", "bank/other.json"), SILVER, report)
    assert any("matches 0 silver sources among ['silver_account']" in line for line in report)


# --------------------------------------------------------------------------------------------------
# the lineage rows and the command line
# --------------------------------------------------------------------------------------------------
def test_lineage_rows_name_the_silver_column_every_gold_column_reads(migrated, migration_tool):
    _, tables, _ = migrated
    rows = migration_tool.lineage_rows({"tables": list(tables.values())}, SILVER)
    got = {tuple(r[:4]): r[4] for r in rows}
    assert got[("household", "display_name", "silver_household", "primaryOwnerName")] == ""
    assert got[("household", "primary_customer_id", "silver_household", "_owner_id")] == "owner"
    assert got[("household", "release_id", "silver_household", "_release_id")] == ""
    assert got[("account", "posted_balance_inr", "silver_account", "postedBalanceMinor")] == "minor_to_inr"
    assert got[("account", "posted_balance_paise", "silver_account", "postedBalanceMinor")] == "minor_to_paise"
    assert got[("account", "interest_rate_pct", "silver_account", "xInterestRatePct")] == "dec_to_float"
    assert got[("account", "interest_rate_bps", "silver_account", "xInterestRatePct")] == "dec_to_bps"
    assert got[("customer", "segment", "silver_party", "cohort")] == "" and got[("customer", "segment", "", "")] == "context $segment = primary"
    assert got[("account", "opened_by", "", "")].startswith("none: no silver column carries")
    assert got[("law", "law_id", "reference: docs/laws.md", "id")] == ""
    assert not [r for r in rows if r[4] == "UNRESOLVED"]


def test_the_lineage_csv_has_the_documented_header_and_unix_line_endings(migrated, migration_tool):
    _, tables, _ = migrated
    rows = migration_tool.lineage_rows({"tables": list(tables.values())}, SILVER)
    text = migration_tool.lineage_csv(rows)
    assert "\r" not in text and text.splitlines()[0] == "gold_table,gold_column,silver_table,silver_column,transform"
    assert list(csv.reader(io.StringIO(text)))[1:] == rows


def test_the_command_line_dry_runs_rewrites_and_rebuilds_the_lineage(tmp_path, migration_tool, capsys):
    (tmp_path / "silver").mkdir()
    (tmp_path / "gold").mkdir()
    (tmp_path / "silver" / "10_mini.yaml").write_text(yaml.safe_dump({"silver": SILVER["tables"]}), encoding="utf-8", newline="\n")
    gold_file, lineage = tmp_path / "gold" / "10_mini.yaml", tmp_path / "GOLD_LINEAGE.csv"
    gold_file.write_text(V1, encoding="utf-8", newline="\n")
    base = ["--model-dir", str(tmp_path), "--lineage", str(lineage)]

    assert migration_tool.main(base + ["--dry-run"]) == 0
    assert gold_file.read_text(encoding="utf-8") == V1 and not lineage.exists()
    assert "household: silver ['silver_household']" in capsys.readouterr().out

    assert migration_tool.main(base) == 0
    migrated_text = gold_file.read_text(encoding="utf-8")
    assert "physical: dim_household" in migrated_text and "{silver: silver_household}" in migrated_text
    csv_text = lineage.read_text(encoding="utf-8")
    assert csv_text.startswith("gold_table,gold_column,silver_table,silver_column,transform\n") and "silver_account,postedBalanceMinor,minor_to_inr" in csv_text

    lineage.unlink()
    assert migration_tool.main(base + ["--lineage-only"]) == 0                                     # the lineage again, from the migrated YAML
    assert lineage.read_text(encoding="utf-8") == csv_text and gold_file.read_text(encoding="utf-8") == migrated_text
