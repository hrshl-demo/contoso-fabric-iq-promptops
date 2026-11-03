"""Golden questions through the live ontology MCP endpoint: ask the ontology agent (tool ask_ontology) and score its answers.

The endpoint speaks JSON-RPC 2.0 over HTTPS POST (Streamable HTTP MCP). Measured on an F4 capacity: one answer takes 25 to 100 s and about a
third of first attempts fail (connection closed without a response, empty text, or "Something went wrong ... Please try again."); asking again
usually works. HTTP 403 means the ontology agent is not enabled for the capacity.
"""
from __future__ import annotations

import contextlib
import http.client
import itertools
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import requests

from . import agents as agents_mod, oac, oracles

TOOL = "ask_ontology"
PROTOCOL_VERSION = "2025-06-18"
HTTP_TRIES = 3                      # per request: HTTP 429 and 5xx are retried with backoff
BACKOFF_S = 3.0                     # before the second attempt at a question, 6 s before the third, ...
FAILED_TEXT = "something went wrong"
FORBIDDEN = re.compile(r"\b403\b|forbidden", re.I)


class AgentUnavailable(RuntimeError):
    """The ontology agent cannot be used until an operator acts (not enabled for the capacity, token rejected, no token): asking more is pointless.
    str() tells the operator what to do; brief is the short first part, for a one-line report."""

    def __init__(self, message: str, brief: str = ""):
        super().__init__(message)
        self.brief = brief or message


class McpError(RuntimeError):
    """A request failed in a way that is worth asking again: connection closed, HTTP 429 or 5xx, tool error, empty answer, "Something went wrong"."""


def _short(text: Any, n: int = 160) -> str:
    """One printable ASCII line of at most n characters."""
    one = " ".join(str(text).split()).encode("ascii", "backslashreplace").decode("ascii")
    return one if len(one) <= n else one[: n - 3] + "..."


def _not_enabled(how: str, detail: str = "") -> AgentUnavailable:
    brief = f"the ontology agent is not available ({how})"
    return AgentUnavailable(
        f"the ontology agent is not available ({how}{': ' + detail if detail else ''}). Ask a Fabric or capacity admin to turn on the delegated capacity setting "
        "'Users can use Copilot and other features powered by Azure OpenAI' (called 'Users can use Copilot, AI Agents and other AI experiences powered by "
        "Azure OpenAI' in newer portals) under Admin portal > Capacity settings > the capacity > Delegated tenant settings; a capacity outside the US and EU "
        "also needs 'Data sent to Azure OpenAI can be processed outside your capacity's geographic region, compliance boundary, or national cloud instance'. "
        "A change takes about 10 minutes to propagate; then run the golden test again.", brief)


