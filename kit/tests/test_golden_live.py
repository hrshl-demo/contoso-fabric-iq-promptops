"""Golden questions through the live ontology MCP endpoint: the MCP client, answer scoring, question selection and the run loop.
Everything runs against fakes: no network and no real backoff sleeping."""
import http.client
import json
import re
import threading
import time
from collections import namedtuple

import pytest
import requests

from contoso_onto import agents, golden_live as G, oac, oracles

URL = "https://api.fabric.test/v1/mcp/dataPlane/workspaces/ws-1/items/ont-1/ontologyEndpoint"
WENT_WRONG = "Something went wrong while processing the Fabric ontology request. Please try again."
SETTING = "Users can use Copilot and other features powered by Azure OpenAI"

# rows shaped like out/eval/golden_expected.json (oracles.run_oracles)
EXPECTED = [
    {"id": "G01", "tier": "core", "surfaces": ["data_agent", "mcp"], "tags": ["authority", "money"], "expected": 20000.0,
     "question": "What is the most Rhea's agent mandate may move in a single action?"},
    {"id": "G04", "tier": "core", "surfaces": ["data_agent", "mcp", "voice"], "tags": ["authority"], "must_include": ["revoked"],
     "expected": {"status": "revoked", "revoked_on": "2026-09-10"}, "question": "Is Rhea's mandate still active, and if not when was it revoked?"},
    {"id": "G06", "tier": "core", "surfaces": ["data_agent", "mcp", "voice"], "tags": ["balances", "voice"],
     "expected": {"balance_inr": 1008300.0, "as_on": "2026-09-30"}, "question": "What is the balance in Rhea's primary account and as of which date?"},
    {"id": "G12", "tier": "core", "surfaces": ["data_agent"], "tags": ["transactions"], "expected": {"count": 120, "debits": 80},
     "question": "How many transactions does Rhea have, and how much went out and came in?"},
    {"id": "G25", "tier": "core", "surfaces": ["data_agent", "mcp"], "tags": ["balances", "guardrail"], "must_not_include": ["goal balances added"],
     "expected": {"assets_inr": 1493300.0, "liabilities_inr": 5981092.13}, "question": "What is Rhea's net position? Mention anything that is not included."},
    {"id": "G27", "tier": "core", "surfaces": ["data_agent", "mcp"], "tags": ["identity", "guardrail"], "judge": "manual", "must_include": ["superseded"],
     "expected": {"superseded_by": "rhea"}, "question": "Is the older Rhea record the same person, and should its balances be added to Rhea's?"},
]
ROW = {g["id"]: g for g in EXPECTED}
Q = {gid: g["question"] for gid, g in ROW.items()}
GOOD = {
    "G01": "Rhea's agent mandate can move up to INR 20,000 in a single action.",
    "G04": "The mandate is not active: it was revoked on 2026-09-10T13:00:00Z.",
    "G06": "The primary account holds INR 10,08,300 as of 2026-09-30.",
    "G25": "Assets are INR 14,93,300 and liabilities INR 59,81,092.13; legacy records are not included.",
    "G27": "Yes, rhea~legacy is the same person but it is superseded by rhea; do not add the balances.",
}


# ---------------------------------------------------------------------------------------------- fakes
Call = namedtuple("Call", "method headers body timeout")


def rpc(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def tool_result(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error, "_meta": {"conversationId": "c-1"}}


class Resp:
    """The attributes of requests.Response that the client reads. text decodes an event stream as Latin-1, as requests does without a charset."""

    def __init__(self, status=200, payload=None, *, sse=False, headers=None, raw=None):
        body = raw if raw is not None else ("" if payload is None else json.dumps(payload))
        if sse and raw is None and payload is not None:
            body = f"event: message\ndata: {body}\n\n"
        self.status_code = status
        self.headers = {"Content-Type": "text/event-stream" if sse else "application/json", **(headers or {})}
        self.content = body.encode("utf-8")
        self.text = self.content.decode("latin-1" if sse else "utf-8")


class FakeMcp:
    """Stands in for requests.post against the MCP endpoint. script maps a question (or a tool name, or "initialize") to its replies in order; the last
    one repeats. A str is an answer text, an exception is raised, a Resp is returned as it is, any other callable gets the request id and returns a Resp."""

    def __init__(self, script=None, *, sse=False, session="sess-1", init_delay=0.0):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.sse, self.session, self.init_delay = sse, session, init_delay
        self.calls, self.lock = [], threading.Lock()

    def of(self, method):
        return [c for c in self.calls if c.method == method]

    def step(self, key):
        with self.lock:
            replies = self.script.get(key)
            return None if not replies else (replies.pop(0) if len(replies) > 1 else replies[0])

    def __call__(self, url, headers=None, json=None, timeout=None):
        method, rid = json["method"], json.get("id")
        with self.lock:
            self.calls.append(Call(method, dict(headers), json, timeout))
        if method == "notifications/initialized":
            return Resp(202, raw="")
        if method == "initialize":
            step = self.step("initialize")
            if isinstance(step, BaseException):
                raise step
            time.sleep(self.init_delay)
            return Resp(payload=rpc(rid, {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}), sse=self.sse,
                        headers={"Mcp-Session-Id": self.session} if self.session else {})
        if method == "tools/list":
            return Resp(payload=rpc(rid, {"tools": [{"name": "ask_ontology"}, {"name": "list_ontology_entities"}]}), sse=self.sse)
        params = json["params"]
        step = self.step(params["arguments"]["request"] if params["name"] == "ask_ontology" else params["name"])
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Resp):
            return step
        if callable(step):
            return step(rid)
        return Resp(payload=rpc(rid, tool_result(step)), sse=self.sse)


