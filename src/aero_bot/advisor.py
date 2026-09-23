"""The shadow advisor: a bounded, fail-closed intelligence surface.

The intelligence layer exists to observe the bot's own audited history and
offer prose interpretation - never to act. The ``aero-bot-advisor`` command
reads the recent ``cycle_reported`` audit records plus the cycle book,
composes a deterministic factual picture (held position and its day
economics, fee evidence, decision cadence, cooldown churn, halted flags),
and asks one language model - served from a private inference plane behind
an ``AERO_BOT_ADVISOR_URL`` the operator seals - for a concise brief and
anomaly flags. The result is printed, persisted beside the audit store, and
appended to the audit chain as one ``advisor_reported`` record.

The fail-closed posture is absolute: the advisor is dark until a URL and
model are configured, every transport failure, timeout, malformed body, or
schema-invalid answer becomes a typed absence with a stable reason - never
an exception into a caller's path and never a guess - and the surface sends
nothing, signs nothing, and never writes the cycle book. Trading authority
stays exactly where it was: the locked policy engine inside the cycle.

Discipline:

- **Advisory only.** No executor boundary, no signing key path, no alert
  transport; the report is the entire effect.
- **Bounded output.** The model must answer in one JSON object validated
  against a strict schema (a short brief, at most ten anomaly flags with
  bounded confidence); anything else is a schema-invalid absence.
- **Dark by default.** The URL defaults empty and the systemd unit ships
  installed but gated on a sealed overlay file, so nothing runs until the
  deploy operator arms it (see ``docs/advisor.md``).
"""

import argparse
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol, TextIO, cast

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from aero_bot.audit import MAX_RECORDS_PER_READ, AuditEventType, AuditRecord, AuditStore
from aero_bot.config import Settings
from aero_bot.cycle import CycleStateStore
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG

# Environment variable carrying the inference plane's base URL; empty keeps
# the advisor dark.
ADVISOR_URL_ENV = "AERO_BOT_ADVISOR_URL"
# Environment variable naming the model the plane serves for this surface.
ADVISOR_MODEL_ENV = "AERO_BOT_ADVISOR_MODEL"
# Environment variable carrying the request timeout in seconds.
ADVISOR_TIMEOUT_ENV = "AERO_BOT_ADVISOR_TIMEOUT_SECONDS"
# Environment variable overriding the bounded generation budget in tokens.
ADVISOR_MAX_TOKENS_ENV = "AERO_BOT_ADVISOR_MAX_TOKENS"
# Environment variable disabling reasoning-model thinking where the plane
# supports it (Ollama's think parameter); empty leaves thinking on.
ADVISOR_DISABLE_THINKING_ENV = "AERO_BOT_ADVISOR_DISABLE_THINKING"
# Environment variable overriding the persisted report's path (default:
# advisor_last_report.json beside the audit store).
ADVISOR_REPORT_PATH_ENV = "AERO_BOT_ADVISOR_REPORT_PATH"

# The default request timeout: short enough that a slow plane never delays
# an operator loop, long enough for a local model's first token.
DEFAULT_ADVISOR_TIMEOUT_SECONDS = 15.0
# The bounded generation budget. The validated answer is a short JSON
# object, but reasoning models spend tokens thinking before their content:
# the budget must cover thinking plus answer or the plane returns an empty
# content under a length cutoff (observed live with qwen3.6:35b-a3b).
ADVISOR_MAX_OUTPUT_TOKENS = 4096
# The bounded generation budget's accepted range.
ADVISOR_MAX_TOKENS_BOUNDS = (200, 32_768)
# The bounded brief the schema accepts; longer model prose is invalid.
ADVISOR_BRIEF_MAX_CHARS = 2000
# The bounded anomaly count the schema accepts.
ADVISOR_ANOMALY_MAX_COUNT = 10
# The default report file name beside the audit store.
DEFAULT_ADVISOR_REPORT_NAME = "advisor_last_report.json"
# How many recent audit records the monitor composes its window from.
ADVISOR_WINDOW_RECORDS = 40


