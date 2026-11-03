"""Ownership ledger and lock: the safety net behind wipe.sh."""
import json
import os

import pytest

from contoso_onto.ledger import LedgerError, Ledger, Lock, LockBusy, WIPE_ORDER


def test_record_get_and_persist(tmp_path):
    led = Ledger(tmp_path / "inv" / "ledger.json")
    led.record("fabricWorkspace", "WS", owned=True, state="pending", capacity="cap")
    led.record("fabricWorkspace", "WS", owned=True, state="present", id="ws-1")
    again = Ledger(tmp_path / "inv" / "ledger.json")
    e = again.get("fabricWorkspace", "WS")
    assert e["id"] == "ws-1" and e["state"] == "present" and e["owned"] and e["details"] == {"capacity": "cap"}
    assert not list((tmp_path / "inv").glob(".ledger-*"))                      # atomic write leaves no temp files behind


def test_ownership_is_never_downgraded(tmp_path):
    led = Ledger(tmp_path / "l.json")
    led.record("fabricCapacity", "cap", owned=True, state="present")
    led.record("fabricCapacity", "cap", owned=False, state="present")           # a re-run that merely finds its own capacity
    assert led.get("fabricCapacity", "cap")["owned"] is True
    led.record("resourceGroup", "rg", owned=False, state="present")             # adopted stays adopted
    led.record("resourceGroup", "rg", owned=False, state="present")
    assert led.get("resourceGroup", "rg")["owned"] is False and led.owned() == [led.get("fabricCapacity", "cap")]


def test_pending_never_downgrades_a_present_entry(tmp_path):
    led = Ledger(tmp_path / "l.json")
    led.record("fabricCapacity", "cap", owned=True, state="present")
    led.record("fabricCapacity", "cap", owned=True, state="pending")
    assert led.get("fabricCapacity", "cap")["state"] == "present"


def test_wipe_plan_is_children_first_and_only_owned_and_live(tmp_path):
    led = Ledger(tmp_path / "l.json")
    for kind in ("resourceGroup", "fabricCapacity", "fabricWorkspace", "fabricOntology", "fabricSemanticModel", "fabricLakehouse"):
        led.record(kind, kind + "-1", owned=True, state="present")
    led.record("fabricWorkspace", "adopted-ws", owned=False, state="present")
    led.record("fabricLakehouse", "gone", owned=True, state="present")
    led.mark("fabricLakehouse", "gone", "deleted")
    plan = [e["kind"] for e in led.wipe_plan()]
    assert plan == [k for k in WIPE_ORDER] and "adopted-ws" not in [e["name"] for e in led.wipe_plan()]
    assert plan.index("fabricWorkspace") < plan.index("fabricCapacity") < plan.index("resourceGroup")      # the billable capacity goes after its workspace


def test_tombstones_and_a_fresh_life_cycle_after_delete(tmp_path):
    led = Ledger(tmp_path / "l.json")
    led.record("fabricCapacity", "cap", owned=True, state="present", id="id-1")
    led.mark("fabricCapacity", "cap", "deleted", "deleted by wipe")
    led.mark("resourceGroup", "unknown", "deleted")                                # unknown entries are ignored, not an error
    assert Ledger(tmp_path / "l.json").data["tombstones"][0]["outcome"] == "deleted by wipe"
    led.record("fabricCapacity", "cap", owned=False, state="present")              # re-created later by somebody else: not ours any more
    assert led.get("fabricCapacity", "cap")["owned"] is False and led.wipe_plan() == []
    led.mark("fabricCapacity", "cap", "released", "kept")
    assert led.data["tombstones"][-1]["outcome"] == "kept"


def test_corrupt_or_foreign_ledgers_are_refused_not_overwritten(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(LedgerError, match="only record"):
        Ledger(p)
    assert p.read_text() == "{not json"
    p.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
    with pytest.raises(LedgerError, match="version"):
        Ledger(p)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_ledger_file_is_private(tmp_path):
    led = Ledger(tmp_path / "l.json")
    led.record("fabricCapacity", "cap", owned=True)
    assert (tmp_path / "l.json").stat().st_mode & 0o077 == 0


def test_lock_is_exclusive_and_released(tmp_path):
    p = tmp_path / "lock" / "build.lock"
    with Lock(p):
        with pytest.raises(LockBusy):
            with Lock(p):
                pass
    with Lock(p):                                                                  # released on exit, also after contention
        pass


def test_lock_is_released_when_the_body_fails(tmp_path):
    p = tmp_path / "build.lock"
    with pytest.raises(RuntimeError):
        with Lock(p):
            raise RuntimeError("boom")
    with Lock(p):
        pass