class Tokens:
    def __init__(self):
        self.scopes = []

    def __call__(self, scope):
        self.scopes.append(scope)
        return f"tok-{len(self.scopes)}"


def make_client(fake, **kw):
    slept, tokens = [], Tokens()
    return G.McpClient(URL, tokens, post=fake, sleeper=slept.append, **kw), slept, tokens


def tool_error(text):
    return lambda rid: Resp(payload=rpc(rid, tool_result(text, error=True)))


def run(script, ids, *, expected=EXPECTED, **kw):
    fake = FakeMcp(script)
    client, _, _ = make_client(fake)
    lines, slept = [], []
    report = G.run_golden(client, expected, ids, say=lines.append, sleeper=slept.append, **kw)
    return report, lines, slept, fake


def timeless(lines):
    return [re.sub(r"\(\d+s", "(Ns", ln) for ln in lines]


class FakeClient:
    """Duck-typed McpClient for the run loop: the answer is a function of the question, every question asked is recorded."""

    def __init__(self, answer):
        self.answer, self.asked, self.lock = answer, [], threading.Lock()

    def ask(self, question):
        with self.lock:
            self.asked.append(question)
        return self.answer(question)


def numbered(n):
    return [{"id": f"Q{i:02d}", "question": f"question {i}", "expected": float(i * 1000)} for i in range(1, n + 1)]


def answer_by_number(question):
    return f"INR {int(question.split()[1]) * 1000:,}"


# ---------------------------------------------------------------------------------------------- the client: parsing and handshake
@pytest.mark.parametrize("sse", [False, True], ids=["json", "event-stream"])
def test_ask_returns_the_answer_text_from_json_or_an_event_stream(sse):
    client, _, _ = make_client(FakeMcp({"q": ["Rhea's mandate can move INR 20,000 per action."]}, sse=sse))
    assert client.ask("q") == "Rhea's mandate can move INR 20,000 per action."


def test_ask_sends_the_question_as_the_request_argument_of_ask_ontology():
    fake = FakeMcp({"q": ["a"]})
    make_client(fake)[0].ask("q")
    body = fake.of("tools/call")[0].body
    assert body["jsonrpc"] == "2.0" and body["method"] == "tools/call" and body["params"] == {"name": "ask_ontology", "arguments": {"request": "q"}}


def test_event_stream_with_comments_progress_events_crlf_and_multiline_data():
    answer = "Total: INR 1,443,300\nAs of 2026-09-30"

    def stream(rid):
        progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1, "total": 2}}
        pretty = json.dumps(rpc(rid, tool_result(answer)), indent=1)
        data = "\r\n".join("data: " + ln for ln in pretty.splitlines())
        return Resp(sse=True, raw=f": keep-alive\r\n\r\nevent: message\r\ndata: {json.dumps(progress)}\r\n\r\nevent: message\r\n{data}\r\n\r\n")

    assert make_client(FakeMcp({"q": [stream]}))[0].ask("q") == answer


def test_an_event_stream_that_ends_without_a_blank_line_is_still_read():
    raw = lambda rid: Resp(sse=True, raw="data: " + json.dumps(rpc(rid, tool_result("INR 20,000"))))  # noqa: E731
    assert make_client(FakeMcp({"q": [raw]}))[0].ask("q") == "INR 20,000"


def test_the_answer_is_picked_by_request_id_when_the_stream_has_other_results():
    def stream(rid):
        other = json.dumps(rpc(rid + 99, tool_result("somebody else")))
        mine = json.dumps(rpc(rid, tool_result("INR 20,000")))
        return Resp(sse=True, raw=f"data: {mine}\n\ndata: {other}\n\n")

    assert make_client(FakeMcp({"q": [stream]}))[0].ask("q") == "INR 20,000"


def test_the_rupee_sign_survives_an_event_stream_without_a_charset():
    raw = lambda rid: Resp(sse=True, raw="data: " + json.dumps(rpc(rid, tool_result("EMI \u20b948,500")), ensure_ascii=False) + "\n\n")  # noqa: E731
    assert raw(1).text != "EMI \u20b948,500" and "\u20b9" not in raw(1).text                      # the fake decodes like requests would
    assert make_client(FakeMcp({"q": [raw]}))[0].ask("q") == "EMI \u20b948,500"


