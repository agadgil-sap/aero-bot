"""Pin the shadow advisor's fail-closed client and monitor contract."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import BaseModel

from aero_bot.advisor import (
    ADVISOR_DISABLE_THINKING_ENV,
    ADVISOR_FALLBACK_URL_ENV,
    ADVISOR_JSON_MODE_ENV,
    ADVISOR_MAX_TOKENS_ENV,
    ADVISOR_MODEL_ENV,
    ADVISOR_REPORT_PATH_ENV,
    ADVISOR_SYSTEM_PROMPT,
    ADVISOR_TEACHING_FILE_ENV,
    ADVISOR_TEACHING_MAX_CHARS,
    ADVISOR_TEACHING_SEPARATOR,
    ADVISOR_TIMEOUT_ENV,
    ADVISOR_URL_ENV,
    AdvisorAbsentReason,
    AdvisorAnomaly,
    AdvisorBrief,
    AdvisorConfig,
    AdvisorHttpResponse,
    AdvisorMonitor,
    AdvisorMonitorReport,
    AdvisorOutcome,
    AdvisorTimeoutError,
    AdvisorUnreachableError,
    HttpxAdvisorTransport,
    build_user_prompt,
    compose_system_prompt,
    compose_window_facts,
    main,
    parse_advisor_config,
    request_brief,
    resolve_report_path,
)
from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.config import Settings
from aero_bot.cycle import (
    CYCLE_STATE_PATH_ENV,
    CycleReportPayload,
    CycleStateBook,
    CycleStateStore,
    ReentryCooldown,
    TrackedPosition,
)

# Fixed aware time keeps composed-window arithmetic deterministic.
CREATED_AT = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
# A second fixture instant proves most-recent-first reason ordering.
LATER_AT = datetime(2026, 9, 24, 13, 0, tzinfo=UTC)


class ScriptedTransport:
    """Serve one scripted HTTP answer or raise one scripted failure."""

    def __init__(
        self,
        response: AdvisorHttpResponse | None = None,
        failure: Exception | None = None,
    ) -> None:
        """Hold the one answer every advisory request receives.

        Args:
            response: The scripted successful HTTP answer.
            failure: The scripted transport failure to raise instead.
        """
        self.response = response
        self.failure = failure
        self.requests: list[tuple[str, Mapping[str, object], float]] = []

    def complete(
        self,
        url: str,
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> AdvisorHttpResponse:
        """Record one request and serve the scripted answer or failure."""
        self.requests.append((url, payload, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        assert self.response is not None
        return self.response


class SequencedTransport:
    """Serve scripted per-call answers or failures, in call order."""

    def __init__(
        self,
        script: list[tuple[AdvisorHttpResponse | None, Exception | None]],
    ) -> None:
        """Hold one (response, failure) pair per expected call.

        Args:
            script: One entry per call; a None pair answers a bare 200.
        """
        self.script = script
        self.requests: list[tuple[str, Mapping[str, object], float]] = []

    def complete(
        self,
        url: str,
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> AdvisorHttpResponse:
        """Record one request and serve the next scripted entry."""
        self.requests.append((url, payload, timeout_seconds))
        response, failure = self.script[len(self.requests) - 1]
        if failure is not None:
            raise failure
        assert response is not None
        return response


class ForeignPayload(BaseModel):
    """Provide one non-cycle payload proving window filtering."""

    detail: str


def chat_body(content: str, *, reasoning: str = "") -> str:
    """Wrap model content in one Ollama-native chat answer body."""
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    return json.dumps({"model": "m", "message": message, "done_reason": "stop"})


def accepted_answer() -> dict[str, object]:
    """Build one schema-valid advisory answer with one anomaly flag."""
    return {
        "brief": "The bot holds SNDKc out of range above; day P&L is calm.",
        "anomalies": [
            {
                "label": "long_range_wait",
                "confidence": 0.42,
                "rationale": "The wait anchor has persisted for hours without a recenter.",
            }
        ],
    }


def seeded_store(tmp_path: Path) -> AuditStore:
    """Build one audit chain carrying two cycle summaries and one foreign record."""
    store = AuditStore(tmp_path / "audit" / "audit.sqlite3")
    store.append(
        AuditEventType.CYCLE_REPORTED,
        CycleReportPayload(
            mode="live",
            symbol="SNDKc",
            action="hold",
            reason="open_above_range_waiting",
            tracked_token_id=123,
            equity_usdc="99.12",
            day_start_equity_usdc="98.00",
            day_pnl_usdc="1.12",
            claimable_pool_fees_usdc="0.31",
            measured_fee_apr="0.018",
            action_count=1,
        ),
        CREATED_AT,
    )
    store.append(
        AuditEventType.SYSTEM_STATE,
        ForeignPayload(detail="startup"),
        CREATED_AT + timedelta(minutes=1),
    )
    store.append(
        AuditEventType.CYCLE_REPORTED,
        CycleReportPayload(
            mode="live",
            symbol="SNDKc",
            action="hold",
            reason="open_above_range_waiting",
            equity_usdc="99.50",
            day_start_equity_usdc="98.00",
            day_pnl_usdc="1.50",
            claimable_pool_fees_usdc="0.45",
            measured_fee_apr="0.021",
            halted_reason="daily_loss_halt",
        ),
        LATER_AT,
    )
    return store


def tracked_book() -> CycleStateBook:
    """Build one book tracking an out-of-range position with one cooldown."""
    return CycleStateBook(
        position=TrackedPosition(
            symbol="SNDKc",
            token_id=123,
            pool_address="0x" + "ab" * 20,
            committed_usd=Decimal("80"),
            entered_at=CREATED_AT - timedelta(days=2),
            out_of_range_since=CREATED_AT - timedelta(minutes=300),
            out_of_range_side="above",
        ),
        reentry_cooldowns=(
            ReentryCooldown(symbol="TSLAc", blocked_until=CREATED_AT + timedelta(hours=4)),
        ),
        updated_at=LATER_AT,
    )


def test_config_defaults_dark_and_enables_only_with_both_values() -> None:
    """An empty environment stays dark; both sealed values enable the surface."""
    assert parse_advisor_config({}) == AdvisorConfig(url="", model="")
    assert not parse_advisor_config({}).enabled
    sealed = parse_advisor_config(
        {ADVISOR_URL_ENV: "http://100.106.111.37:11434", ADVISOR_MODEL_ENV: "qwen3.6:35b-a3b"}
    )
    assert sealed.enabled
    assert sealed.timeout_seconds == 15.0
    url_only = parse_advisor_config({ADVISOR_URL_ENV: "http://plane"})
    assert not url_only.enabled


def test_config_rejects_bad_url_and_timeout_naming_the_variable() -> None:
    """Malformed values raise naming the variable, never any value."""
    with pytest.raises(ValueError, match=ADVISOR_URL_ENV):
        parse_advisor_config({ADVISOR_URL_ENV: "ftp://plane"})
    with pytest.raises(ValueError, match=ADVISOR_TIMEOUT_ENV):
        parse_advisor_config({ADVISOR_TIMEOUT_ENV: "0"})
    with pytest.raises(ValueError, match=ADVISOR_TIMEOUT_ENV):
        parse_advisor_config({ADVISOR_TIMEOUT_ENV: "not-a-number"})
    bounded = parse_advisor_config(
        {
            ADVISOR_URL_ENV: "http://plane",
            ADVISOR_MODEL_ENV: "m",
            ADVISOR_TIMEOUT_ENV: "30",
        }
    )
    assert bounded.timeout_seconds == 30.0


def test_request_brief_dark_config_never_touches_the_transport() -> None:
    """A dark configuration answers dark without delivering anything."""
    outcome = request_brief(
        AdvisorConfig(url="", model="m"),
        "system",
        "user",
        cast("ScriptedTransport", None),
    )
    assert outcome.reason is AdvisorAbsentReason.DARK
    assert outcome.result is None


def test_request_brief_ignores_reasoning_models_separate_thinking_field() -> None:
    """A reasoning model's sibling reasoning field never reaches the schema."""
    reasoning_body = chat_body(
        json.dumps(accepted_answer()),
        reasoning="I should think about <think> braces }</think> noise.",
    )
    outcome = request_brief(
        AdvisorConfig(url="http://plane", model="qwen3.6:35b-a3b"),
        "s",
        "u",
        ScriptedTransport(response=AdvisorHttpResponse(status_code=200, body=reasoning_body)),
    )
    assert outcome.result is not None
    assert outcome.result.brief.anomalies[0].label == "long_range_wait"


