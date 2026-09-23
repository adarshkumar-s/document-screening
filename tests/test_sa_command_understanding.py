"""Command understanding and self-healing behaviour for the SA assistant.

SA is the conversational interface in the administrator portal. These tests
pin down two things that used to fail silently:

1. Command understanding: what counts as a record/property identifier, which
   phrasings map to a governed operation, and which requests belong to Arena
   (the implementation agent) rather than to SA.
2. Self-healing: a stale model name, a fenced JSON plan, an empty model
   answer or a locked SQLite file must degrade to the next-best behaviour
   instead of failing the administrator's turn.
"""
import asyncio

import pytest

import admin_assistant
import sa_agent


# --------------------------------------------------------------------------
# Command understanding: identifier parsing
# --------------------------------------------------------------------------

ID_CASES = [
    # (natural language, expected identifier or None)
    ("Show document #DOC-2026-ABC", "DOC-2026-ABC"),
    ("Propose reprocessing record 123", "123"),
    ("Escalate record #123", "123"),
    ("Assign record #1042 to a free Verification Officer", "1042"),
    ("record id 1042", "1042"),
    ("document number 987", "987"),
    ("Propose reprocessing for record adb30ee0c232", "adb30ee0c232"),
    ("Inspect record: adb30ee0c232", "adb30ee0c232"),
    ("Where is property DEMO-PROP-103-A?", "DEMO-PROP-103-A"),
    ("Show property history for parcel 103-A-2", "103-A-2"),
    # Ordinary English must never be read as an identifier.
    ("what operations can be done on this record", None),
    ("find which operation can be done on record", None),
    ("this record, find which operation can be done on it", None),
    ("show me pending records", None),
    ("show document", None),
    ("record id unknown", None),
    ("", None),
]


@pytest.mark.parametrize("prompt,expected", ID_CASES)
def test_record_id_parsing(prompt, expected):
    assert admin_assistant._record_id_from_prompt(prompt) == expected


@pytest.mark.parametrize("prompt,expected", ID_CASES)
def test_sa_uses_the_same_identifier_parser(prompt, expected):
    """SA and the Admin Assistant must not drift apart on identifiers."""
    assert sa_agent._extract_id(prompt) == expected


def test_parser_never_returns_a_stopword():
    for prompt in (
        "record find", "document show", "record the", "property which",
        "record list", "document open", "record operation",
    ):
        assert admin_assistant._record_id_from_prompt(prompt) is None


# --------------------------------------------------------------------------
# Command understanding: which requests belong to Arena
# --------------------------------------------------------------------------

ARENA_TASKS = [
    "implement a new endpoint for audit export",
    "add a column to the documents table",
    "the map page is broken",
    "refactor the OCR pipeline into a separate module",
    "deploy the latest changes to render",
    "write tests for the mutation workflow",
    "open a pull request for the litigation fix",
    "modify the code so the dashboard loads faster",
]

SA_TASKS = [
    "show me pending records",
    "list the uploaded files",
    "is the production API healthy?",
    "check the audit log",
    "assign record #1042 to a free officer",
    "create a verification task for record 123",
    "which operations can be done on record 123",
    "how many documents are waiting for verification?",
    "summarise the officers' workload",
    "show property history for DEMO-PROP-103-A",
]


@pytest.mark.parametrize("task", ARENA_TASKS)
def test_implementation_requests_are_handed_to_arena(task):
    assert sa_agent._is_arena_task(task) is True


@pytest.mark.parametrize("task", SA_TASKS)
def test_operational_questions_stay_with_sa(task):
    """Bare code nouns must not steal operational questions from SA."""
    assert sa_agent._is_arena_task(task) is False


def test_very_long_request_is_handed_to_arena():
    assert sa_agent._is_arena_task("explain " * 120) is True


def test_empty_request_is_not_handed_to_arena():
    assert sa_agent._is_arena_task("") is False
    assert sa_agent._is_arena_task(None) is False


# --------------------------------------------------------------------------
# Command understanding: governed operation recognition
# --------------------------------------------------------------------------

WRITE_CASES = [
    ("Propose reprocessing record 123", "reprocess"),
    ("re-run OCR on document 123", "reprocess"),
    ("escalate record #123", "escalate"),
    ("flag record 123 for review", "escalate"),
    ("assign record 123 to a free officer", "assign_task"),
    ("hand over record 123 to an officer", "assign_task"),
    ("open a verification case for record 123", "verification_case"),
    ("remove exact pin for property DEMO-PROP-103-A", "clear_property_location"),
    ("set exact pin for property DEMO-PROP-103-A at 28.6, 77.1", "set_property_location"),
    ("create a mutation application for property DEMO-PROP-103-A", "mutation_application"),
    ("create a verification task for record 123", "create_task"),
    ("which operations can be done on record 123", None),
    ("show me pending records", None),
]


