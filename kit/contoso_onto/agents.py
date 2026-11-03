"""Agent grounding pack generated from the same spec as the ontology: data-agent instructions, data dictionary, voice
profile, MCP / Foundry IQ wiring and the golden evaluation set. Everything here is text; nothing is sent anywhere."""
from __future__ import annotations

import json
import re
from typing import Optional

from .oac import physical_of

FABRIC_MCP = "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{ws}/items/{ont}/ontologyEndpoint"
IQ_API = "2026-08-01-preview"


def _ont_tables(spec: dict) -> list[dict]:
    return [t for t in spec["tables"] if t["ontology"]]


def _kind(c: dict) -> str:
    col = c["col"]
    if col.endswith("_inr") or col.endswith("_crore_inr"):
        return "money_inr" if not col.endswith("_crore_inr") else "money_crore_inr"
    if col.endswith("_paise"):
        return "money_paise"
    if col.endswith("_pct"):
        return "percent"
    if col.endswith("_bps"):
        return "basis_points"
    if c["type"] == "date":
        return "date"
    if c["type"] == "datetime":
        return "instant_utc"
    if c.get("key") or col.endswith("_id") or col.endswith("_ids"):
        return "identifier"
    return c["type"]


def data_dictionary(spec: dict) -> dict:
    """The data dictionary. Table names here are the lakehouse (physical) names; `logicalId` is the table's key in the specification, which
    relationships, measures and rules use. Ontology and semantic model objects are named by entity."""
    by_table = {t["table"]: t for t in spec["tables"]}

    def endpoint(ref: str) -> str:
        table, _, col = ref.partition(".")
        return f"{physical_of(by_table[table])}.{col}" if table in by_table else ref

    ents = []
    for t in spec["tables"]:
        cols = []
        for c in t["columns"]:
            cols.append({"column": c["col"], "property": c["prop"] if c["ontology"] and t["ontology"] else None,
                         "label": c["label"], "type": c["type"], "kind": _kind(c), "key": c["key"], "pii": c["pii"],
                         "inOntology": bool(c["ontology"] and t["ontology"]), "hiddenInSemanticModel": c["hidden"],
                         "description": c["desc"]})
        reg = t.get("registry") or {}
        ents.append({"table": physical_of(t), "logicalId": t["table"], "entity": t["entity"] if t["ontology"] else None,
                     "kind": t.get("kind"), "grain": t.get("grain"), "silver": list(t.get("silver") or []), "bronzeFiles": reg.get("bronze_files", []),
                     "profileEntity": reg.get("profile_entities", []), "extension": reg.get("extension"), "family": reg.get("family"),
                     "cdmReference": [{"entity": c["entity"], "status": "semantic reference only", "nameAliases": c["aliases"]} for c in reg.get("cdm", [])],
                     "bianDomains": [{"domain": b["domain"], "controlRecord": b["control_record"], "status": b["status"]} for b in reg.get("bian", [])],
                     "renamedFrom": reg.get("renamed_from") or None, "domain": t["domain"], "tier": t["tier"],
                     "description": t["description"], "synonyms": t["synonyms"], "columns": cols})
    return {"name": spec["name"], "descriptionsMode": spec.get("descriptionsMode", "full"), "entities": ents,
            "relationships": [{"name": r["name"], "from": endpoint(r["from"]), "to": endpoint(r["to"]), "inOntology": r["ontology"],
                               "inSemanticModel": r["semantic"], "description": r.get("desc", "")} for r in spec["relationships"]],
            "measures": [{"name": m["name"], "table": physical_of(by_table[m["table"]]), "entity": by_table[m["table"]]["entity"],
                          "description": m.get("desc", ""), "dax": m["expr"]} for m in spec["measures"]],
            "rules": [{"name": r["name"], "statement": r["statement"]} for r in spec["rules"]]}


def _architecture_lines(e: dict) -> list[str]:
    """The star role, lineage and standards facts of one dictionary entity as markdown bullets (nothing here is typed by hand)."""
    lineage = " + ".join(f"`{s}`" for s in e["silver"]) + (f" <- {', '.join(e['bronzeFiles'])}" if e["bronzeFiles"] else "") if e["silver"] else "curated reference data"
    profile = ", ".join(e["profileEntity"]) if e["profileEntity"] else "none (Contoso extension entity)"
    cdm = "; ".join(f"{c['entity']} ({c['status']})" + (f", name aliases {', '.join(c['nameAliases'])}" if c["nameAliases"] else "") for c in e["cdmReference"]) or "none"
    bian = ", ".join(f"{b['domain']} ({b['status']})" for b in e["bianDomains"]) or "none verified"
    return [f"- **Star role:** {e['kind']}. **Grain:** {e['grain']}",
            f"- **Lineage:** `{e['table']}` <- {lineage}. **Profile entity:** {profile}. **Official CDM reference:** {cdm}. **BIAN domains:** {bian}."]