def test_request_brief_threads_the_native_payload_contract() -> None:
    """The sealed knobs ride the Ollama-native payload exactly once each."""
    transport = ScriptedTransport(
        response=AdvisorHttpResponse(status_code=200, body=chat_body(json.dumps(accepted_answer())))
    )
    request_brief(
        AdvisorConfig(url="http://plane", model="m", max_tokens=2048), "s", "u", transport
    )
    url, payload, timeout = transport.requests[0]
    assert url == "http://plane/api/chat"
    assert payload["model"] == "m"
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0, "num_predict": 2048}
    # The hardened defaults ride every request: JSON-constrained generation,
    # thinking off, and the residency pin on the sealed primary plane.
    assert payload["format"] == "json"
    assert payload["think"] is False
    assert payload["keep_alive"] == -1
    assert timeout == 15.0

    thinking_on = ScriptedTransport(
        response=AdvisorHttpResponse(status_code=200, body=chat_body(json.dumps(accepted_answer())))
    )
    request_brief(
        AdvisorConfig(url="http://plane", model="m", disable_thinking=False, json_mode=False),
        "s",
        "u",
        thinking_on,
    )
    _, unsealed_payload, _ = thinking_on.requests[0]
    assert "think" not in unsealed_payload
    assert "format" not in unsealed_payload
    # The residency pin survives the knobs: it is the seat's contract, not
    # an operator toggle.
    assert unsealed_payload["keep_alive"] == -1