def test_handshake_order_session_header_and_a_fresh_token_for_every_request():
    fake = FakeMcp({"q": ["INR 20,000"]})
    client, _, tokens = make_client(fake)
    assert client.ask("q") == client.ask("q") == "INR 20,000"
    assert [c.method for c in fake.calls] == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    init, note, first, second = fake.calls
    assert "Mcp-Session-Id" not in init.headers
    assert note.headers["Mcp-Session-Id"] == first.headers["Mcp-Session-Id"] == second.headers["Mcp-Session-Id"] == "sess-1"
    assert [c.headers["Authorization"] for c in fake.calls] == ["Bearer tok-1", "Bearer tok-2", "Bearer tok-3", "Bearer tok-4"]
    assert tokens.scopes == [oac.FABRIC_SCOPE] * 4
    assert init.headers["Accept"] == "application/json, text/event-stream" and init.timeout == (30, 150)
    assert init.body["params"]["protocolVersion"] == "2025-06-18" and "id" in init.body and "id" not in note.body


def test_session_header_is_found_whatever_its_case_and_absent_means_no_session():
    fake = FakeMcp({"q": ["a"]})
    original = fake.__call__

    def lower_case(url, headers=None, json=None, timeout=None):
        resp = original(url, headers=headers, json=json, timeout=timeout)
        if resp.headers.get("Mcp-Session-Id"):
            resp.headers = {"mcp-session-id": resp.headers["Mcp-Session-Id"]}
        return resp

    G.McpClient(URL, Tokens(), post=lower_case, sleeper=lambda s: None).ask("q")
    assert fake.of("tools/call")[0].headers["Mcp-Session-Id"] == "sess-1"
    stateless = FakeMcp({"q": ["a"]}, session="")
    make_client(stateless)[0].ask("q")
    assert "Mcp-Session-Id" not in stateless.of("tools/call")[0].headers