def _header(resp: Any, name: str) -> str | None:
    """A response header whatever its case (requests gives a case-insensitive dict, a test fake may not)."""
    for key, value in (getattr(resp, "headers", None) or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def _text(resp: Any) -> str:
    """The reply body as UTF-8: requests would decode text/event-stream without a charset as Latin-1 and garble the rupee sign."""
    raw = getattr(resp, "content", None)
    return raw.decode("utf-8-sig", "replace") if isinstance(raw, bytes) else (resp.text or "")


def _retry_after(resp: Any, attempt: int) -> float:
    """Seconds before retrying a 429 or 5xx reply: Retry-After when it is a number, else 2 s, 4 s, ... (at most 60 s)."""
    try:
        wait = float(_header(resp, "Retry-After") or 0)
    except ValueError:
        wait = 0.0
    return min(wait if wait > 0 else 2.0 * attempt, 60.0)


def _messages(text: str) -> list[dict]:
    """The JSON-RPC messages of a reply: one JSON document, or the data: events of a server-sent-events stream."""
    docs: list[Any] = []
    if text.startswith(("{", "[")):
        docs.append(json.loads(text))
    else:
        data: list[str] = []
        for line in [*re.split(r"\r\n|\r|\n", text), ""]:
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif not line and data:
                payload, data = "\n".join(data), []
                with contextlib.suppress(ValueError):
                    docs.append(json.loads(payload))
    return [m for d in docs for m in (d if isinstance(d, list) else [d]) if isinstance(m, dict)]


def _result(resp: Any, rid: int) -> dict:
    """The JSON-RPC result of request rid; a stream may also carry progress notifications, which are skipped."""
    text = _text(resp).strip()
    if not text:
        raise McpError("empty reply")
    try:
        msgs = [m for m in _messages(text) if "result" in m or "error" in m]
    except ValueError as ex:
        raise McpError(f"reply is not valid JSON: {_short(text, 100)}") from ex
    own = [m for m in msgs if m.get("id") == rid] or msgs
    if not own:
        raise McpError(f"no JSON-RPC result in the reply: {_short(text, 100)}")
    msg = own[-1]
    if "error" in msg:
        err = msg["error"]
        code, detail = (err.get("code"), err.get("message")) if isinstance(err, dict) else (None, err)
        if FORBIDDEN.search(str(detail)):
            raise _not_enabled("403 in the MCP error", _short(detail, 200))
        raise McpError(f"MCP error{'' if code is None else f' {code}'}: {_short(detail)}")
    return msg["result"] if isinstance(msg["result"], dict) else {}


class McpClient:
    """Minimal MCP client for one ontology endpoint: JSON-RPC 2.0 over HTTPS POST, replies as plain JSON or server-sent events.

    The handshake runs lazily on first use, once, however many threads ask; after an HTTP 404 on a call that carried a session id (the server dropped
    the session) the next call shakes hands again. A bearer token is requested for every call (tokens expire).
    post defaults to requests.post; sleeper is only used between retries of HTTP 429 and 5xx."""

    def __init__(self, url: str, token_fn: Callable[[str], str], post: Callable[..., Any] | None = None,
                 sleeper: Callable[[float], None] = time.sleep, timeout: tuple[float, float] = (30, 150)):
        self.url, self.token_fn, self.sleep, self.timeout = url, token_fn, sleeper, timeout
        self.post = requests.post if post is None else post
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._session: str | None = None
        self._ready = False

    def initialize(self) -> None:
        """MCP handshake (initialize, then notifications/initialized); done once, safe to call from several threads."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._session = None
            rid = next(self._ids)
            resp = self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "contoso-golden", "version": "1"}}})
            _result(resp, rid)
            self._session = _header(resp, "Mcp-Session-Id")
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            self._ready = True

    def tools_list(self) -> list[dict]:
        """The tools the endpoint offers (name, description, inputSchema)."""
        return list(self._rpc("tools/list", {}).get("tools") or [])

    def call(self, name: str, arguments: dict | None = None) -> str:
        """tools/call: the text of the result; McpError when the tool reports an error."""
        text, is_error = self._call(name, arguments)
        if is_error:
            raise McpError(f"{name} reported an error: {_short(text) or 'no detail'}")
        return text

    def ask(self, question: str) -> str:
        """The ontology agent's answer text. McpError for a tool error, an empty answer or the service's "Something went wrong" reply;
        AgentUnavailable when the agent is not enabled (HTTP 401 or 403, or a tool error that says 403)."""
        text, is_error = self._call(TOOL, {"request": question})
        if is_error and FORBIDDEN.search(text):
            raise _not_enabled("403 in the tool error", _short(text, 200))
        if is_error:
            raise McpError(f"{TOOL} reported an error: {_short(text) or 'no detail'}")
        if not text.strip():
            raise McpError("empty answer")
        if FAILED_TEXT in text[:200].lower():
            raise McpError(f"service error: {_short(text)}")
        return text

    def _call(self, name: str, arguments: dict | None) -> tuple[str, bool]:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(str(p.get("text") or "") for p in result.get("content") or [] if isinstance(p, dict))
        return text, bool(result.get("isError"))

    def _rpc(self, method: str, params: dict) -> dict:
        self.initialize()
        rid = next(self._ids)
        return _result(self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}), rid)

    def _post(self, body: dict) -> Any:
        """POST one JSON-RPC message. HTTP 429 and 5xx are retried with backoff; 401 and 403 mean the agent is unusable; other errors are McpError."""
        for attempt in itertools.count(1):
            try:
                token = self.token_fn(oac.FABRIC_SCOPE)
            except Exception as ex:  # noqa: BLE001 - whatever the token source says, nothing can be asked without a token
                raise AgentUnavailable(f"no Fabric access token: {_short(ex, 300)}", "no Fabric access token") from ex
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if self._session:
                headers["Mcp-Session-Id"] = self._session
            try:
                resp = self.post(self.url, headers=headers, json=body, timeout=self.timeout)
            except (OSError, http.client.HTTPException) as ex:
                raise McpError(f"connection failed: {type(ex).__name__}: {_short(ex, 120)}") from ex
            status = resp.status_code
            if status in (401, 403):
                detail = _short(_text(resp), 200)
                if status == 403:
                    raise _not_enabled("HTTP 403", detail)
                raise AgentUnavailable(f"the ontology MCP endpoint rejected the access token (HTTP 401{': ' + detail if detail else ''}); "
                                       "sign in again with az login on this host and run the golden test again", "the access token was rejected (HTTP 401)")
            if (status == 429 or status >= 500) and attempt < HTTP_TRIES:
                self.sleep(_retry_after(resp, attempt))
                continue
            if status == 404 and self._session:
                self._ready = False                         # the server dropped the session: the next call shakes hands again
            if status >= 400:
                raise McpError(f"HTTP {status}: {_short(_text(resp))}")
            return resp


def build_client(ws_id: str, ontology_id: str, token_fn: Callable[[str], str] | None = None) -> McpClient:
    """Client for the MCP endpoint of a deployed ontology; tokens come from the signed-in az context unless token_fn is given."""
    return McpClient(agents_mod.FABRIC_MCP.format(ws=ws_id, ont=ontology_id), token_fn or oac.default_token_fn())


def score_answer(g: dict, text: str) -> tuple[bool, str]:
    """(passed, reason) by the rule of `verify --answers`: every expected value appears in the text (oracles.compare), every must_include
    string is there and no must_not_include string is. The reason is empty when the answer passes."""
    res = oracles.compare(g["expected"], text)
    low = text.lower()
    miss = [m for m in g.get("must_include") or [] if m.lower() not in low] + [m for m in g.get("must_not_include") or [] if m.lower() in low]
    passed = res["pass"] and not miss
    return passed, "" if passed else f"missing={res['missing']} guard={miss}"


def select_ids(golden: list[dict], mode: str) -> list[str]:
    """Question ids to ask through MCP (golden.yaml entries or golden_expected.json rows: only id, surfaces and tags are read).
    none: nothing. full: every question whose surfaces include mcp. smoke: those of them tagged smoke, or the first 5 when none is tagged."""
    if mode == "none":
        return []
    if mode not in ("smoke", "full"):
        raise ValueError(f"unknown golden mode {mode!r}: use none, smoke or full")
    mcp = [g for g in golden if "mcp" in (g.get("surfaces") or [])]
    if mode == "smoke":
        mcp = [g for g in mcp if "smoke" in (g.get("tags") or [])] or mcp[:5]
    return [g["id"] for g in mcp]


def _ids(ids: Iterable[str]) -> str:
    ids = list(ids)
    return f" ({', '.join(ids)})" if ids else ""


@dataclass
class GoldenReport:
    """What one run found. asked = answered + len(errored); answered = passed + len(failed).
    failed holds (id, reason) for wrong answers, errored the ids that never produced an answer, skipped the ids not asked because the agent is
    unavailable (unavailable carries the operator's instructions); answers maps id to the answer text, seconds is the wall time of the run."""

    asked: int = 0
    answered: int = 0
    passed: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    errored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unavailable: str | None = None
    answers: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not (self.failed or self.errored or self.unavailable)

    def summary(self) -> str:
        """One line, for example: golden: 4/5 passed, 1 failed (G03), 0 errored."""
        failed = [x if isinstance(x, str) else x[0] for x in self.failed]
        line = f"golden: {self.passed}/{self.asked} passed, {len(failed)} failed{_ids(failed)}, {len(self.errored)} errored{_ids(self.errored)}"
        return line + (f"; agent unavailable, {len(self.skipped)} not asked" if self.unavailable else "")


@dataclass
class _Outcome:
    gid: str
    status: str                     # PASS, FAIL, ERROR or SKIPPED
    text: str = ""
    reason: str = ""
    seconds: float = 0.0
    tries: int = 0
    unavailable: str = ""


def _ask_one(client: McpClient, g: dict | None, gid: str, stop: threading.Event, attempts: int, sleeper: Callable[[float], None]) -> _Outcome:
    if g is None or "expected" not in g:
        return _Outcome(gid, "ERROR", reason=f"no expected answer: {(g or {}).get('error') or 'the id is not in the expected answers'}")
    if stop.is_set():
        return _Outcome(gid, "SKIPPED")
    started, reason, tries = time.monotonic(), "", 0
    while tries < attempts and not stop.is_set():
        tries += 1
        try:
            text = client.ask(g["question"])
        except AgentUnavailable as ex:
            stop.set()
            return _Outcome(gid, "ERROR", reason=ex.brief, seconds=time.monotonic() - started, tries=tries, unavailable=str(ex))
        except Exception as ex:  # noqa: BLE001 - a failed attempt is retried, and reported when the last one fails too
            reason = str(ex) if isinstance(ex, McpError) else f"{type(ex).__name__}: {ex}"
            if tries < attempts and not stop.is_set():
                sleeper(BACKOFF_S * tries)
            continue
        passed, why = score_answer(g, text)
        return _Outcome(gid, "PASS" if passed else "FAIL", text, why, time.monotonic() - started, tries)
    return _Outcome(gid, "ERROR", reason=reason, seconds=time.monotonic() - started, tries=tries)


def _line(out: _Outcome, g: dict) -> str:
    notes = [f"{out.seconds:.0f}s"]
    if out.tries > 1:
        notes.append(f"{out.tries} tries")
    if out.status in ("PASS", "FAIL") and g.get("judge") == "manual":
        notes.append("judge manually")
    return f"  {out.gid}: {out.status} ({', '.join(notes)})" + (f" {_short(out.reason, 200)}" if out.reason else "")


def run_golden(client: McpClient, expected: list[dict], ids: list[str], *, parallel: int = 2, attempts: int = 2,
               say: Callable[[str], None] = print, sleeper: Callable[[float], None] = time.sleep) -> GoldenReport:
    """Ask the golden questions `ids` through client and score each answer against its row in `expected` (golden_expected.json).

    Up to `parallel` questions are in flight. A question that fails (connection closed, empty or error answer) is asked again, up to `attempts`
    times in all, after a short backoff. Once the agent proves unavailable nothing more is asked and report.unavailable says why. One short line per
    question goes to `say` (never the answer text, which is kept in report.answers). Nothing is raised for a single question."""
    by_id = {g["id"]: g for g in expected}
    order = list(dict.fromkeys(ids))
    report, outcomes, stop, started = GoldenReport(), {}, threading.Event(), time.monotonic()
    if order:
        pool = ThreadPoolExecutor(max_workers=max(1, min(parallel, len(order))))
        try:
            futures = {pool.submit(_ask_one, client, by_id.get(gid), gid, stop, max(1, attempts), sleeper): gid for gid in order}
            for fut in as_completed(futures):
                gid = futures[fut]
                try:
                    out = fut.result()
                except Exception as ex:  # noqa: BLE001 - one broken question must not end the run
                    out = _Outcome(gid, "ERROR", reason=f"{type(ex).__name__}: {ex}")
                outcomes[gid] = out
                if out.unavailable and not report.unavailable:
                    report.unavailable = out.unavailable
                if out.status != "SKIPPED":
                    say(_line(out, by_id.get(gid, {})))
        finally:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
    for gid in order:
        out = outcomes[gid]
        if out.status == "SKIPPED":
            report.skipped.append(gid)
            continue
        report.asked += 1
        if out.status == "ERROR":
            report.errored.append(gid)
            continue
        report.answered += 1
        report.answers[gid] = out.text
        if out.status == "PASS":
            report.passed += 1
        else:
            report.failed.append((gid, out.reason))
    report.seconds = time.monotonic() - started
    return report