def test_config_parses_the_hardening_knobs_and_names_bad_variables() -> None:
    """The budget, thinking, JSON-mode, and fallback switches parse."""
    sealed = parse_advisor_config(
        {
            ADVISOR_URL_ENV: "http://plane",
            ADVISOR_MODEL_ENV: "m",
            ADVISOR_MAX_TOKENS_ENV: "8192",
            ADVISOR_DISABLE_THINKING_ENV: "false",
            ADVISOR_JSON_MODE_ENV: "0",
            ADVISOR_FALLBACK_URL_ENV: "http://shared",
        }
    )
    assert sealed.max_tokens == 8192
    assert sealed.disable_thinking is False
    assert sealed.json_mode is False
    assert sealed.fallback_url == "http://shared"
    # The hardened defaults: thinking off, JSON mode on, no fallback.
    defaults = parse_advisor_config({ADVISOR_URL_ENV: "http://p", ADVISOR_MODEL_ENV: "m"})
    assert defaults.max_tokens == 4096
    assert defaults.disable_thinking is True
    assert defaults.json_mode is True
    assert defaults.fallback_url == ""
    with pytest.raises(ValueError, match=ADVISOR_MAX_TOKENS_ENV):
        parse_advisor_config({ADVISOR_MAX_TOKENS_ENV: "not-tokens"})
    with pytest.raises(ValueError, match=ADVISOR_MAX_TOKENS_ENV):
        parse_advisor_config({ADVISOR_MAX_TOKENS_ENV: "10"})
    with pytest.raises(ValueError, match=ADVISOR_DISABLE_THINKING_ENV):
        parse_advisor_config({ADVISOR_DISABLE_THINKING_ENV: "maybe"})
    with pytest.raises(ValueError, match=ADVISOR_JSON_MODE_ENV):
        parse_advisor_config({ADVISOR_JSON_MODE_ENV: "maybe"})
    with pytest.raises(ValueError, match=ADVISOR_FALLBACK_URL_ENV):
        parse_advisor_config({ADVISOR_FALLBACK_URL_ENV: "ftp://shared"})


def test_request_brief_accepts_schema_valid_answer_and_threads_metadata() -> None:
    """One valid JSON answer validates into a brief with serving metadata."""
    transport = ScriptedTransport(
        response=AdvisorHttpResponse(status_code=200, body=chat_body(json.dumps(accepted_answer())))
    )
    outcome = request_brief(
        AdvisorConfig(url="http://plane/", model="qwen3.6:35b-a3b"), "s", "u", transport
    )
    assert outcome.reason is None
    assert outcome.result is not None
    assert outcome.result.model == "qwen3.6:35b-a3b"
    assert outcome.result.brief.anomalies[0].label == "long_range_wait"
    assert outcome.status == "brief"
    url, payload, timeout = transport.requests[0]
    assert url == "http://plane/api/chat"
    assert payload["model"] == "qwen3.6:35b-a3b"
    options = cast("Mapping[str, object]", payload["options"])
    assert options["temperature"] == 0
    assert timeout == 15.0