def test_handshake_runs_once_for_many_threads():
    fake = FakeMcp({"q": ["INR 20,000"]}, init_delay=0.03)
    client, _, _ = make_client(fake)
    start, answers = threading.Barrier(8, timeout=10), []

    def worker():
        start.wait()
        answers.append(client.ask("q"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert answers == ["INR 20,000"] * 8
    assert len(fake.of("initialize")) == 1 and len(fake.of("notifications/initialized")) == 1
    assert {c.headers["Mcp-Session-Id"] for c in fake.of("tools/call")} == {"sess-1"}


def test_a_failed_handshake_is_not_remembered():
    fake = FakeMcp({"initialize": [requests.ConnectionError("Connection aborted."), "go"], "q": ["INR 20,000"]})
    client, _, _ = make_client(fake)
    with pytest.raises(G.McpError, match="connection failed"):
        client.ask("q")
    assert client.ask("q") == "INR 20,000"
    inits = fake.of("initialize")
    assert len(inits) == 2 and all("Mcp-Session-Id" not in c.headers for c in inits)


def test_a_404_for_a_known_session_means_the_next_call_starts_a_new_session():
    fake = FakeMcp({"q": [Resp(404, raw="session not found"), "INR 20,000"]})
    client, _, _ = make_client(fake)
    with pytest.raises(G.McpError, match="HTTP 404: session not found"):
        client.ask("q")
    assert client.ask("q") == "INR 20,000"
    inits = fake.of("initialize")
    assert len(inits) == 2 and "Mcp-Session-Id" not in inits[1].headers and fake.of("tools/call")[1].headers["Mcp-Session-Id"] == "sess-1"


def test_a_404_from_a_server_without_sessions_does_not_trigger_new_handshakes():
    fake = FakeMcp({"q": [Resp(404, raw="no such tool")]}, session="")
    client, _, _ = make_client(fake)
    for _ in range(2):
        with pytest.raises(G.McpError, match="HTTP 404"):
            client.ask("q")
    assert len(fake.of("initialize")) == 1


def test_tools_list_and_call():
    fake = FakeMcp({"list_ontology_entities": ['{"values": [{"name": "Customer"}]}'], "broken": [tool_error("boom")]})
    client, _, _ = make_client(fake)
    assert [t["name"] for t in client.tools_list()] == ["ask_ontology", "list_ontology_entities"]
    assert json.loads(client.call("list_ontology_entities")) == {"values": [{"name": "Customer"}]}
    assert fake.of("tools/call")[0].body["params"] == {"name": "list_ontology_entities", "arguments": {}}
    with pytest.raises(G.McpError, match="broken reported an error: boom"):
        client.call("broken", {"x": 1})


def test_the_default_transport_is_requests_post():
    assert G.McpClient(URL, Tokens()).post is requests.post


# ---------------------------------------------------------------------------------------------- the client: failures
@pytest.mark.parametrize("step,fragment", [
    pytest.param("", "empty answer", id="empty-text"),
    pytest.param("  \n", "empty answer", id="blank-text"),
    pytest.param(WENT_WRONG, "service error: Something went wrong", id="something-went-wrong"),
    pytest.param("Error: something WENT wrong, try later", "service error", id="something-went-wrong-wrapped"),
    pytest.param(tool_error("backend hiccup"), "ask_ontology reported an error: backend hiccup", id="is-error"),
    pytest.param(tool_error(""), "reported an error: no detail", id="is-error-without-text"),
    pytest.param(lambda rid: Resp(payload={"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error"}}),
                 "MCP error -32603: Internal error", id="json-rpc-error"),
    pytest.param(Resp(400, raw='{"error": "bad request"}'), "HTTP 400: ", id="http-400"),
    pytest.param(Resp(raw=""), "empty reply", id="empty-body"),
    pytest.param(Resp(raw='{"jsonrpc": "2.0", "id": 2, "res'), "not valid JSON", id="truncated-json"),
    pytest.param(Resp(raw="<html>Bad gateway</html>"), "no JSON-RPC result", id="html"),
    pytest.param(Resp(sse=True, raw=": keep-alive\n\n"), "no JSON-RPC result", id="stream-without-result"),
    pytest.param(requests.ConnectionError("Connection aborted."), "connection failed: ConnectionError", id="connection-error"),
    pytest.param(http.client.RemoteDisconnected("Remote end closed connection without response"), "connection failed: RemoteDisconnected", id="remote-disconnected"),
    pytest.param(ConnectionResetError(10054, "forcibly closed"), "connection failed: ConnectionResetError", id="connection-reset"),
    pytest.param(http.client.IncompleteRead(b"abc"), "connection failed: IncompleteRead", id="incomplete-read"),
    pytest.param(requests.ReadTimeout("read timed out"), "connection failed: ReadTimeout", id="read-timeout"),
])
def test_ask_raises_a_clear_error_when_the_call_produced_no_answer(step, fragment):
    client, _, _ = make_client(FakeMcp({"q": [step]}))
    with pytest.raises(G.McpError, match=re.escape(fragment)):
        client.ask("q")


def test_http_403_means_the_agent_is_not_enabled_and_is_never_retried():
    fake = FakeMcp({"q": [Resp(403, raw='{"errorCode": "Forbidden"}')]})
    client, slept, _ = make_client(fake)
    with pytest.raises(G.AgentUnavailable) as err:
        client.ask("q")
    msg = str(err.value)
    assert "HTTP 403" in msg and "Forbidden" in msg and SETTING in msg and "10 minutes" in msg and "delegated capacity setting" in msg.lower()
    assert slept == [] and len(fake.of("tools/call")) == 1
    assert not isinstance(err.value, G.McpError) and "\n" not in msg
    assert err.value.brief == "the ontology agent is not available (HTTP 403)" and msg.startswith("the ontology agent is not available (HTTP 403: ")


@pytest.mark.parametrize("step", [
    pytest.param(tool_error("Request failed with status code 403 (Forbidden)"), id="tool-error"),
    pytest.param(lambda rid: Resp(payload={"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Forbidden"}}), id="json-rpc-error"),
])
def test_a_403_reported_inside_a_200_reply_means_the_same(step):
    client, slept, _ = make_client(FakeMcp({"q": [step]}))
    with pytest.raises(G.AgentUnavailable, match="403 in the"):
        client.ask("q")
    assert slept == []


def test_a_rejected_or_missing_token_is_unavailable_too():
    client, _, _ = make_client(FakeMcp({"q": [Resp(401, raw="invalid audience")]}))
    with pytest.raises(G.AgentUnavailable, match=r"rejected the access token \(HTTP 401: invalid audience\).*az login") as err:
        client.ask("q")
    assert err.value.brief == "the access token was rejected (HTTP 401)"
    fake = FakeMcp({"q": ["x"]})

    def no_token(scope):
        raise RuntimeError("az could not issue a token for https://api.fabric.microsoft.com: not signed in")

    with pytest.raises(G.AgentUnavailable, match="no Fabric access token: az could not issue a token") as err:
        G.McpClient(URL, no_token, post=fake, sleeper=lambda s: None).ask("q")
    assert err.value.brief == "no Fabric access token" and fake.calls == []
    assert G.AgentUnavailable("just this").brief == "just this"


@pytest.mark.parametrize("first,waits", [
    pytest.param([Resp(429, headers={"Retry-After": "7"}, raw="slow down")], [7.0], id="429-retry-after"),
    pytest.param([Resp(503, raw="")], [2.0], id="503"),
    pytest.param([Resp(500, raw=""), Resp(502, raw="")], [2.0, 4.0], id="500-then-502"),
    pytest.param([Resp(429, headers={"retry-after": "soon"}, raw="")], [2.0], id="retry-after-not-a-number"),
    pytest.param([Resp(429, headers={"Retry-After": "900"}, raw="")], [60.0], id="retry-after-capped"),
])
def test_http_429_and_5xx_are_retried_with_backoff_then_succeed(first, waits):
    fake = FakeMcp({"q": [*first, "INR 20,000"]})
    client, slept, _ = make_client(fake)
    assert client.ask("q") == "INR 20,000"
    assert slept == waits and len(fake.of("tools/call")) == len(first) + 1