@pytest.mark.parametrize("task,expected", WRITE_CASES)
def test_write_action_recognition(task, expected):
    assert sa_agent._write_action(task) == expected


def test_operation_discovery_question_plans_both_reads():
    plan = sa_agent._fallback_plan("which operations can be done on record 123")
    tools = [step["tool"] for step in plan["steps"]]
    assert "document" in tools
    assert "record_operations" in tools


def test_plain_question_falls_back_to_operational_context():
    plan = sa_agent._fallback_plan("hello, anything urgent today?")
    assert plan["steps"], "an unrecognised request still gathers site context"
    assert plan["steps"][0]["tool"] == "operational_intelligence"


# --------------------------------------------------------------------------
# Self-healing: model selection
# --------------------------------------------------------------------------

class _FakeModels:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def generate_content(self, model=None, contents=None, config=None):
        self.calls.append(model)
        outcome = self.behaviour(model, len(self.calls))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, behaviour):
        self.models = _FakeModels(behaviour)


class _FakeResponse:
    def __init__(self, text=""):
        self.text = text


@pytest.fixture
def model_candidates(monkeypatch):
    """Pin the candidate list and clear the last-known-good model."""
    monkeypatch.setattr(sa_agent, "_model_candidates", lambda: ["m-one", "m-two", "m-three"])
    monkeypatch.setattr(sa_agent, "_last_working_model", None)


def test_retired_model_is_skipped_and_the_working_one_is_remembered(model_candidates):
    def behaviour(model, call):
        if model == "m-one":
            return RuntimeError("404 NOT_FOUND: models/m-one is not found for API version v1beta")
        return _FakeResponse("ok")

    client = _FakeClient(behaviour)
    response = sa_agent._generate_content(client, "prompt", None)

    assert response.text == "ok"
    assert client.models.calls == ["m-one", "m-two"]
    assert sa_agent.last_working_model() == "m-two"
    assert sa_agent._ordered_models()[0] == "m-two", "the healthy model is tried first next time"


def test_transient_failure_moves_to_the_next_model(model_candidates):
    def behaviour(model, call):
        if model == "m-one":
            return RuntimeError("503 UNAVAILABLE: the service is temporarily unavailable")
        return _FakeResponse("ok")

    client = _FakeClient(behaviour)
    assert sa_agent._generate_content(client, "prompt", None).text == "ok"
    assert client.models.calls == ["m-one", "m-two"]


def test_unknown_error_fails_fast_instead_of_burning_every_candidate(model_candidates):
    def behaviour(model, call):
        return TypeError("unexpected programming error")

    client = _FakeClient(behaviour)
    with pytest.raises(TypeError):
        sa_agent._generate_content(client, "prompt", None)
    assert client.models.calls == ["m-one"]


def test_all_models_unusable_raises_after_trying_each_candidate(model_candidates):
    def behaviour(model, call):
        return RuntimeError("404 NOT_FOUND: models/%s is not found" % model)

    client = _FakeClient(behaviour)
    with pytest.raises(RuntimeError):
        sa_agent._generate_content(client, "prompt", None)
    assert client.models.calls == ["m-one", "m-two", "m-three"]


@pytest.mark.parametrize(
    "message,expected",
    [
        ("404 NOT_FOUND: model retired", "MODEL"),
        ("403 PERMISSION_DENIED", "MODEL"),
        ("models/gemini-x is not supported for generateContent", "MODEL"),
        ("503 UNAVAILABLE", "TRANSIENT"),
        ("429 RESOURCE_EXHAUSTED quota exceeded", "TRANSIENT"),
        ("deadline exceeded while waiting for the response", "TRANSIENT"),
        ("something completely unexpected", "UNKNOWN"),
    ],
)
def test_error_classification(message, expected):
    assert sa_agent.classify_error(RuntimeError(message)) == expected


# --------------------------------------------------------------------------
# Self-healing: plan parsing
# --------------------------------------------------------------------------

def test_coerce_json_handles_fenced_and_prose_wrapped_output():
    expected = {"goal": "g", "steps": []}
    assert sa_agent._coerce_json('```json\n{"goal": "g", "steps": []}\n```') == expected
    assert sa_agent._coerce_json('Here you go:\n{"goal": "g", "steps": []}\nHope that helps') == expected
    assert sa_agent._coerce_json('{"goal": "g", "steps": []}') == expected