def test_request_brief_tolerates_fenced_json_answers() -> None:
    """A fence-wrapped JSON body with trailing prose still validates."""
    fenced = "```json\n" + json.dumps(accepted_answer()) + "\n```\nDone."
    outcome = request_brief(
        AdvisorConfig(url="http://plane", model="m"),
        "s",
        "u",
        ScriptedTransport(response=AdvisorHttpResponse(status_code=200, body=chat_body(fenced))),
    )
    assert outcome.result is not None
    assert outcome.result.brief.anomalies[0].confidence == 0.42


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (500, chat_body(json.dumps(accepted_answer())), AdvisorAbsentReason.HTTP_STATUS),
        # The native plane answers some internal failures (a Metal compute
        # error under memory pressure, observed live) as a 200 carrying an
        # error envelope; that is a plane failure, not a parse failure.
        (200, json.dumps({"error": "Compute error."}), AdvisorAbsentReason.HTTP_STATUS),
        (200, "not-json", AdvisorAbsentReason.MALFORMED_JSON),
        (200, json.dumps({"message": {}}), AdvisorAbsentReason.MALFORMED_JSON),
        (
            200,
            json.dumps({"message": {"role": "assistant", "content": ""}}),
            AdvisorAbsentReason.EMPTY_CONTENT,
        ),
        (200, chat_body("prose without any object"), AdvisorAbsentReason.MALFORMED_JSON),
        (
            200,
            chat_body(json.dumps({"brief": "missing anomalies"})),
            AdvisorAbsentReason.SCHEMA_INVALID,
        ),
        (
            200,
            chat_body(json.dumps({"brief": "x" * 2001, "anomalies": []})),
            AdvisorAbsentReason.SCHEMA_INVALID,
        ),
        (
            200,
            chat_body(
                json.dumps(
                    {
                        "brief": "ok",
                        "anomalies": [{"label": "a", "confidence": 1.5, "rationale": "over"}],
                    }
                )
            ),
            AdvisorAbsentReason.SCHEMA_INVALID,
        ),
    ],
)
def test_request_brief_failures_become_typed_absences(
    status: int, body: str, expected: AdvisorAbsentReason
) -> None:
    """Every failure shape answers one stable absence reason, never an exception."""
    outcome = request_brief(
        AdvisorConfig(url="http://plane", model="m"),
        "s",
        "u",
        ScriptedTransport(response=AdvisorHttpResponse(status_code=status, body=body)),
    )
    assert outcome.result is None
    assert outcome.reason is expected
    assert outcome.status == expected.value


def test_request_brief_maps_transport_failures_to_timeout_and_unreachable() -> None:
    """Timeout and unreachable transports answer their own absence reasons."""
    timeout = request_brief(
        AdvisorConfig(url="http://plane", model="m"),
        "s",
        "u",
        ScriptedTransport(failure=AdvisorTimeoutError("slow")),
    )
    assert timeout.reason is AdvisorAbsentReason.TIMEOUT
    unreachable = request_brief(
        AdvisorConfig(url="http://plane", model="m"),
        "s",
        "u",
        ScriptedTransport(failure=AdvisorUnreachableError("down")),
    )
    assert unreachable.reason is AdvisorAbsentReason.UNREACHABLE


def test_unreachable_primary_retries_once_against_the_fallback() -> None:
    """A downed dedicated plane answers through the shared fallback."""
    transport = SequencedTransport(
        [
            (None, AdvisorUnreachableError("dedicated down")),
            (
                AdvisorHttpResponse(status_code=200, body=chat_body(json.dumps(accepted_answer()))),
                None,
            ),
        ]
    )
    outcome = request_brief(
        AdvisorConfig(url="http://dedicated", model="m", fallback_url="http://shared/"),
        "s",
        "u",
        transport,
    )
    assert outcome.result is not None
    assert outcome.result.brief.anomalies[0].label == "long_range_wait"
    assert len(transport.requests) == 2
    first_url, first_payload, _ = transport.requests[0]
    second_url, second_payload, _ = transport.requests[1]
    assert first_url == "http://dedicated/api/chat"
    assert second_url == "http://shared/api/chat"
    # The model and prompt never change between attempts: the fallback can
    # never serve a silent wrong-model call.
    assert first_payload["model"] == second_payload["model"]
    assert first_payload["messages"] == second_payload["messages"]
    # Only the residency pin differs: the fallback leaves the shared
    # instance's own eviction policy alone so one answered window never
    # permanently claims its VRAM.
    assert first_payload["keep_alive"] == -1
    assert "keep_alive" not in second_payload