def data_dictionary_md(dd: dict) -> str:
    L = [f"# {dd['name']} data dictionary (generated)", "",
         "Columns marked *restricted* stay in the lakehouse and are not exposed through the ontology. Columns ending `_inr` are rupees, `_paise` exact integer paise, `_pct` percent.",
         "The backticked name of each table is its lakehouse table (what a SQL query on the lakehouse names): `dim_*` dimensions, `fact_*` facts, `bridge_*` links, `ref_*` reference data. "
         "The ontology and the semantic model use the entity names.", ""]
    if dd.get("descriptionsMode") == "compact":
        L[-1:] = ["Entity descriptions are in the compact form (the business meaning and the standards sentence, the same text the ontology and the semantic model carry); "
                  "the star role, lineage and standards facts of every entity are listed under its description.", ""]
    by_domain: dict[str, list[dict]] = {}
    for e in dd["entities"]:
        by_domain.setdefault(e["domain"] or "Other", []).append(e)
    for dom, ents in by_domain.items():
        L += [f"## {dom}", ""]
        for e in ents:
            L.append(f"### {e['entity'] or e['table'] + ' (semantic model only)'}  `{e['table']}`  [{e['tier']}]")
            L.append(e["description"])
            if e["kind"]:
                L += ["", *_architecture_lines(e)]
            L += ["", "| Property | Column | Type | Kind | Notes |", "|---|---|---|---|---|"]
            for c in e["columns"]:
                notes = []
                if c["key"]:
                    notes.append("key")
                if c["pii"]:
                    notes.append("PII")
                if not c["inOntology"]:
                    notes.append("restricted" if c["kind"] != "money_paise" and c["kind"] != "basis_points" else "exact twin, not in ontology")
                L.append(f"| {c['property'] or '-'} | {c['column']} | {c['type']} | {c['kind']} | {', '.join(notes)} {c['description']} |")
            L.append("")
    L += ["## Relationships", "", "| Name | From | To | Ontology | Semantic model |", "|---|---|---|---|---|"]
    L += [f"| {r['name']} | {r['from']} | {r['to']} | {r['inOntology']} | {r['inSemanticModel']} |" for r in dd["relationships"]]
    L += ["", *_example_query(dd)]
    return "\n".join(L).rstrip("\n") + "\n"


def _example_query(dd: dict) -> list[str]:
    """One star join as a query on the lakehouse's SQL endpoint, from the first relationship that joins a fact to a dimension in the ontology.
    It names lakehouse tables (the physical names), never entities."""
    kind = {e["table"]: e["kind"] for e in dd["entities"]}
    for r in dd["relationships"]:
        (fact, fk), (dim, key) = r["from"].split("."), r["to"].split(".")
        if r["inOntology"] and kind.get(fact) == "fact" and kind.get(dim) == "dimension":
            return ["## Example lakehouse query", "", "A fact joins a dimension on its business key. The table names are the lakehouse tables, never entity names.", "",
                    "```sql", f"SELECT d.{key}, COUNT(*) AS fact_rows", f"FROM {fact} f", f"JOIN {dim} d ON f.{fk} = d.{key}", f"GROUP BY d.{key}", "```"]
    return []


def voice_profile(spec: dict) -> dict:
    """How each ontology property should be spoken: the contract between the ontology and the voice agent."""
    speak = {"money_inr": "Say rupees with Indian grouping (lakh, crore). Round only for speech. State the as-of date.",
             "money_crore_inr": "Say crore rupees.", "percent": "Say 'percent'; 8.5 is eight point five percent.",
             "date": "Say day, month, year. India time.", "instant_utc": "Convert to India time before speaking.",
             "identifier": "Never read an identifier aloud; say the display name instead.", "basis_points": "Not exposed.", "money_paise": "Not exposed."}
    props = []
    for t in _ont_tables(spec):
        for c in t["columns"]:
            if not c["ontology"]:
                continue
            k = _kind(c)
            props.append({"entity": t["entity"], "property": c["prop"], "kind": k, "speak": speak.get(k, "Say it naturally."),
                          "pii": c["pii"], "neverSpeakInFull": c["pii"] or "masked" in c["col"] or c["col"].endswith("_fingerprint")})
    return {"rules": {"language": "Respond in the customer's preferred language; Indian English by default.",
                      "amounts": "Speak amounts as rupees, never as paise. Never combine release ids.",
                      "identity": "Employees authenticate with their own identity (on-behalf-of); customers are verified by the bank's deterministic Tool API, never by the model.",
                      "confirmation": "Read back any figure you quote together with the date it is valid for. Ask before any action; actions are prepared, not executed.",
                      "barge_in": "Stop speaking on barge-in; never finish a sentence that contains a figure after being interrupted."},
            "properties": props}


