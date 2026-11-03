"""Ownership ledger and build/wipe lock: the operator model of the superapp4 `build.sh` / `wipe.sh`.

* The ledger records every object this kit creates BEFORE it creates it (write-ahead) and flags it `owned` only when this
  run created it. `wipe` deletes owned objects and nothing else, so a pre-existing resource group, capacity or workspace
  that the kit merely adopted is never removed, and a crash between "create" and "record" cannot leak a billable capacity.
* Ownership is never downgraded: a re-run that finds its own capacity must not turn it into an adopted one.
* The lock makes build, wipe and capacity changes mutually exclusive (exit code 6 on contention).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

try:                                    # POSIX
    import fcntl
except ImportError:                     # pragma: no cover - Windows
    fcntl = None                        # type: ignore[assignment]
try:                                    # Windows
    import msvcrt
except ImportError:                     # pragma: no cover - POSIX
    msvcrt = None                       # type: ignore[assignment]

PROJECT = "contoso-bank-ontology-demo"
LEDGER_VERSION = 1
# Deletion order: children before parents, billable capacity last (a workspace on a deleted capacity is orphaned).
WIPE_ORDER = ("fabricOntology", "fabricSemanticModel", "fabricLakehouse", "fabricWorkspace", "fabricCapacity", "resourceGroup")
LIVE_STATES = ("pending", "present")


class LockBusy(RuntimeError):
    pass


class LedgerError(RuntimeError):
    pass


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Lock:
    """Exclusive, non-blocking, advisory lock held for the duration of a mutating command."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh: Any = None

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:                                     # pragma: no cover - Windows
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            raise LockBusy(f"another build/wipe/capacity command holds {self.path}") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, *exc: Any) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:                                     # pragma: no cover - Windows
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": LEDGER_VERSION, "project": PROJECT, "entries": [], "tombstones": []}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError as ex:
                raise LedgerError(f"ledger {self.path} is not valid JSON ({ex}); keep it, it is the only record of what was created") from None
            if loaded.get("version") != LEDGER_VERSION:
                raise LedgerError(f"ledger {self.path} has version {loaded.get('version')}, expected {LEDGER_VERSION}")
            self.data = loaded

    # ---- queries ----
    @property
    def entries(self) -> list[dict]:
        return self.data["entries"]

    def get(self, kind: str, name: str) -> Optional[dict]:
        return next((e for e in self.entries if e["kind"] == kind and e["name"] == name), None)

    def live(self) -> list[dict]:
        return [e for e in self.entries if e["state"] in LIVE_STATES]

    def owned(self, kind: Optional[str] = None) -> list[dict]:
        return [e for e in self.live() if e["owned"] and (kind is None or e["kind"] == kind)]

    def wipe_plan(self) -> list[dict]:
        """Owned, still-live entries in deletion order."""
        rank = {k: i for i, k in enumerate(WIPE_ORDER)}
        return sorted(self.owned(), key=lambda e: (rank.get(e["kind"], len(rank)), e["name"]))

    # ---- updates (each one is persisted immediately: write-ahead) ----
    def record(self, kind: str, name: str, *, owned: bool, state: str = "present", id: Optional[str] = None, **details: Any) -> dict:
        e = self.get(kind, name)
        if e is not None and e["state"] in ("deleted", "released"):    # re-created after a wipe: a fresh life cycle
            self.entries.remove(e)
            e = None
        if e is None:
            e = {"kind": kind, "name": name, "id": id, "owned": bool(owned), "state": state, "createdAt": utcnow(), "updatedAt": utcnow(), "details": dict(details)}
            self.entries.append(e)
        else:
            e["owned"] = bool(e["owned"] or owned)                    # never downgrade
            if state == "present" or e["state"] != "present":
                e["state"] = state
            if id:
                e["id"] = id
            e["details"].update(details)
            e["updatedAt"] = utcnow()
        self.save()
        return e

    def mark(self, kind: str, name: str, state: str, outcome: Optional[str] = None) -> None:
        e = self.get(kind, name)
        if e is None:
            return
        e["state"] = state
        e["updatedAt"] = utcnow()
        if state in ("deleted", "released"):
            self.data["tombstones"].append({"kind": kind, "name": name, "id": e.get("id"), "outcome": outcome or state, "at": utcnow()})
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:                                                  # pragma: no cover - Windows
            pass