def test_coerce_json_rejects_non_objects():
    assert sa_agent._coerce_json("not json at all") is None
    assert sa_agent._coerce_json("") is None
    assert sa_agent._coerce_json(None) is None


def test_sanitize_plan_drops_unknown_tools_and_malformed_steps():
    plan = {
        "goal": "inspect",
        "steps": [
            {"id": "a", "tool": "document", "args": {"record_id": "123"}, "purpose": "read"},
            {"id": "b", "tool": "delete_everything", "args": {}, "purpose": "hallucinated"},
            {"id": "c", "tool": "officers"},
            "not-a-dict",
        ],
    }
    cleaned = sa_agent._sanitize_plan(plan)
    assert [step["tool"] for step in cleaned["steps"]] == ["document", "officers"]
    assert cleaned["steps"][1]["args"] == {}
    assert cleaned["steps"][1]["depends_on"] == []


def test_sanitize_plan_returns_none_when_nothing_is_usable():
    assert sa_agent._sanitize_plan({"steps": [{"tool": "nope"}]}) is None
    assert sa_agent._sanitize_plan({"steps": "not a list"}) is None
    assert sa_agent._sanitize_plan(None) is None


# --------------------------------------------------------------------------
# Self-healing: empty or failing model answers
# --------------------------------------------------------------------------

def test_empty_model_answer_is_retried_then_summarised(monkeypatch):
    calls = []

    def behaviour(model, call):
        calls.append(call)
        return _FakeResponse("" if len(calls) == 1 else "Recovered summary.")

    monkeypatch.setattr(sa_agent, "_ai", lambda: _FakeClient(behaviour))
    monkeypatch.setattr(sa_agent, "_model_candidates", lambda: ["m-one"])
    monkeypatch.setattr(sa_agent, "_last_working_model", None)

    answer = sa_agent._synthesize("what is pending?", [{"tool": "pending_records", "ok": True, "result": {"count": 3}}])
    assert answer == "Recovered summary."


def test_deterministic_answer_used_when_no_model_can_answer(monkeypatch):
    monkeypatch.setattr(sa_agent, "_ai", lambda: _FakeClient(lambda model, call: RuntimeError("404 NOT_FOUND")))
    monkeypatch.setattr(sa_agent, "_model_candidates", lambda: ["m-one"])
    monkeypatch.setattr(sa_agent, "_last_working_model", None)

    answer = sa_agent._synthesize(
        "what is pending?",
        [
            {"tool": "pending_records", "ok": True, "result": {"count": 3}},
            {"tool": "officers", "ok": False, "error": "database is locked"},
        ],
    )

    assert "pending_records" in answer
    assert "database is locked" in answer, "a failed read is reported, not hidden"
    assert "Administrator Approval" in answer


def test_deterministic_answer_reports_evidence_only():
    answer = sa_agent._deterministic_answer("inspect", [{"tool": "document", "ok": True, "result": {"id": "42"}}])
    assert "42" in answer
    assert "could not reach the language model" in answer


# --------------------------------------------------------------------------
# Self-healing: transient SQLite contention
# --------------------------------------------------------------------------

def test_locked_database_read_is_retried(monkeypatch):
    attempts = {"count": 0}

    def flaky(args):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("database is locked")
        return {"id": "123"}

    monkeypatch.setattr(sa_agent, "_read_tools", lambda: {"document": flaky})
    plan = {"steps": [{"id": "d", "tool": "document", "args": {"record_id": "123"}, "purpose": "read"}]}

    results = asyncio.run(sa_agent._execute_reads(plan))

    assert results[0]["ok"] is True
    assert results[0]["result"] == {"id": "123"}
    assert attempts["count"] == 3


def test_permanent_read_error_is_not_retried_forever(monkeypatch):
    attempts = {"count": 0}

    def broken(args):
        attempts["count"] += 1
        raise RuntimeError("no such table: documents")

    monkeypatch.setattr(sa_agent, "_read_tools", lambda: {"document": broken})
    plan = {"steps": [{"id": "d", "tool": "document", "args": {}, "purpose": "read"}]}

    results = asyncio.run(sa_agent._execute_reads(plan))

    assert results[0]["ok"] is False
    assert attempts["count"] == 1, "a non-transient error fails immediately"


def test_transient_db_error_detection():
    assert sa_agent.is_transient_db_error("database is locked") is True
    assert sa_agent.is_transient_db_error("database table is locked") is True
    assert sa_agent.is_transient_db_error("no such table: documents") is False