def _former_words(spec: dict) -> list[tuple[str, str]]:
    """(the words customers use, the entity) for every ontology entity the naming standard renamed: the registry's `renamed_from`."""
    out = []
    for t in _ont_tables(spec):
        old = (t.get("renamed_from") or {}).get("entity")
        if old:
            out.append((re.sub(r"(?<=[a-z])(?=[A-Z])", " ", old).lower(), t["entity"]))
    return out


def data_agent_instructions(spec: dict, entity_count: Optional[int] = None) -> str:
    ents = [t["entity"] for t in _ont_tables(spec)]
    measures = ", ".join(m["name"] for m in spec["measures"][:12])
    former = ", ".join(f"{words} = {entity}" for words, entity in _former_words(spec))
    names = f"- Entity names follow the bank's CDM profile, while customers use plainer words: {former}.\n" if former else ""
    standards = ""
    if any("Standards: " in (t.get("description") or "") for t in _ont_tables(spec)):         # only when the ontology carries the BIAN / CDM alignment
        no_bian = ", ".join(t["entity"] for t in _ont_tables(spec) if "no verified BIAN mapping" in (t.get("description") or ""))
        tail = ", then their star role and lakehouse lineage" if spec.get("descriptionsMode", "full") == "full" else " (their star role and lineage are in the data dictionary)"
        standards = ("\nSTANDARDS\n"
                     f"- Entity descriptions carry their BIAN 13 and Microsoft CDM alignment{tail}. It is a demo mapping carried over from the source repo, not a certification: "
                     "never say the data or the ontology is BIAN-compliant, BIAN-certified or endorsed by Microsoft.\n"
                     f"- Where an entity says there is no verified BIAN mapping ({no_bian}), say so plainly instead of naming a service domain.\n"
                     "- An entity described as a Contoso extension entity has no CDM profile entity: never present it as part of a standard.\n")
    return f"""You are the Contoso Bank data assistant. You answer questions about Contoso Bank's SYNTHETIC demo data through the {spec['ontology']['name']} ontology ({entity_count or len(ents)} entity types). You are read-only: you analyse and explain, you never execute, approve or change anything.

IDENTITY AND SCOPE
- Default to ReleaseId = retail-frontier-v1: the Rhea Bansal household (CustomerId rhea). Records with ReleaseId retail4-v1 are the older generation. Customer rhea~legacy is a superseded, non-reconciled copy of Rhea: never merge it with rhea and never add its balances to Rhea's.
- Never combine records across ReleaseId. If a question is ambiguous about who or which release, ask one short clarifying question.
- Only answer for the customer or household in the question. Never reveal another customer's data.

UNITS
- Properties ending Inr are rupees. Paise columns are not exposed. 'Crore' = 10,000,000 rupees. Properties ending Pct are percentages (8.5 = 8.5%).
- Say amounts with Indian grouping (INR 12,50,000 or 12.5 lakh) and always give the date a balance is valid for (Account.BalanceAsOn).

HONESTY RULES (these come from the bank's own laws)
- Quote a figure only if you read it from the data. If it is not there, say you cannot see it. Never estimate or round the source figure.
- Goal.CurrentInr is an earmark on money already in an account or investment: never add goal balances to balances. A deposit that also appears as an Investment is one asset. Card credit limits are not cash. Loan proceeds are a liability, not income. Forecast income and unposted maturities are not cash.
- Net position excludes the home's market value (no valuation in the data).
- Recording a service instruction is not executing it: use ServiceRequestType.NeverClaim for what must never be claimed.
- While an Incident is unresolved, do not recommend or sell products.
- Household access is resource-specific: use HouseholdAccessGrant.Permissions and DeniedPermissions, never the relationship. Agent scopes: RegisteredAgent.Scopes and DeniedScopes (denied wins). Who may do what: AuthorityRule.

HOW TO QUERY
- Prefer DefaultLabel / DisplayName / Name properties when naming things; show internal ids only when asked.
{names}- Support group by in GQL. Use Transaction.SignedAmountInr for net cash flow and Transaction.PostedDateIst for 'this month' questions (India time).
- Useful metrics (semantic model): {measures}.
- For policy and procedure text, say which Sop or Law applies and that the full text is in the knowledge base.
{standards}
PRIVACY
- Never speak or write masked numbers, phone numbers, addresses, policy numbers or key fingerprints in full: at most the last four characters. Properties tagged [PII] are personal data.

ANSWER STYLE
- Lead with the answer, then the figure with unit and as-of date, then the entity it came from. Keep voice answers to two short sentences.
"""