class AdvisorAbsentReason(StrEnum):
    """Name every stable reason an advisory answer can be absent."""

    # The surface is unconfigured: no URL or no model sealed.
    DARK = "dark"
    # The transport could not reach the plane at all.
    UNREACHABLE = "unreachable"
    # The transport reached the plane but exceeded the timeout.
    TIMEOUT = "timeout"
    # The plane answered a non-200 status.
    HTTP_STATUS = "http_status"
    # The plane answered no message content.
    EMPTY_CONTENT = "empty_content"
    # The content was not parseable JSON.
    MALFORMED_JSON = "malformed_json"
    # The JSON violated the bounded brief schema.
    SCHEMA_INVALID = "schema_invalid"


class AdvisorAnomaly(BaseModel):
    """Carry one flagged observation with its confidence and rationale."""

    # Frozen strict fields keep every anomaly coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # A short stable label for the flag.
    label: Annotated[str, Field(min_length=1, max_length=120)]
    # The model's confidence in the flag, bounded to the unit interval.
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    # One line saying why the flag was raised.
    rationale: Annotated[str, Field(min_length=1, max_length=400)]


class AdvisorBrief(BaseModel):
    """Validate the bounded JSON object the advisory contract demands."""

    # Frozen strict fields keep one accepted answer immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # A concise prose reading of the composed facts.
    brief: Annotated[str, Field(min_length=1, max_length=ADVISOR_BRIEF_MAX_CHARS)]
    # Zero or more anomaly flags; absence of anomalies is a valid answer.
    anomalies: Annotated[tuple[AdvisorAnomaly, ...], Field(max_length=ADVISOR_ANOMALY_MAX_COUNT)]


class AdvisorResult(BaseModel):
    """Carry one accepted advisory answer with its serving metadata."""

    # The model name that produced the answer.
    model: str
    # The validated bounded answer.
    brief: AdvisorBrief
    # Wall-clock seconds the request consumed.
    elapsed_seconds: Annotated[float, Field(ge=0.0)]


class AdvisorOutcome(BaseModel):
    """Carry either one accepted answer or one typed absence, never both."""

    # Exactly one of result or reason is set on every outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The accepted answer, else None.
    result: AdvisorResult | None = None
    # The stable absence reason, else None.
    reason: AdvisorAbsentReason | None = None

    @model_validator(mode="after")
    def _exactly_one_branch(self) -> "AdvisorOutcome":
        """Reject any outcome carrying both branches or neither."""
        if (self.result is None) == (self.reason is None):
            raise ValueError("exactly one of result or reason must be set")
        return self

    @property
    def status(self) -> str:
        """Expose the outcome's one-word status for reports and audit."""
        if self.result is not None:
            return "brief"
        # The exactly-one validator guarantees the reason branch here.
        return cast(AdvisorAbsentReason, self.reason).value


class AdvisorConfig(BaseModel):
    """Carry the sealed advisory-surface configuration."""

    # The inference plane's base URL; empty keeps the surface dark.
    url: str = ""
    # The model the plane serves; empty keeps the surface dark.
    model: str = ""
    # The bounded request timeout in seconds.
    timeout_seconds: Annotated[float, Field(gt=0.0, le=120.0)] = DEFAULT_ADVISOR_TIMEOUT_SECONDS
    # The bounded generation budget in tokens, covering any reasoning
    # tokens plus the short JSON answer.
    max_tokens: Annotated[
        int, Field(ge=ADVISOR_MAX_TOKENS_BOUNDS[0], le=ADVISOR_MAX_TOKENS_BOUNDS[1])
    ] = ADVISOR_MAX_OUTPUT_TOKENS
    # Whether to ask reasoning models not to think, where the plane
    # supports the Ollama think parameter.
    disable_thinking: bool = False

    @property
    def enabled(self) -> bool:
        """Report whether both required sealing values are present."""
        return bool(self.url.strip()) and bool(self.model.strip())


