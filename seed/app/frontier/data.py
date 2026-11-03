"""Verified read model for the separate Frontier household data package."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.canonical import constant_time_equals, content_manifest_sha256, sha256_file, sha256_hex
from app.state.atomic import resolve_within

FRONTIER_SCHEMA = "retail-frontier-seed-v1"


class FrontierDataError(ValueError):
    """The Frontier package is absent, unapproved, changed, or inconsistent."""


@dataclass(frozen=True)
class FrontierData:
    release_dir: Path
    release: dict[str, Any] = field(default_factory=dict)
    data: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        release_dir: Path,
        *,
        expected_descriptor_sha256: str | None = None,
        require_approved: bool = True,
    ) -> "FrontierData":
        root = Path(release_dir)
        descriptor_path = root / "release.json"
        try:
            descriptor_bytes = descriptor_path.read_bytes()
            descriptor = json.loads(descriptor_bytes.decode("utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrontierDataError("The Frontier release descriptor is unavailable or invalid.") from exc
        if descriptor.get("schemaVersion") != FRONTIER_SCHEMA:
            raise FrontierDataError("The Frontier release has an unsupported schema version.")
        if require_approved and descriptor.get("releaseStatus") != "approved":
            raise FrontierDataError("Only an explicitly approved Frontier release may be loaded.")
        observed_descriptor = sha256_hex(descriptor_bytes)
        if expected_descriptor_sha256 is not None and not constant_time_equals(
            observed_descriptor, expected_descriptor_sha256,
        ):
            raise FrontierDataError("The Frontier release descriptor does not match its deployment pin.")
        files = descriptor.get("files")
        if not isinstance(files, dict):
            raise FrontierDataError("The Frontier release has no file manifest.")
        observed_manifest = content_manifest_sha256(files)
        approved_manifest = (descriptor.get("approval") or {}).get("contentManifestSha256", "")
        if not constant_time_equals(observed_manifest, approved_manifest):
            raise FrontierDataError("The Frontier release content manifest was not approved.")
        loaded: dict[str, list[dict[str, Any]]] = {}
        for relative, entry in sorted(files.items()):
            target = resolve_within(root, relative)
            digest, length = sha256_file(str(target))
            if not constant_time_equals(digest, entry.get("sha256", "")) or length != entry.get("bytes"):
                raise FrontierDataError(f"The Frontier release file changed: {relative}.")
            try:
                document = json.loads(target.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FrontierDataError(f"The Frontier release file is invalid JSON: {relative}.") from exc
            if not isinstance(document, list):
                raise FrontierDataError(f"The Frontier release dataset must be an array: {relative}.")
            loaded[Path(relative).stem] = document
        listed = {"release.json", *files}
        extras = [
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.relative_to(root).as_posix() not in listed
        ]
        if extras:
            raise FrontierDataError(f"Unlisted Frontier release files are present: {sorted(extras)}.")
        instance = cls(release_dir=root, release=descriptor, data=loaded)
        instance._validate_owner_scope()
        return instance

    @property
    def business_date(self) -> date:
        return date.fromisoformat(self.release["businessDate"])

    def records(self, dataset: str, owner_id: str = "rhea") -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.data.get(dataset, [])
            if row.get("ownerId", owner_id) == owner_id
        ]

    def persona(self, owner_id: str) -> dict[str, Any] | None:
        record = next(
            (dict(row) for row in self.data.get("personas", []) if row.get("ownerId") == owner_id),
            None,
        )
        if record is not None:
            record.setdefault("displayName", record.get("name"))
            record.setdefault("cohort", "frontier_household")
        return record

    def accounts(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("accounts", owner_id)

    def cards(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("cards", owner_id)

    def transactions(self, owner_id: str) -> list[dict[str, Any]]:
        rows = self.records("transactions", owner_id)
        return sorted(rows, key=lambda row: (row["valueDate"], row["transactionId"]), reverse=True)

    def goals(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("goals", owner_id)

    def loans(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("loans", owner_id)

    def investments(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("investments", owner_id)

    def insurance(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("insurance", owner_id)

    def household(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("household", owner_id)

    def obligations(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("scheduled-obligations", owner_id)

    def deposits(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("deposits", owner_id)

    def income(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("income", owner_id)

    def incidents(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("incidents", owner_id)

    def service_records(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("service-records", owner_id)

    def preferences(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("preferences", owner_id)

    def consents(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("consents", owner_id)

    def access_grants(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("access-grants", owner_id)

    def agents(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("agents", owner_id)

    def mandates(self, owner_id: str) -> list[dict[str, Any]]:
        return self.records("mandates", owner_id)

    def proactive_events(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.data.get("proactive-events", [])]

    def overview(self, owner_id: str) -> dict[str, Any]:
        accounts = self.accounts(owner_id)
        cards = self.cards(owner_id)
        loans = self.loans(owner_id)
        investments = self.investments(owner_id)
        liquid = sum(row["availableBalance"]["amountMinor"] for row in accounts)
        card_outstanding = sum(
            (row.get("currentBalance") or {}).get("amountMinor", 0) for row in cards
        )
        loan_outstanding = sum(
            (row.get("currentPrincipal") or {}).get("amountMinor", 0) for row in loans
        )
        investment_value = sum(
            (row.get("currentValue") or {}).get("amountMinor", 0) for row in investments
        )
        persona = self.persona(owner_id) or {}
        return {
            "ownerId": owner_id,
            "displayName": persona.get("displayName"),
            "businessDate": self.release["businessDate"],
            "currency": "INR",
            "totals": {
                "liquidBalanceMinor": liquid,
                "depositPrincipalMinor": sum(
                    row["principal"]["amountMinor"]
                    for row in self.records("deposits", owner_id)
                ),
                "cardOutstandingMinor": card_outstanding,
                "loanOutstandingMinor": loan_outstanding,
                "investmentValueMinor": investment_value,
            },
            "counts": {
                "accounts": len(accounts),
                "cards": len(cards),
                "deposits": len(self.records("deposits", owner_id)),
                "goals": len(self.goals(owner_id)),
                "documents": len(self.records("documents", owner_id)),
                "loans": len(loans),
                "investments": len(investments),
                "insurance": len(self.insurance(owner_id)),
                "householdMembers": len(self.household(owner_id)),
            },
        }

    def monthly_flow(self, owner_id: str, months: int = 6) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, int]] = {}
        for row in self.transactions(owner_id):
            if row.get("instrument") != "account" or row.get("status") != "posted":
                continue
            month = row["valueDate"][:7]
            bucket = buckets.setdefault(month, {"creditMinor": 0, "debitMinor": 0, "count": 0})
            amount = row["amount"]["amountMinor"]
            bucket[f"{row['direction']}Minor"] += amount
            bucket["count"] += 1
        return [
            {"month": month, **values, "netMinor": values["creditMinor"] - values["debitMinor"]}
            for month, values in sorted(buckets.items())[-months:]
        ]

    def upcoming(self, owner_id: str) -> list[dict[str, Any]]:
        today = self.business_date
        items: list[dict[str, Any]] = []
        card_balance = sum(
            (row.get("currentBalance") or {}).get("amountMinor", 0)
            for row in self.cards(owner_id)
        )
        for row in self.obligations(owner_id):
            if row.get("status") != "active":
                continue
            day = int(row["dayOfMonth"])
            year, month = today.year, today.month
            if day < today.day:
                month += 1
                if month == 13:
                    year, month = year + 1, 1
            due = date(year, month, min(day, 28))
            amount = (row.get("amount") or {}).get("amountMinor")
            if amount is None and row.get("amountRule") == "statement_due":
                amount = card_balance
            items.append({
                "kind": "scheduled_obligation",
                "referenceId": row["obligationId"],
                "label": row["name"],
                "date": due.isoformat(),
                "daysAway": (due - today).days,
                "amountMinor": amount,
            })
        return sorted(items, key=lambda row: (row["date"], row["referenceId"]))

    def _validate_owner_scope(self) -> None:
        personas = self.data.get("personas", [])
        if [row.get("ownerId") for row in personas] != ["rhea"]:
            raise FrontierDataError("The reviewed Frontier release must contain only Rhea as primary owner.")
        for dataset, rows in self.data.items():
            for row in rows:
                owner = row.get("ownerId")
                if owner is not None and owner != "rhea":
                    raise FrontierDataError(
                        f"The Frontier {dataset} dataset contains another primary owner."
                    )


__all__ = ["FRONTIER_SCHEMA", "FrontierData", "FrontierDataError"]