def test_http_errors_that_persist_give_up_after_three_tries():
    fake = FakeMcp({"q": [Resp(503, raw="service unavailable")]})
    client, slept, _ = make_client(fake)
    with pytest.raises(G.McpError, match="HTTP 503: service unavailable"):
        client.ask("q")
    assert len(fake.of("tools/call")) == G.HTTP_TRIES == 3 and slept == [2.0, 4.0]


# ---------------------------------------------------------------------------------------------- scoring
def test_score_answer_passes_a_correct_answer_in_any_number_format():
    assert G.score_answer(ROW["G01"], "Up to INR 20,000 per action") == (True, "")
    assert G.score_answer(ROW["G01"], "Rs 20000") == (True, "")
    assert G.score_answer(ROW["G01"], "\u20b920k") == (True, "")
    for gid, text in GOOD.items():
        assert G.score_answer(ROW[gid], text) == (True, ""), gid


def test_score_answer_fails_a_wrong_number_a_missing_string_and_a_guard_with_the_verify_wording():
    assert G.score_answer(ROW["G01"], "It can move INR 25,000 per action.") == (False, "missing=[20000.0] guard=[]")
    assert G.score_answer(ROW["G04"], "The mandate is still active (since 2026-09-10).") == (False, "missing=['revoked'] guard=['revoked']")
    assert G.score_answer(ROW["G25"], GOOD["G25"] + " The goal balances added are not shown.") == (False, "missing=[] guard=['goal balances added']")
    assert G.score_answer(ROW["G27"], "It is the same person, a legacy copy.") == (False, "missing=['rhea'] guard=['superseded']")


def test_score_answer_agrees_with_what_verify_does_for_every_row():
    for gid, text in {**GOOD, "G01": "INR 1", "G06": "nothing"}.items():
        g = ROW[gid]
        res = oracles.compare(g["expected"], text)
        miss = [m for m in g.get("must_include", []) if m.lower() not in text.lower()] + [m for m in g.get("must_not_include", []) if m.lower() in text.lower()]
        assert G.score_answer(g, text)[0] == (res["pass"] and not miss), gid


# ---------------------------------------------------------------------------------------------- selecting the questions
GOLDEN = [
    {"id": "A1", "surfaces": ["data_agent", "mcp"], "tags": ["money"]},
    {"id": "A2", "surfaces": ["data_agent"], "tags": ["smoke"]},
    {"id": "A3", "surfaces": ["mcp", "voice"], "tags": ["smoke", "authority"]},
    {"id": "A4", "surfaces": ["mcp"]},
    {"id": "A5", "tags": ["smoke"]},
    {"id": "A6", "surfaces": ["mcp"], "tags": ["smoke"]},
]


def test_select_ids_none_smoke_and_full():
    assert G.select_ids(GOLDEN, "none") == []
    assert G.select_ids(GOLDEN, "full") == ["A1", "A3", "A4", "A6"]
    assert G.select_ids(GOLDEN, "smoke") == ["A3", "A6"]                                           # tagged smoke AND asked through mcp
    assert G.select_ids([], "smoke") == G.select_ids([], "full") == []


def test_smoke_falls_back_to_the_first_five_mcp_questions_when_none_is_tagged():
    rows = [{"id": f"M{i}", "surfaces": ["mcp"], "tags": ["money"]} for i in range(8)]
    rows.insert(1, {"id": "D1", "surfaces": ["data_agent"], "tags": ["smoke"]})                    # tagged, but not an mcp question: no tagged mcp question
    assert G.select_ids(rows, "smoke") == ["M0", "M1", "M2", "M3", "M4"]
    assert G.select_ids(rows[:4], "smoke") == ["M0", "M1", "M2"]
    assert len(G.select_ids(rows, "full")) == 8


def test_select_ids_works_on_expected_rows_and_rejects_unknown_modes():
    assert G.select_ids(EXPECTED, "full") == ["G01", "G04", "G06", "G25", "G27"]
    for bad in ("bogus", "", "FULL", None):
        with pytest.raises(ValueError, match="unknown golden mode"):
            G.select_ids(GOLDEN, bad)