class AdvisorHttpResponse(BaseModel):
    """Carry one transport answer's status and body without headers."""

    # The HTTP status code the plane returned.
    status_code: int
    # The response body text, exactly as received.
    body: str


class AdvisorTransport(Protocol):
    """Define the one-method surface an advisory request travels through."""

    def complete(
        self,
        url: str,
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> AdvisorHttpResponse:
        """Deliver one request and return the raw response.

        Args:
            url: The complete chat-completions endpoint URL.
            payload: The OpenAI-compatible request body.
            timeout_seconds: The bounded wall-clock request timeout.

        Returns:
            The status and body the plane answered.

        Raises:
            AdvisorTimeoutError: The request exceeded its timeout.
            AdvisorUnreachableError: The plane could not be reached.
        """
        ...


class AdvisorTimeoutError(RuntimeError):
    """Indicate a request that exceeded its bounded timeout."""


class AdvisorUnreachableError(RuntimeError):
    """Indicate a plane that could not be reached at all."""


class HttpxAdvisorTransport:
    """Deliver advisory requests through one shared httpx client."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Bind the transport to a client, defaulting to a fresh one.

        Args:
            client: An injected httpx client (tests pass MockTransport here).
        """
        self._client = client if client is not None else httpx.Client()

    def complete(
        self,
        url: str,
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> AdvisorHttpResponse:
        """Post one OpenAI-compatible chat completion request.

        Args:
            url: The complete chat-completions endpoint URL.
            payload: The OpenAI-compatible request body.
            timeout_seconds: The bounded wall-clock request timeout.

        Returns:
            The status and body the plane answered.

        Raises:
            AdvisorTimeoutError: The request exceeded its timeout.
            AdvisorUnreachableError: The plane could not be reached.
        """
        try:
            response = self._client.post(
                url,
                json=dict(payload),
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise AdvisorTimeoutError(str(error)) from error
        except httpx.TransportError as error:
            raise AdvisorUnreachableError(str(error)) from error
        return AdvisorHttpResponse(status_code=response.status_code, body=response.text)


def _positive_float(value: str, variable: str) -> float:
    """Parse one strictly positive bounded float environment value.

    Args:
        value: The raw environment text.
        variable: The variable name for error messages.

    Returns:
        The parsed float.

    Raises:
        ValueError: If the text is not a positive bounded float.
    """
    try:
        parsed = float(value)
    except ValueError as error:
        # The message names the variable, never the malformed value text.
        raise ValueError(f"{variable} must be a number of seconds") from error
    if parsed <= 0 or parsed > 120.0:
        raise ValueError(f"{variable} must be between 0 and 120 seconds")
    return parsed


def parse_advisor_config(environ: Mapping[str, str] | None = None) -> AdvisorConfig:
    """Parse the advisory configuration from the environment.

    Args:
        environ: The mapping to read; None reads ``os.environ``.

    Returns:
        The validated configuration; an empty URL or model stays dark.

    Raises:
        ValueError: If a present value is malformed; the message names the
            variable, never any value.
    """
    source = os.environ if environ is None else environ
    url = source.get(ADVISOR_URL_ENV, "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError(f"{ADVISOR_URL_ENV} must be an http or https URL")
    timeout_text = source.get(ADVISOR_TIMEOUT_ENV, "").strip()
    timeout_seconds = (
        _positive_float(timeout_text, ADVISOR_TIMEOUT_ENV)
        if timeout_text
        else DEFAULT_ADVISOR_TIMEOUT_SECONDS
    )
    max_tokens_text = source.get(ADVISOR_MAX_TOKENS_ENV, "").strip()
    max_tokens = ADVISOR_MAX_OUTPUT_TOKENS
    if max_tokens_text:
        try:
            max_tokens = int(max_tokens_text)
        except ValueError as error:
            raise ValueError(f"{ADVISOR_MAX_TOKENS_ENV} must be a number of tokens") from error
        lower, upper = ADVISOR_MAX_TOKENS_BOUNDS
        if max_tokens < lower or max_tokens > upper:
            raise ValueError(f"{ADVISOR_MAX_TOKENS_ENV} must be between {lower} and {upper} tokens")
    disable_thinking_text = source.get(ADVISOR_DISABLE_THINKING_ENV, "").strip().lower()
    if disable_thinking_text and disable_thinking_text not in {
        "1",
        "true",
        "yes",
        "0",
        "false",
        "no",
    }:
        raise ValueError(f"{ADVISOR_DISABLE_THINKING_ENV} must be a boolean")
    disable_thinking = disable_thinking_text in {"1", "true", "yes"}
    return AdvisorConfig(
        url=url,
        model=source.get(ADVISOR_MODEL_ENV, "").strip(),
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        disable_thinking=disable_thinking,
    )


# A code fence or leading prose wrapper around the JSON body; the advisor
# contract tolerates fences but nothing else before the first brace.
_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json_object(content: str) -> str:
    """Extract the first balanced JSON object from model content.

    Args:
        content: The raw message content, possibly fence-wrapped.

    Returns:
        The substring from the first ``{`` through its balanced close.
    """
    fenced = _FENCED_JSON.search(content)
    if fenced is not None:
        content = fenced.group(1)
    start = content.find("{")
    if start < 0:
        return content
    depth = 0
    for index in range(start, len(content)):
        character = content[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
    return content[start:]


def request_brief(
    config: AdvisorConfig,
    system_prompt: str,
    user_prompt: str,
    transport: AdvisorTransport,
) -> AdvisorOutcome:
    """Request one bounded advisory answer, never raising to the caller.

    Args:
        config: The sealed configuration; dark yields a dark outcome.
        system_prompt: The bounded role and contract instructions.
        user_prompt: The composed factual prompt.
        transport: The delivery surface; failures become typed absences.

    Returns:
        Exactly one of an accepted result or a typed absence reason.
    """
    if not config.enabled:
        return AdvisorOutcome(reason=AdvisorAbsentReason.DARK)
    endpoint = config.url.rstrip("/") + "/v1/chat/completions"
    payload: dict[str, object] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        # The budget covers reasoning tokens plus the short JSON answer.
        "max_tokens": config.max_tokens,
    }
    if config.disable_thinking:
        # Ollama's think parameter; reasoning-capable planes honor it and
        # strict OpenAI-compatible servers only see it when the operator
        # sealed the switch.
        payload["think"] = False
    started = time.monotonic()
    try:
        response = transport.complete(endpoint, payload, config.timeout_seconds)
    except AdvisorTimeoutError:
        return AdvisorOutcome(reason=AdvisorAbsentReason.TIMEOUT)
    except AdvisorUnreachableError:
        return AdvisorOutcome(reason=AdvisorAbsentReason.UNREACHABLE)
    elapsed = time.monotonic() - started
    if response.status_code != 200:
        return AdvisorOutcome(reason=AdvisorAbsentReason.HTTP_STATUS)
    try:
        body = json.loads(response.body)
        content = body["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return AdvisorOutcome(reason=AdvisorAbsentReason.MALFORMED_JSON)
    if not isinstance(content, str) or not content.strip():
        return AdvisorOutcome(reason=AdvisorAbsentReason.EMPTY_CONTENT)
    try:
        answer = json.loads(extract_json_object(content))
    except json.JSONDecodeError:
        return AdvisorOutcome(reason=AdvisorAbsentReason.MALFORMED_JSON)
    try:
        brief = AdvisorBrief.model_validate(answer)
    except ValidationError:
        return AdvisorOutcome(reason=AdvisorAbsentReason.SCHEMA_INVALID)
    return AdvisorOutcome(
        result=AdvisorResult(
            model=config.model,
            brief=brief,
            elapsed_seconds=elapsed,
        )
    )


class AdvisorWindowFacts(BaseModel):
    """Carry the deterministic picture composed from audited history."""

    # Frozen strict fields keep the composed facts immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # How many cycle records the window examined.
    record_count: Annotated[int, Field(ge=0)]
    # The tracked symbol when a position is live, else None.
    tracked_symbol: str | None = None
    # The tracked position's committed USDC, else None.
    committed_usdc: str | None = None
    # The out-of-range side when persisted, else None.
    out_of_range_side: str | None = None
    # Minutes since the persisted out-of-range anchor, else None.
    out_of_range_minutes: float | None = None
    # The latest report's portfolio equity, else None.
    equity_usdc: str | None = None
    # The latest day-start anchor, else None.
    day_start_equity_usdc: str | None = None
    # The latest day P&L, else None.
    day_pnl_usdc: str | None = None
    # The latest claimable pool fees, else None.
    claimable_pool_fees_usdc: str | None = None
    # The latest measured fee APR fraction, else None.
    measured_fee_apr: str | None = None
    # How many windowed cycles halted.
    halted_count: Annotated[int, Field(ge=0)] = 0
    # How many windowed cycles attempted actions.
    acting_count: Annotated[int, Field(ge=0)] = 0
    # The distinct decision reasons seen, most recent first.
    reasons: tuple[str, ...] = ()
    # Distinct symbols under re-entry cooldown in the book.
    cooldown_symbols: tuple[str, ...] = ()
    # The most recent cycle's action.
    latest_action: str | None = None


class AdvisorMonitorReport(BaseModel):
    """Carry one complete monitor pass: facts plus the advisory outcome."""

    # Frozen strict fields keep one report coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # When the pass ran, timezone-aware.
    created_at: datetime
    # The deterministic facts the pass composed.
    facts: AdvisorWindowFacts
    # The advisory answer or its typed absence.
    outcome: AdvisorOutcome


def compose_window_facts(
    records: Sequence[AuditRecord],
    tracked_symbol: str | None,
    committed_usdc: str | None,
    out_of_range_side: str | None,
    out_of_range_since: datetime | None,
    cooldown_symbols: tuple[str, ...],
    now: datetime,
) -> AdvisorWindowFacts:
    """Compose the deterministic factual picture from audited records.

    Args:
        records: The recent audit records (any types; only cycle summaries
            are consumed).
        tracked_symbol: The book's tracked symbol, else None.
        committed_usdc: The tracked position's committed value, else None.
        out_of_range_side: The book's persisted range-wait side, else None.
        out_of_range_since: The book's persisted range-wait anchor, else None.
        cooldown_symbols: The book's cooldown symbols.
        now: The pass's reference time, timezone-aware.

    Returns:
        The immutable composed facts.
    """
    summaries: list[dict[str, object]] = []
    for record in records:
        if record.event_type is not AuditEventType.CYCLE_REPORTED:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            summaries.append(json.loads(record.payload_json))
    latest: dict[str, object] | None = summaries[-1] if summaries else None

    def _text(key: str) -> str | None:
        if latest is None:
            return None
        value = latest.get(key)
        return value if isinstance(value, str) else None

    def _int(summary: Mapping[str, object], key: str) -> int:
        # Defensive reads keep a malformed payload from crashing the monitor.
        value = summary.get(key, 0)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return 0
        return 0

    halted_count = sum(1 for summary in summaries if str(summary.get("halted_reason", "")) != "")
    acting_count = sum(1 for summary in summaries if _int(summary, "action_count") > 0)
    reasons: list[str] = []
    for summary in reversed(summaries):
        reason = str(summary.get("reason", ""))
        if reason and reason not in reasons:
            reasons.append(reason)
    out_of_range_minutes = (
        (now - out_of_range_since).total_seconds() / 60 if out_of_range_since is not None else None
    )
    return AdvisorWindowFacts(
        record_count=len(summaries),
        tracked_symbol=tracked_symbol,
        committed_usdc=committed_usdc,
        out_of_range_side=out_of_range_side,
        out_of_range_minutes=out_of_range_minutes,
        equity_usdc=_text("equity_usdc"),
        day_start_equity_usdc=_text("day_start_equity_usdc"),
        day_pnl_usdc=_text("day_pnl_usdc"),
        claimable_pool_fees_usdc=_text("claimable_pool_fees_usdc"),
        measured_fee_apr=_text("measured_fee_apr"),
        halted_count=halted_count,
        acting_count=acting_count,
        reasons=tuple(reasons[:8]),
        cooldown_symbols=cooldown_symbols,
        latest_action=str(latest["action"]) if latest is not None and "action" in latest else None,
    )


ADVISOR_SYSTEM_PROMPT = (
    "You are the shadow advisor for an autonomous emissions-farming bot. "
    "You observe audited facts and interpret them. You have no authority "
    "and no ability to trade. Answer with exactly one JSON object and no "
    'other text: {"brief": string, "anomalies": [{"label": string, '
    '"confidence": number, "rationale": string}]}. The brief is at most '
    "three sentences describing what happened. Anomalies list at most ten "
    "concerning observations (label at most 120 characters, confidence "
    "between 0 and 1, rationale one sentence); an empty list is a valid "
    "answer. Ground every statement in the provided facts; never invent "
    "numbers."
)


def build_user_prompt(facts: AdvisorWindowFacts) -> str:
    """Render the composed facts as the bounded user prompt.

    Args:
        facts: The deterministic composed facts.

    Returns:
        The prompt text carrying the facts as JSON.
    """
    return "Recent audited bot history follows. Interpret it per your contract.\n\n" + json.dumps(
        json.loads(facts.model_dump_json()), indent=2
    )


class AdvisorMonitor:
    """Run one advisory pass over the local audit history and book."""

    def __init__(
        self,
        audit_store: AuditStore,
        state_store: CycleStateStore,
        config: AdvisorConfig,
        transport: AdvisorTransport,
        report_path: Path,
        *,
        window_records: int = ADVISOR_WINDOW_RECORDS,
    ) -> None:
        """Bind the monitor to its stores, transport, and report path.

        Args:
            audit_store: The audit chain the monitor reads and appends to.
            state_store: The cycle book store the monitor reads.
            config: The sealed advisory configuration.
            transport: The delivery surface for the advisory request.
            report_path: The persisted report file's path.
            window_records: How many recent records to compose.
        """
        self._audit_store = audit_store
        self._state_store = state_store
        self._config = config
        self._transport = transport
        self._report_path = report_path
        self._window_records = window_records

    def run_once(self, now: datetime | None = None) -> AdvisorMonitorReport:
        """Compose facts, request the advisory answer, persist, and audit.

        Args:
            now: The pass's reference time; None reads the clock.

        Returns:
            The complete monitor report.
        """
        moment = now if now is not None else datetime.now(UTC)
        book = self._state_store.load()
        # Read the bounded most-recent window by paginating from the end; an
        # empty store reads nothing rather than a zero-limit error.
        depth = _store_depth(self._audit_store)
        records = (
            self._audit_store.read_records(
                limit=min(self._window_records, depth),
                offset=max(0, depth - self._window_records),
            )
            if depth
            else ()
        )
        facts = compose_window_facts(
            records,
            tracked_symbol=book.position.symbol if book.position is not None else None,
            committed_usdc=(
                str(book.position.committed_usd) if book.position is not None else None
            ),
            out_of_range_side=(
                book.position.out_of_range_side if book.position is not None else None
            ),
            out_of_range_since=(
                book.position.out_of_range_since if book.position is not None else None
            ),
            cooldown_symbols=tuple(cooldown.symbol for cooldown in book.reentry_cooldowns),
            now=moment,
        )
        outcome = request_brief(
            self._config,
            ADVISOR_SYSTEM_PROMPT,
            build_user_prompt(facts),
            self._transport,
        )
        report = AdvisorMonitorReport(created_at=moment, facts=facts, outcome=outcome)
        self._persist(report)
        self._audit(report)
        return report

    def _persist(self, report: AdvisorMonitorReport) -> None:
        """Atomically rewrite the persisted report file.

        Args:
            report: The complete report to persist.
        """
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._report_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(json.loads(report.model_dump_json()), indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._report_path)

    def _audit(self, report: AdvisorMonitorReport) -> None:
        """Append the advisory-summary record to the audit chain.

        Args:
            report: The complete report to record.
        """
        self._audit_store.append(
            AuditEventType.ADVISOR_REPORTED,
            AdvisorReportedAuditPayload(
                outcome=report.outcome.status,
                model=report.outcome.result.model if report.outcome.result is not None else "",
                brief=report.outcome.result.brief.brief
                if report.outcome.result is not None
                else "",
                anomalies=tuple(
                    AdvisorAnomalyAuditPayload(
                        label=anomaly.label,
                        confidence=anomaly.confidence,
                        rationale=anomaly.rationale,
                    )
                    for anomaly in (
                        report.outcome.result.brief.anomalies
                        if report.outcome.result is not None
                        else ()
                    )
                ),
                latency_ms=int(
                    round(
                        (report.outcome.result.elapsed_seconds if report.outcome.result else 0)
                        * 1000
                    )
                ),
                window_records=report.facts.record_count,
            ),
            report.created_at,
        )


class AdvisorAnomalyAuditPayload(BaseModel):
    """Carry one audited anomaly flag."""

    # Frozen strict fields keep one flag coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The flag's short label.
    label: str
    # The flag's bounded confidence.
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    # The flag's one-line rationale.
    rationale: str


class AdvisorReportedAuditPayload(BaseModel):
    """Persist one advisory-summary record on the audit chain."""

    # Frozen strict fields keep the audited summary immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # brief when an answer was accepted, else the stable absence reason.
    outcome: str
    # The answering model name, empty on absence.
    model: str
    # The accepted brief text, empty on absence.
    brief: Annotated[str, Field(max_length=ADVISOR_BRIEF_MAX_CHARS)] = ""
    # The accepted anomaly flags, empty on absence.
    anomalies: tuple[AdvisorAnomalyAuditPayload, ...] = ()
    # The request latency in milliseconds, zero on absence.
    latency_ms: Annotated[int, Field(ge=0)] = 0
    # How many cycle summaries the window examined.
    window_records: Annotated[int, Field(ge=0)] = 0


def _store_depth(store: AuditStore) -> int:
    """Count the audit chain's records with bounded pagination.

    Args:
        store: The audit store to measure.

    Returns:
        The total number of durable records.
    """
    total = 0
    step = MAX_RECORDS_PER_READ
    while True:
        page = store.read_records(limit=step, offset=total)
        if not page:
            return total
        total += len(page)


def resolve_report_path(environ: Mapping[str, str], settings: Settings) -> Path:
    """Resolve the persisted report path from the environment or settings.

    Args:
        environ: The environment mapping to read.
        settings: The application settings supplying the default location.

    Returns:
        The absolute report path beside the audit store by default.
    """
    override = environ.get(ADVISOR_REPORT_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return settings.audit_database_path.parent / DEFAULT_ADVISOR_REPORT_NAME


def _print_report(report: AdvisorMonitorReport, stream: TextIO) -> None:
    """Print one monitor pass's human summary.

    Args:
        report: The complete report to print.
        stream: The destination stream.
    """
    facts = report.facts
    print(f"advisor pass {report.created_at.isoformat()} over {facts.record_count} cycle records")
    if facts.tracked_symbol is not None:
        print(
            f"  tracked: {facts.tracked_symbol} committed {facts.committed_usdc} USDC "
            f"({facts.out_of_range_side or 'in range'}"
            + (
                f" for {facts.out_of_range_minutes:.0f} min"
                if facts.out_of_range_minutes is not None
                else ""
            )
            + ")"
        )
    if facts.day_pnl_usdc is not None:
        print(
            f"  day: pnl {facts.day_pnl_usdc} USDC, equity {facts.equity_usdc} USDC "
            f"from anchor {facts.day_start_equity_usdc} USDC"
        )
    if facts.measured_fee_apr is not None:
        print(
            f"  fees: claimable {facts.claimable_pool_fees_usdc} USDC, "
            f"measured APR {facts.measured_fee_apr}"
        )
    outcome = report.outcome
    if outcome.result is not None:
        print(f"  brief [{outcome.result.model}]: {outcome.result.brief.brief}")
        for anomaly in outcome.result.brief.anomalies:
            print(f"  anomaly: {anomaly.label} (confidence {anomaly.confidence:.2f})")
    else:
        print(f"  advisor absent: {outcome.reason and outcome.reason.value}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the shadow advisor monitor.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on a clean pass (a dark advisor is
        dark, not failed), one on configuration or store failures.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-advisor",
        description=(
            "Run the shadow advisor: compose the audited factual picture "
            "and request one bounded advisory brief. Advisory only - it "
            "sends nothing, signs nothing, and never touches the cycle "
            "book. Dark until AERO_BOT_ADVISOR_URL and "
            "AERO_BOT_ADVISOR_MODEL are sealed."
        ),
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=1,
        help="Bound the run to N passes (default 1); smoke checks use this.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=0.0,
        help="Seconds to wait between passes when max-runs exceeds one.",
    )
    arguments = parser.parse_args(argv)
    if arguments.max_runs < 1:
        parser.error("--max-runs must be at least 1")
    if arguments.interval_seconds < 0:
        parser.error("--interval-seconds cannot be negative")
    try:
        config = parse_advisor_config(os.environ)
    except ValueError as error:
        print(f"the advisor configuration is invalid: {error}", file=sys.stderr)
        return 1
    if not config.enabled:
        print(
            "disabled: set AERO_BOT_ADVISOR_URL and AERO_BOT_ADVISOR_MODEL "
            "in the sealed environment to arm it",
            file=sys.stderr,
        )
        return 0
    settings = Settings()
    monitor = AdvisorMonitor(
        AuditStore(settings.audit_database_path),
        CycleStateStore.from_environment(os.environ, settings),
        config,
        HttpxAdvisorTransport(),
        resolve_report_path(os.environ, settings),
    )
    for run in range(arguments.max_runs):
        if run and arguments.interval_seconds:
            time.sleep(arguments.interval_seconds)
        try:
            report = monitor.run_once()
        except (OSError, ValueError, RuntimeError) as error:
            print(f"the advisor pass failed: {error}", file=sys.stderr)
            return 1
        _print_report(report, sys.stdout)
    return 0


__all__ = [
    "ADVISOR_ANOMALY_MAX_COUNT",
    "ADVISOR_BRIEF_MAX_CHARS",
    "ADVISOR_DISABLE_THINKING_ENV",
    "ADVISOR_MAX_OUTPUT_TOKENS",
    "ADVISOR_MAX_TOKENS_BOUNDS",
    "ADVISOR_MAX_TOKENS_ENV",
    "ADVISOR_MODEL_ENV",
    "ADVISOR_REPORT_PATH_ENV",
    "ADVISOR_SYSTEM_PROMPT",
    "ADVISOR_TIMEOUT_ENV",
    "ADVISOR_URL_ENV",
    "ADVISOR_WINDOW_RECORDS",
    "AdvisorAbsentReason",
    "AdvisorAnomaly",
    "AdvisorBrief",
    "AdvisorConfig",
    "AdvisorHttpResponse",
    "AdvisorMonitor",
    "AdvisorMonitorReport",
    "AdvisorOutcome",
    "AdvisorReportedAuditPayload",
    "AdvisorResult",
    "AdvisorTimeoutError",
    "AdvisorTransport",
    "AdvisorUnreachableError",
    "AdvisorWindowFacts",
    "HttpxAdvisorTransport",
    "build_user_prompt",
    "compose_window_facts",
    "extract_json_object",
    "main",
    "parse_advisor_config",
    "request_brief",
    "resolve_report_path",
]