def test_unreachable_primary_without_fallback_stays_one_attempt() -> None:
    """Without a sealed fallback the single attempt stands as the absence."""
    transport = ScriptedTransport(failure=AdvisorUnreachableError("down"))
    outcome = request_brief(AdvisorConfig(url="http://plane", model="m"), "s", "u", transport)
    assert outcome.reason is AdvisorAbsentReason.UNREACHABLE
    assert len(transport.requests) == 1


def test_both_planes_unreachable_answers_the_typed_absence() -> None:
    """A fallback that is also down costs exactly one retry, never a loop."""
    transport = SequencedTransport(
        [
            (None, AdvisorUnreachableError("dedicated down")),
            (None, AdvisorUnreachableError("shared down")),
        ]
    )
    outcome = request_brief(
        AdvisorConfig(url="http://dedicated", model="m", fallback_url="http://shared"),
        "s",
        "u",
        transport,
    )
    assert outcome.reason is AdvisorAbsentReason.UNREACHABLE
    assert len(transport.requests) == 2


def test_a_timeout_never_burns_a_second_attempt() -> None:
    """A timed-out primary answers timeout; the clock is already spent."""
    transport = SequencedTransport([(None, AdvisorTimeoutError("wedged"))])
    outcome = request_brief(
        AdvisorConfig(url="http://dedicated", model="m", fallback_url="http://shared"),
        "s",
        "u",
        transport,
    )
    assert outcome.reason is AdvisorAbsentReason.TIMEOUT
    assert len(transport.requests) == 1


def test_a_fallback_equal_to_the_primary_never_retries() -> None:
    """The same base URL sealed twice is one endpoint, not a retry loop."""
    transport = ScriptedTransport(failure=AdvisorUnreachableError("down"))
    outcome = request_brief(
        AdvisorConfig(url="http://plane/", model="m", fallback_url="http://plane"),
        "s",
        "u",
        transport,
    )
    assert outcome.reason is AdvisorAbsentReason.UNREACHABLE
    assert len(transport.requests) == 1


def test_a_fallback_http_failure_is_not_retried_further() -> None:
    """A reached fallback answering a failure stands as the typed absence."""
    transport = SequencedTransport(
        [
            (None, AdvisorUnreachableError("dedicated down")),
            (AdvisorHttpResponse(status_code=503, body="unavailable"), None),
        ]
    )
    outcome = request_brief(
        AdvisorConfig(url="http://dedicated", model="m", fallback_url="http://shared"),
        "s",
        "u",
        transport,
    )
    assert outcome.reason is AdvisorAbsentReason.HTTP_STATUS
    assert len(transport.requests) == 2


def test_outcome_rejects_both_branches_and_neither() -> None:
    """Exactly one of result or reason must be set on every outcome."""
    brief = AdvisorBrief(
        brief="ok",
        anomalies=(AdvisorAnomaly(label="l", confidence=0.1, rationale="r"),),
    )
    with pytest.raises(ValueError, match="exactly one"):
        AdvisorOutcome()
    from aero_bot.advisor import AdvisorResult

    result = AdvisorResult(model="m", brief=brief, elapsed_seconds=1.0)
    with pytest.raises(ValueError, match="exactly one"):
        AdvisorOutcome(result=result, reason=AdvisorAbsentReason.DARK)


def test_httpx_transport_serves_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The httpx transport maps transport errors and returns raw answers."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=chat_body(json.dumps(accepted_answer())))

    transport = HttpxAdvisorTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))
    answer = transport.complete("http://plane/api/chat", {"model": "m"}, 5.0)
    assert answer.status_code == 200
    assert "message" in answer.body
    assert seen[0].url == "http://plane/api/chat"

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    failing_transport = HttpxAdvisorTransport(
        client=httpx.Client(transport=httpx.MockTransport(failing))
    )
    with pytest.raises(AdvisorUnreachableError):
        failing_transport.complete("http://plane", {}, 5.0)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    slow_transport = HttpxAdvisorTransport(client=httpx.Client(transport=httpx.MockTransport(slow)))
    with pytest.raises(AdvisorTimeoutError):
        slow_transport.complete("http://plane", {}, 5.0)