def mcp_configs(workspace_id: str = "<WORKSPACE_ID>", ontology_id: str = "<ONTOLOGY_ID>", name: str = "contoso-bank-ontology") -> dict:
    url = FABRIC_MCP.format(ws=workspace_id, ont=ontology_id)
    return {
        "endpoint": url,
        "vscode_mcp.json": {"servers": {name: {"type": "http", "url": url}}},
        "copilot_studio": {"steps": ["Agent > Tools > Add a tool > Model Context Protocol > Fabric IQ MCP (Preview)", f"Select workspace and ontology '{name}'",
                                     "Authenticate with the end user's Entra identity (service principals are not supported by the ontology MCP endpoint)"]},
        "foundry_agent_tool": {"type": "mcp", "server_label": name, "server_url": url, "require_approval": "never",
                               "note": "Use the on-behalf-of token of the signed-in employee; keep customer voice flows behind a deterministic Tool API."},
        "tools_to_expect": ["list_ontology_entity_types", "search_ontology", "list_ontology_rules"],
    }


def foundry_iq_pack(workspace_id: str = "<WORKSPACE_ID>", ontology_id: str = "<ONTOLOGY_ID>", ks_name: str = "contoso-bank-ontology-ks",
                    kb_name: str = "contoso-bank-ontology-kb") -> dict:
    ks = {"name": ks_name, "kind": "fabricOntology",
          "description": "Contoso bank business-context ontology in Microsoft Fabric IQ: customers, households, holdings, money movement, goals, delegated authority, servicing, catalogue and bank laws (synthetic).",
          "fabricOntologyParameters": {"workspaceId": workspace_id, "ontologyId": ontology_id}}
    kb = {"name": kb_name, "description": "Structured, ontology-grounded answers for Contoso bank agents. Query with the end user's token in x-ms-query-source-authorization.",
          "knowledgeSources": [{"name": ks_name}], "retrievalReasoningEffort": {"kind": "low"}}
    return {"api_version": IQ_API, "knowledge_source": ks, "knowledge_base": kb,
            "rest": [f"PUT {{AZURE_AI_SEARCH_ENDPOINT}}/knowledgesources/{ks_name}?api-version={IQ_API}",
                     f"PUT {{AZURE_AI_SEARCH_ENDPOINT}}/knowledgebases/{kb_name}?api-version={IQ_API}"],
            "toolbox_connection": {"name": "contoso-bank-ontology-mcp", "kind": "remote-mcp",
                                   "target": f"${{AZURE_AI_SEARCH_ENDPOINT}}/knowledgebases/{kb_name}/mcp?api-version={IQ_API}",
                                   "authType": "project-managed-identity", "audience": "https://search.azure.com", "allowedTools": ["knowledge_base_retrieve"],
                                   "structuredInputs": [{"name": "user_search_token", "header": "x-ms-query-source-authorization", "persist": False}]},
            "notes": ["The existing repo knowledge base is extractive (reasoning 'minimal'). The Fabric ontology source needs reasoning effort low or medium, so it lives in its own knowledge base and its own MCP tool; the extractive KB stays untouched.",
                      "Verify field names against the Azure AI Search 2026-08-01-preview reference before the first PUT: this API is in preview."]}


def build_pack(spec: dict, golden: list[dict], brief: str, workspace_id: str = "<WORKSPACE_ID>", ontology_id: str = "<ONTOLOGY_ID>") -> dict[str, str]:
    dd = data_dictionary(spec)
    files = {
        "data_agent/instructions.md": data_agent_instructions(spec),
        "data_agent/golden_questions.json": json.dumps(golden, indent=1, default=str),
        "dictionary/data_dictionary.json": json.dumps(dd, indent=1),
        "dictionary/data_dictionary.md": data_dictionary_md(dd),
        "voice/voice_profile.json": json.dumps(voice_profile(spec), indent=1),
        "mcp/mcp_configs.json": json.dumps(mcp_configs(workspace_id, ontology_id), indent=1),
        "foundry_iq/ontology_knowledge.json": json.dumps(foundry_iq_pack(workspace_id, ontology_id), indent=1),
        "grounding_brief.md": brief,
    }
    return files