# ---------------------------------------------------------------------------------------------- the run loop
@pytest.mark.parametrize("bad", [
    pytest.param(requests.ConnectionError("Connection aborted.", http.client.RemoteDisconnected("Remote end closed connection without response")), id="connection-closed"),
    pytest.param(http.client.RemoteDisconnected("Remote end closed connection without response"), id="remote-disconnected"),
    pytest.param(ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"), id="connection-reset"),
    pytest.param("", id="empty-text"),
    pytest.param(WENT_WRONG, id="something-went-wrong"),
    pytest.param(tool_error("backend hiccup"), id="is-error"),
])
def test_a_first_attempt_that_fails_is_asked_again_after_a_short_backoff(bad):
    report, lines, slept, fake = run({Q["G01"]: [bad, GOOD["G01"]]}, ["G01"])
    assert report.ok and report.passed == 1 and report.asked == report.answered == 1 and report.answers == {"G01": GOOD["G01"]}
    assert len(fake.of("tools/call")) == 2 and slept == [G.BACKOFF_S]
    assert timeless(lines) == ["  G01: PASS (Ns, 2 tries)"]


def test_a_question_that_fails_every_attempt_is_errored_and_nothing_is_raised():
    report, lines, slept, fake = run({Q["G01"]: [requests.ConnectionError("Connection aborted.")], Q["G04"]: [GOOD["G04"]]}, ["G01", "G04"], parallel=1)
    assert report.errored == ["G01"] and report.passed == 1 and report.asked == 2 and report.answered == 1 and not report.ok
    assert len(fake.of("tools/call")) == 3 and slept == [G.BACKOFF_S]                              # G01 twice, G04 once
    assert timeless(lines)[0].startswith("  G01: ERROR (Ns, 2 tries) connection failed: ConnectionError") and timeless(lines)[1] == "  G04: PASS (Ns)"
    assert "G01" not in report.answers


def test_attempts_is_the_total_number_of_asks_and_backoff_grows():
    report, _, slept, fake = run({Q["G01"]: [WENT_WRONG]}, ["G01"], attempts=3)
    assert report.errored == ["G01"] and len(fake.of("tools/call")) == 3 and slept == [G.BACKOFF_S, G.BACKOFF_S * 2]
    report, _, slept, fake = run({Q["G01"]: [WENT_WRONG]}, ["G01"], attempts=1)
    assert report.errored == ["G01"] and len(fake.of("tools/call")) == 1 and slept == []
    report, _, _, fake = run({Q["G01"]: [WENT_WRONG]}, ["G01"], attempts=0)                        # never less than one ask
    assert report.errored == ["G01"] and len(fake.of("tools/call")) == 1


def test_a_wrong_answer_is_failed_not_retried_and_scoring_matches_verify():
    report, lines, slept, fake = run({Q["G01"]: ["It can move INR 25,000 per action."], Q["G04"]: ["It is still active (2026-09-10)."]}, ["G01", "G04"], parallel=1)
    assert report.failed == [("G01", "missing=[20000.0] guard=[]"), ("G04", "missing=['revoked'] guard=['revoked']")]
    assert report.passed == 0 and report.answered == report.asked == 2 and report.errored == [] and not report.ok
    assert len(fake.of("tools/call")) == 2 and slept == []
    assert report.answers["G01"] == "It can move INR 25,000 per action."


def test_one_short_line_per_question_and_never_the_answer_text():
    marker = "ANSWER-BODY-MARKER"
    script = {Q["G01"]: [f"{marker} " * 40 + "It can move INR 25,000."], Q["G04"]: [GOOD["G04"]], Q["G06"]: [GOOD["G06"]], Q["G27"]: [GOOD["G27"]]}
    report, lines, _, _ = run(script, ["G01", "G04", "G06", "G27"], parallel=1)
    assert timeless(lines) == ["  G01: FAIL (Ns) missing=[20000.0] guard=[]", "  G04: PASS (Ns)", "  G06: PASS (Ns)", "  G27: PASS (Ns, judge manually)"]
    assert marker not in " ".join(lines) and max(len(ln) for ln in lines) < 100
    assert report.summary() == "golden: 3/4 passed, 1 failed (G01), 0 errored" and report.seconds >= 0
    assert marker in report.answers["G01"] and set(report.answers) == {"G01", "G04", "G06", "G27"}


def test_long_reasons_are_cut_and_made_printable():
    row = {"id": "G90", "question": "q", "expected": ["Ren\u00e9e " + "x" * 300]}
    report, lines, _, _ = run({"q": ["nothing useful"]}, ["G90"], expected=[row])
    assert len(lines[0]) < 260 and lines[0].isascii() and lines[0].endswith("...")
    assert report.failed[0][1].startswith("missing=['Ren\u00e9e ")                                  # the report keeps the full reason


def test_403_stops_the_run_and_the_remaining_questions_are_not_asked():
    ids = ["G01", "G04", "G06", "G25", "G27"]
    script = {Q[i]: [Resp(403, raw="Forbidden")] for i in ids}
    report, lines, slept, fake = run(script, ids, parallel=1)
    assert len(fake.of("tools/call")) == 1 and slept == []
    assert report.unavailable and SETTING in report.unavailable and "10 minutes" in report.unavailable
    assert report.errored == ["G01"] and report.skipped == ["G04", "G06", "G25", "G27"] and report.asked == 1 and report.passed == 0 and not report.ok
    assert len(lines) == 1 and lines[0].startswith("  G01: ERROR") and "HTTP 403" in lines[0] and len(lines[0]) < 100 and SETTING not in lines[0]    # one short line; the report carries the instructions
    assert report.summary() == "golden: 0/1 passed, 0 failed, 1 errored (G01); agent unavailable, 4 not asked"


def test_403_with_questions_in_flight_asks_at_most_one_per_worker():
    ids = ["G01", "G04", "G06", "G25", "G27"]
    report, _, _, fake = run({Q[i]: [Resp(403, raw="Forbidden")] for i in ids}, ids, parallel=2)
    assert 1 <= len(fake.of("tools/call")) <= 2 and report.unavailable
    assert report.asked + len(report.skipped) == 5 and sorted(report.errored + report.skipped) == sorted(ids)


def test_403_on_a_second_attempt_also_stops_the_run():
    script = {Q["G01"]: [requests.ConnectionError("Connection aborted."), Resp(403, raw="Forbidden")], Q["G04"]: [GOOD["G04"]]}
    report, lines, slept, fake = run(script, ["G01", "G04"], parallel=1)
    assert len(fake.of("tools/call")) == 2 and slept == [G.BACKOFF_S]
    assert report.unavailable and report.errored == ["G01"] and report.skipped == ["G04"]
    assert timeless(lines)[0].startswith("  G01: ERROR (Ns, 2 tries) the ontology agent is not available (HTTP 403")


def test_a_rejected_token_is_reported_as_unavailable_not_as_29_errors():
    ids = ["G01", "G04", "G06"]
    report, _, _, fake = run({Q[i]: [Resp(401, raw="")] for i in ids}, ids, parallel=1)
    assert len(fake.of("tools/call")) == 1 and "rejected the access token" in report.unavailable and report.skipped == ["G04", "G06"]


def test_a_handshake_that_cannot_be_made_is_retried_like_a_question():
    fake = FakeMcp({"initialize": [requests.ConnectionError("Connection aborted."), "go"], Q["G01"]: [GOOD["G01"]]})
    client, _, _ = make_client(fake)
    lines, slept = [], []
    report = G.run_golden(client, EXPECTED, ["G01"], say=lines.append, sleeper=slept.append)
    assert report.ok and report.passed == 1 and len(fake.of("initialize")) == 2 and slept == [G.BACKOFF_S]


def test_a_session_the_server_dropped_is_replaced_before_the_second_attempt():
    report, lines, slept, fake = run({Q["G01"]: [Resp(404, raw="session not found"), GOOD["G01"]]}, ["G01"])
    assert report.ok and report.passed == 1 and len(fake.of("initialize")) == 2 and slept == [G.BACKOFF_S]
    assert timeless(lines) == ["  G01: PASS (Ns, 2 tries)"]


def test_a_parallel_run_shares_one_session_and_collects_every_answer():
    ids = ["G01", "G04", "G06", "G25", "G27"]
    fake = FakeMcp({Q[i]: [GOOD[i]] for i in ids}, init_delay=0.03)
    client, _, _ = make_client(fake)
    report = G.run_golden(client, EXPECTED, ids, parallel=3, say=lambda line: None, sleeper=lambda s: None)
    assert report.ok and report.passed == 5 and sorted(report.answers) == sorted(ids)
    assert len(fake.of("initialize")) == 1 and {c.headers["Mcp-Session-Id"] for c in fake.of("tools/call")} == {"sess-1"}


def test_parallel_questions_really_run_together_and_none_is_lost_or_mixed_up():
    rows = numbered(12)
    seen, lock, barrier = [0], threading.Lock(), threading.Barrier(4, timeout=10)

    def answer(question):
        with lock:
            seen[0] += 1
            first = seen[0] <= 4
        if first:
            barrier.wait()                                                                          # four asks must be in flight at the same moment
        return answer_by_number(question)

    client, lines = FakeClient(answer), []
    report = G.run_golden(client, rows, [r["id"] for r in rows], parallel=4, say=lines.append, sleeper=lambda s: None)
    assert report.ok and report.asked == report.answered == report.passed == 12 and len(lines) == 12
    assert sorted(client.asked) == sorted(r["question"] for r in rows) and report.errored == [] and report.failed == []
    assert list(report.answers) == [r["id"] for r in rows]                                          # in the order of the ids, whatever finished first
    assert all(report.answers[r["id"]] == answer_by_number(r["question"]) for r in rows)
    assert report.summary() == "golden: 12/12 passed, 0 failed, 0 errored"


def test_failed_ids_keep_the_order_of_the_ids_when_answers_finish_out_of_order():
    rows, lines, second_reported = numbered(3), [], threading.Event()

    def answer(question):
        if question == "question 1":
            second_reported.wait(10)                                                                # question 1 finishes after question 2 was reported
        return "INR 1"

    def say(line):
        lines.append(line)
        if "Q02" in line:
            second_reported.set()

    report = G.run_golden(FakeClient(answer), rows, ["Q01", "Q02", "Q03"], parallel=3, say=say, sleeper=lambda s: None)
    assert [gid for gid, _ in report.failed] == ["Q01", "Q02", "Q03"]
    assert [ln.split(":")[0].strip() for ln in lines].index("Q02") < [ln.split(":")[0].strip() for ln in lines].index("Q01")


def test_unknown_ids_and_rows_without_an_expected_value_are_errors_and_are_not_asked():
    rows = [ROW["G01"], {"id": "G30", "question": "q30", "error": "KeyError('x')"}]
    client, lines = FakeClient(lambda q: GOOD["G01"]), []
    report = G.run_golden(client, rows, ["G01", "G30", "G99", "G01"], say=lines.append, sleeper=lambda s: None)
    assert client.asked == [Q["G01"]]                                                               # duplicate ids are asked once
    assert report.passed == 1 and report.errored == ["G30", "G99"] and report.asked == 3 and not report.ok
    text = "\n".join(timeless(lines))
    assert "G30: ERROR (Ns) no expected answer: KeyError('x')" in text and "G99: ERROR (Ns) no expected answer: the id is not in the expected answers" in text


def test_an_unexpected_exception_in_one_question_does_not_end_the_run():
    def answer(question):
        if question == "question 2":
            return None                                                                             # not a str: scoring blows up
        return answer_by_number(question)

    rows = numbered(3)
    report = G.run_golden(FakeClient(answer), rows, ["Q01", "Q02", "Q03"], parallel=1, say=lambda line: None, sleeper=lambda s: None)
    assert report.passed == 2 and report.errored == ["Q02"] and report.asked == 3


def test_nothing_to_ask_is_an_ok_empty_report():
    client, lines = FakeClient(lambda q: "x"), []
    report = G.run_golden(client, EXPECTED, [], say=lines.append)
    assert report.ok and report.asked == 0 and lines == [] and client.asked == []
    assert report.summary() == "golden: 0/0 passed, 0 failed, 0 errored"
    assert G.run_golden(client, EXPECTED, ["G01"], parallel=0, say=lines.append).passed == 0 and client.asked == [Q["G01"]]   # parallel below 1 means 1


def test_an_interrupt_cancels_the_questions_still_queued():
    rows, release, asked = numbered(6), threading.Event(), []

    def answer(question):
        asked.append(question)
        if question == "question 2":
            release.wait(10)
        return answer_by_number(question)

    def say(line):
        threading.Timer(0.2, release.set).start()                                                   # the question in flight finishes while run_golden is shutting down
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        G.run_golden(FakeClient(answer), rows, [r["id"] for r in rows], parallel=1, say=say, sleeper=lambda s: None)
    assert asked[0] == "question 1" and set(asked) <= {"question 1", "question 2"}


# ---------------------------------------------------------------------------------------------- the report
def test_report_summary_ok_and_unavailable_text():
    clean = G.GoldenReport(asked=5, answered=5, passed=5)
    assert clean.ok and clean.summary() == "golden: 5/5 passed, 0 failed, 0 errored"
    one_wrong = G.GoldenReport(asked=5, answered=5, passed=4, failed=[("G03", "missing=[1]")])
    assert not one_wrong.ok and one_wrong.summary() == "golden: 4/5 passed, 1 failed (G03), 0 errored"
    mixed = G.GoldenReport(asked=6, answered=4, passed=3, failed=[("G03", "x")], errored=["G07", "G09"])
    assert not mixed.ok and mixed.summary() == "golden: 3/6 passed, 1 failed (G03), 2 errored (G07, G09)"
    down = G.GoldenReport(asked=1, errored=["G01"], skipped=["G02", "G03"], unavailable="the ontology agent is not available")
    assert not down.ok and down.summary() == "golden: 0/1 passed, 0 failed, 1 errored (G01); agent unavailable, 2 not asked"
    assert not G.GoldenReport(unavailable="x").ok and G.GoldenReport().ok
    assert G.GoldenReport(asked=1, answered=1, failed=["G03"]).summary() == "golden: 0/1 passed, 1 failed (G03), 0 errored"       # ids alone work too


def test_build_client_points_at_the_ontology_endpoint_and_defaults_to_the_az_tokens(monkeypatch):
    mine = lambda scope: "tok"  # noqa: E731
    client = G.build_client("ws-1", "ont-1", mine)
    assert client.url == agents.FABRIC_MCP.format(ws="ws-1", ont="ont-1") == "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/ws-1/items/ont-1/ontologyEndpoint"
    assert client.token_fn is mine
    stub = lambda scope: "stub"  # noqa: E731
    monkeypatch.setattr(oac, "default_token_fn", lambda: stub)
    assert G.build_client("ws-1", "ont-1").token_fn is stub