def test_compose_window_facts_reads_only_cycle_summaries(tmp_path: Path) -> None:
    """Composition filters foreign records and reads the latest report's economics."""
    store = seeded_store(tmp_path)
    records = store.read_records(limit=10, offset=0)
    facts = compose_window_facts(
        records,
        tracked_symbol="SNDKc",
        committed_usdc="80",
        out_of_range_side="above",
        out_of_range_since=CREATED_AT - timedelta(minutes=300),
        cooldown_symbols=("TSLAc",),
        now=LATER_AT,
    )
    # The foreign system-state record is filtered out of the window.
    assert facts.record_count == 2
    # Latest-report economics win, not the first record's.
    assert facts.equity_usdc == "99.50"
    assert facts.day_pnl_usdc == "1.50"
    assert facts.claimable_pool_fees_usdc == "0.45"
    assert facts.measured_fee_apr == "0.021"
    assert facts.latest_action == "hold"
    assert facts.halted_count == 1
    assert facts.acting_count == 1
    assert facts.reasons == ("open_above_range_waiting",)
    assert facts.out_of_range_minutes == 360.0
    assert facts.cooldown_symbols == ("TSLAc",)


def test_compose_window_facts_over_empty_records_stays_all_absent() -> None:
    """An empty window composes zeros and absences rather than guessing."""
    facts = compose_window_facts([], None, None, None, None, (), LATER_AT)
    assert facts.record_count == 0
    assert facts.tracked_symbol is None
    assert facts.equity_usdc is None
    assert facts.halted_count == 0
    assert facts.reasons == ()


def test_monitor_persists_reports_and_audits_the_outcome(tmp_path: Path) -> None:
    """One monitor pass persists the report file and appends the audit record."""
    store = seeded_store(tmp_path)
    state = tmp_path / "cycle_state.json"
    CycleStateStore(state).save(tracked_book())
    transport = ScriptedTransport(
        response=AdvisorHttpResponse(status_code=200, body=chat_body(json.dumps(accepted_answer())))
    )
    report_path = tmp_path / "advisor_last_report.json"
    monitor = AdvisorMonitor(
        store,
        CycleStateStore(state),
        AdvisorConfig(url="http://plane", model="qwen3.6:35b-a3b"),
        transport,
        report_path,
    )
    report = monitor.run_once(now=LATER_AT)
    assert isinstance(report, AdvisorMonitorReport)
    assert report.facts.record_count == 2
    assert report.facts.tracked_symbol == "SNDKc"
    assert report.facts.committed_usdc == "80"
    assert report.facts.out_of_range_side == "above"
    assert report.facts.out_of_range_minutes == 360.0
    assert report.facts.equity_usdc == "99.50"
    assert report.facts.day_pnl_usdc == "1.50"
    assert report.facts.halted_count == 1
    assert report.facts.reasons == ("open_above_range_waiting",)
    assert report.facts.cooldown_symbols == ("TSLAc",)
    assert report.outcome.result is not None

    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["outcome"]["result"]["model"] == "qwen3.6:35b-a3b"
    assert persisted["facts"]["tracked_symbol"] == "SNDKc"

    records = store.read_records(limit=10, offset=0)
    advisor_records = [
        record for record in records if record.event_type is AuditEventType.ADVISOR_REPORTED
    ]
    assert len(advisor_records) == 1
    payload = json.loads(advisor_records[0].payload_json)
    assert payload["outcome"] == "brief"
    assert payload["model"] == "qwen3.6:35b-a3b"
    assert payload["anomalies"][0]["label"] == "long_range_wait"
    assert payload["window_records"] == 2
    assert store.verify_chain().status.value == "verified"


def test_monitor_audits_absence_with_its_stable_reason(tmp_path: Path) -> None:
    """A failing plane still persists and audits the typed absence."""
    store = seeded_store(tmp_path)
    state = tmp_path / "cycle_state.json"
    CycleStateStore(state).save(tracked_book())
    monitor = AdvisorMonitor(
        store,
        CycleStateStore(state),
        AdvisorConfig(url="http://plane", model="m"),
        ScriptedTransport(failure=AdvisorUnreachableError("down")),
        tmp_path / "advisor_last_report.json",
    )
    report = monitor.run_once(now=LATER_AT)
    assert report.outcome.reason is AdvisorAbsentReason.UNREACHABLE
    records = store.read_records(limit=10, offset=0)
    payload = json.loads(
        [record for record in records if record.event_type is AuditEventType.ADVISOR_REPORTED][
            0
        ].payload_json
    )
    assert payload["outcome"] == "unreachable"
    assert payload["brief"] == ""
    assert payload["latency_ms"] == 0


def test_monitor_over_an_empty_store_composes_an_empty_picture(tmp_path: Path) -> None:
    """A fresh store still runs a pass over zero cycle records."""
    store = AuditStore(tmp_path / "audit" / "audit.sqlite3")
    monitor = AdvisorMonitor(
        store,
        CycleStateStore(tmp_path / "cycle_state.json"),
        AdvisorConfig(url="http://plane", model="m"),
        ScriptedTransport(
            response=AdvisorHttpResponse(
                status_code=200, body=chat_body('{"brief": "Nothing yet.", "anomalies": []}')
            )
        ),
        tmp_path / "advisor_last_report.json",
    )
    report = monitor.run_once(now=LATER_AT)
    assert report.facts.record_count == 0
    assert report.facts.tracked_symbol is None
    assert report.outcome.result is not None


def test_build_user_prompt_carries_the_facts_json() -> None:
    """The prompt embeds the composed facts so the answer stays grounded."""
    facts = compose_window_facts([], None, None, None, None, (), LATER_AT)
    prompt = build_user_prompt(facts)
    assert "record_count" in prompt
    assert json.loads(prompt.split("\n\n", 1)[1])["record_count"] == 0


def test_resolve_report_path_defaults_beside_the_audit_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report lands beside the audit store unless the override is sealed."""
    settings = Settings()
    default = resolve_report_path({}, settings)
    assert default == settings.audit_database_path.parent / "advisor_last_report.json"
    override = resolve_report_path(
        {ADVISOR_REPORT_PATH_ENV: str(tmp_path / "scratch.json")}, settings
    )
    assert override == tmp_path / "scratch.json"


def test_cli_dark_posture_exits_zero_with_a_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without sealed values the command is dark, not failed."""
    monkeypatch.delenv(ADVISOR_URL_ENV, raising=False)
    monkeypatch.delenv(ADVISOR_MODEL_ENV, raising=False)
    assert main(["--max-runs", "1"]) == 0
    assert "disabled" in capsys.readouterr().err


def test_cli_invalid_configuration_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed sealed value fails closed with the variable named."""
    monkeypatch.setenv(ADVISOR_URL_ENV, "ftp://plane")
    assert main([]) == 1
    assert ADVISOR_URL_ENV in capsys.readouterr().err


def test_cli_runs_one_pass_and_prints_the_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sealed environment runs one bounded pass end to end over fakes."""
    audit_path = tmp_path / "audit" / "audit.sqlite3"
    seeded_store(tmp_path)
    state_path = tmp_path / "cycle_state.json"
    CycleStateStore(state_path).save(tracked_book())
    report_path = tmp_path / "advisor_last_report.json"
    monkeypatch.setenv("AERO_BOT_AUDIT_DATABASE_PATH", str(audit_path))
    monkeypatch.setenv(CYCLE_STATE_PATH_ENV, str(state_path))
    monkeypatch.setenv(ADVISOR_REPORT_PATH_ENV, str(report_path))
    monkeypatch.setenv(ADVISOR_URL_ENV, "http://plane")
    monkeypatch.setenv(ADVISOR_MODEL_ENV, "qwen3.6:35b-a3b")

    scripted = ScriptedTransport(
        response=AdvisorHttpResponse(status_code=200, body=chat_body(json.dumps(accepted_answer())))
    )
    monkeypatch.setattr(
        "aero_bot.advisor.HttpxAdvisorTransport", lambda: cast("HttpxAdvisorTransport", scripted)
    )
    assert main(["--max-runs", "1"]) == 0
    captured = capsys.readouterr()
    assert "advisor pass" in captured.out
    assert "long_range_wait" in captured.out
    assert report_path.exists()


class TestTeachingBlock:
    """The sealed teaching block appends, never replaces, fail-closed."""

    def test_without_a_teaching_file_the_default_prompt_stands(self) -> None:
        """An unset teaching file keeps the in-repo system prompt alone."""
        composed = compose_system_prompt(AdvisorConfig(url="http://plane", model="m"))
        assert composed == ADVISOR_SYSTEM_PROMPT

    def test_the_block_appends_behind_the_fixed_separator(self, tmp_path: Path) -> None:
        """A sealed block appends behind the separator, contract intact."""
        block = "Watch the halt counter across adjacent snapshots."
        teaching = tmp_path / "teaching.txt"
        teaching.write_text(f"\n  {block}  \n", encoding="utf-8")
        config = AdvisorConfig(url="http://plane", model="m", teaching_file=str(teaching))
        composed = compose_system_prompt(config)
        assert composed == ADVISOR_SYSTEM_PROMPT + ADVISOR_TEACHING_SEPARATOR + block
        # The answer contract always survives a sealed block.
        assert composed.index("exactly one JSON object") < composed.index(block)

    def test_missing_empty_oversized_and_binary_files_fail_closed(self, tmp_path: Path) -> None:
        """Every unreadable or invalid teaching file names the variable."""
        missing = AdvisorConfig(
            url="http://plane", model="m", teaching_file=str(tmp_path / "absent.txt")
        )
        with pytest.raises(ValueError, match=ADVISOR_TEACHING_FILE_ENV):
            compose_system_prompt(missing)
        empty = tmp_path / "empty.txt"
        empty.write_text("   \n", encoding="utf-8")
        with pytest.raises(ValueError, match=ADVISOR_TEACHING_FILE_ENV):
            compose_system_prompt(
                AdvisorConfig(url="http://plane", model="m", teaching_file=str(empty))
            )
        oversized = tmp_path / "oversized.txt"
        oversized.write_text("x" * (ADVISOR_TEACHING_MAX_CHARS + 1), encoding="utf-8")
        with pytest.raises(ValueError, match=ADVISOR_TEACHING_FILE_ENV):
            compose_system_prompt(
                AdvisorConfig(url="http://plane", model="m", teaching_file=str(oversized))
            )
        binary = tmp_path / "binary.txt"
        binary.write_bytes(b"\xff\xfe\x00")
        with pytest.raises(ValueError, match=ADVISOR_TEACHING_FILE_ENV):
            compose_system_prompt(
                AdvisorConfig(url="http://plane", model="m", teaching_file=str(binary))
            )

    def test_the_configuration_requires_an_absolute_path(self) -> None:
        """A relative teaching path is rejected at parse time."""
        parsed = parse_advisor_config(
            {ADVISOR_URL_ENV: "http://plane", ADVISOR_TEACHING_FILE_ENV: "/etc/aero-bot/t.txt"}
        )
        assert parsed.teaching_file == "/etc/aero-bot/t.txt"
        with pytest.raises(ValueError, match=ADVISOR_TEACHING_FILE_ENV):
            parse_advisor_config({ADVISOR_TEACHING_FILE_ENV: "relative.txt"})

    def test_the_monitor_threads_the_composed_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_once asks the plane with the teaching block appended."""
        teaching = tmp_path / "teaching.txt"
        block = "Prefer halt-count deltas over single snapshots."
        teaching.write_text(block, encoding="utf-8")
        transport = ScriptedTransport(
            response=AdvisorHttpResponse(
                status_code=200, body=chat_body(json.dumps(accepted_answer()))
            )
        )
        monitor = AdvisorMonitor(
            AuditStore(tmp_path / "audit" / "audit.sqlite3"),
            CycleStateStore(tmp_path / "cycle_state.json"),
            AdvisorConfig(url="http://plane", model="m", teaching_file=str(teaching)),
            transport,
            tmp_path / "advisor_last_report.json",
        )
        monitor.run_once(now=LATER_AT)
        messages = cast("list[Mapping[str, object]]", transport.requests[0][1]["messages"])
        assert messages[0]["role"] == "system"
        assert cast("str", messages[0]["content"]).endswith(ADVISOR_TEACHING_SEPARATOR + block)

    def test_the_cli_fails_closed_when_the_seal_is_broken(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A configured but unreadable teaching file exits one, named."""
        monkeypatch.setenv("AERO_BOT_AUDIT_DATABASE_PATH", str(tmp_path / "audit.sqlite3"))
        monkeypatch.setenv(CYCLE_STATE_PATH_ENV, str(tmp_path / "cycle_state.json"))
        monkeypatch.setenv(ADVISOR_URL_ENV, "http://plane")
        monkeypatch.setenv(ADVISOR_MODEL_ENV, "m")
        monkeypatch.setenv(ADVISOR_TEACHING_FILE_ENV, str(tmp_path / "absent.txt"))
        assert main(["--max-runs", "1"]) == 1
        assert ADVISOR_TEACHING_FILE_ENV in capsys.readouterr().err

    def test_usage_errors_exit_two(self) -> None:
        """The bounded flags refuse zero runs and negative intervals."""
        with pytest.raises(SystemExit) as raised:
            main(["--max-runs", "0"])
        assert raised.value.code == 2
        with pytest.raises(SystemExit) as raised:
            main(["--max-runs", "2", "--interval-seconds", "-1"])
        assert raised.value.code == 2
