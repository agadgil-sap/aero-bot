#!/usr/bin/env python3
"""Score candidate advisor models against the deterministic baseline.

The bake-off is the intelligence layer's selection instrument: the shadow
advisor will carry one sealed model, and this harness gives that seat to
whichever contestant earns it over committed scenario fixtures - never over
live trading state. Three tasks, each with a deterministic truth the repo
itself computes:

- **Regime.** Classify a price window as one of four allowed labels and echo
  the window's return and swing percents; the baseline is the stated
  deterministic rule, and the echo proves the answer is grounded in the
  fixture's numbers rather than invented.
- **Board.** Rank a pool board; the qualified set must survive exactly (a
  permutation, nothing dropped, nothing unqualified admitted) while the
  order is free. The baseline orders by emissions APR descending.
- **Width.** Given ranging observables, answer the repo solver's own two
  facts - whether the tightest candidate meets the target and the selected
  half width in ticks - by running ``solve_range_width`` as truth.

The deterministic baseline contestant always runs and costs nothing; model
contestants run only when the operator names them (``--model``, repeatable)
against one inference plane (``--url``, defaulting to the sealed advisor
environment). Scores print and land as one markdown report under
``run/bakeoff/``.

Discipline: replay committed fixtures only, no live reads, no signing, no
cycle state; a contestant that answers unparseable, schema-invalid, or
fabricated numbers scores zero on that scenario and is marked, never
guessed for.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from aero_bot.advisor import (
    ADVISOR_URL_ENV,
    AdvisorConfig,
    AdvisorTimeoutError,
    AdvisorUnreachableError,
    HttpxAdvisorTransport,
    extract_json_object,
)
from aero_bot.ranging import RangingObservations, solve_range_width

DEFAULT_FIXTURE_PATH = Path("data/bakeoff/fixtures.json")
DEFAULT_REPORT_DIRECTORY = Path("run/bakeoff")
DEFAULT_CONTEST_TIMEOUT_SECONDS = 120.0
# The evidence tolerance: answers may round but not fabricate.
PERCENT_TOLERANCE = Decimal("0.25")
# The APR echo tolerance: two percent relative.
APR_RELATIVE_TOLERANCE = Decimal("0.02")
ALLOWED_REGIME_LABELS = ("trending_up", "trending_down", "volatile", "steady")


@dataclass(frozen=True)
class ScenarioScore:
    """Carry one scenario's points, ceiling, and honest notes."""

    name: str
    points: float
    ceiling: float
    notes: tuple[str, ...] = ()

    @property
    def fraction(self) -> float:
        """Return points over ceiling, zero when the ceiling itself is zero."""
        return self.points / self.ceiling if self.ceiling else 0.0


@dataclass(frozen=True)
class ContestantResult:
    """Carry one contestant's complete bake-off result."""

    name: str
    kind: str
    scenarios: tuple[ScenarioScore, ...]
    elapsed_seconds: float = 0.0

    @property
    def points(self) -> float:
        """Sum every scenario's points."""
        return sum(score.points for score in self.scenarios)

    @property
    def ceiling(self) -> float:
        """Sum every scenario's ceiling."""
        return sum(score.ceiling for score in self.scenarios)

    @property
    def fraction(self) -> float:
        """Return total points over total ceiling."""
        return self.points / self.ceiling if self.ceiling else 0.0


@dataclass(frozen=True)
class Fixtures:
    """Carry the committed scenario fixtures split by task."""

    regime: tuple[dict[str, Any], ...]
    board: tuple[dict[str, Any], ...]
    width: tuple[dict[str, Any], ...]


@dataclass
class Answer:
    """Hold one contestant's parsed answer or its typed failure."""

    payload: dict[str, Any] | None = None
    failure: str = ""


def load_fixtures(path: Path) -> Fixtures:
    """Load and minimally shape the committed fixture file.

    Args:
        path: Fixture JSON path.

    Returns:
        The three task fixture tuples.

    Raises:
        SystemExit: When the file is missing or not the expected shape.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load fixtures at {path}: {error}") from error
    for key in ("regime", "board", "width"):
        if not isinstance(raw.get(key), list):
            raise SystemExit(f"fixtures at {path} are missing the {key} task list")
    return Fixtures(
        regime=tuple(raw["regime"]),
        board=tuple(raw["board"]),
        width=tuple(raw["width"]),
    )


def baseline_regime(window: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    """Compute the deterministic regime answer for one price window.

    Args:
        window: Ordered window points with minutes and price fields.

    Returns:
        The label, the window return percent, and the swing percent.
    """
    prices = [Decimal(str(point["price"])) for point in window]
    total_return = (prices[-1] / prices[0] - Decimal(1)) * Decimal(100)
    swing = (max(prices) - min(prices)) / prices[0] * Decimal(100)
    if total_return >= Decimal(1):
        label = "trending_up"
    elif total_return <= Decimal(-1):
        label = "trending_down"
    elif swing >= Decimal(3):
        label = "volatile"
    else:
        label = "steady"
    return {
        "label": label,
        "return_percent": float(total_return),
        "swing_percent": float(swing),
    }


def baseline_board(pools: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute the deterministic board ranking: APR descending, qualified only.

    Args:
        pools: Board pools with symbol, qualified, and emissions_apr fields.

    Returns:
        The qualified-symbol ranking and the top pool's APR.
    """
    qualified = [pool for pool in pools if bool(pool["qualified"])]
    ordered = sorted(qualified, key=lambda pool: Decimal(str(pool["emissions_apr"])), reverse=True)
    return {
        "ranking": [str(pool["symbol"]) for pool in ordered],
        "top_apr": float(Decimal(str(ordered[0]["emissions_apr"]))),
    }


def baseline_width(observations: Mapping[str, Any]) -> dict[str, Any]:
    """Run the repo solver as the width task's deterministic truth.

    Args:
        observations: Ranging observation fields, decimal-valued as strings.

    Returns:
        The solver's mode and selected half width in ticks.
    """
    solution = solve_range_width(RangingObservations.model_validate(dict(observations)))
    return {"mode": solution.mode.value, "half_width_ticks": solution.half_width_ticks}


def _decimal_or_none(value: object) -> Decimal | None:
    """Coerce one answer number to Decimal, or None when it is not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - any malformed number is simply absent
        return None


def score_regime(scenario: Mapping[str, Any], answer: Answer) -> ScenarioScore:
    """Score one regime answer against the deterministic baseline.

    Args:
        scenario: The committed regime fixture.
        answer: The contestant's parsed answer or failure.

    Returns:
        One point for the exact label, one for grounded evidence numbers.
    """
    truth = baseline_regime(scenario["window"])
    if answer.payload is None:
        return ScenarioScore(scenario["name"], 0.0, 2.0, (answer.failure,))
    notes: list[str] = []
    points = 0.0
    label = answer.payload.get("label")
    if label == truth["label"] and label in ALLOWED_REGIME_LABELS:
        points += 1.0
    else:
        notes.append(f"label {label!r} != {truth['label']!r}")
    grounded = True
    for key in ("return_percent", "swing_percent"):
        value = _decimal_or_none(answer.payload.get(key))
        expected = Decimal(str(truth[key]))
        if value is None or abs(value - expected) > PERCENT_TOLERANCE:
            grounded = False
            notes.append(f"{key} not within {PERCENT_TOLERANCE} of {expected}")
    if grounded:
        points += 1.0
    return ScenarioScore(scenario["name"], points, 2.0, tuple(notes))


def score_board(scenario: Mapping[str, Any], answer: Answer) -> ScenarioScore:
    """Score one board answer: set preservation required, order compared.

    Args:
        scenario: The committed board fixture.
        answer: The contestant's parsed answer or failure.

    Returns:
        One point for top-pick agreement, one for the APR echo, one for
        pairwise order agreement; an invalid permutation scores zero.
    """
    truth = baseline_board(scenario["pools"])
    if answer.payload is None:
        return ScenarioScore(scenario["name"], 0.0, 3.0, (answer.failure,))
    ranking = answer.payload.get("ranking")
    if not isinstance(ranking, list) or sorted(str(s) for s in ranking) != sorted(truth["ranking"]):
        return ScenarioScore(
            scenario["name"],
            0.0,
            3.0,
            ("ranking is not a permutation of the qualified set",),
        )
    points = 0.0
    notes: list[str] = []
    if ranking and str(ranking[0]) == truth["ranking"][0]:
        points += 1.0
    else:
        notes.append(f"top {ranking[0] if ranking else None!r} != {truth['ranking'][0]!r}")
    top_apr = _decimal_or_none(answer.payload.get("top_apr"))
    expected_apr = Decimal(str(truth["top_apr"]))
    if top_apr is not None and abs(top_apr - expected_apr) <= APR_RELATIVE_TOLERANCE * expected_apr:
        points += 1.0
    else:
        notes.append("top_apr not grounded in the board's numbers")
    position = {symbol: index for index, symbol in enumerate(truth["ranking"])}
    pairs = 0
    agreements = 0
    for first in range(len(ranking)):
        for second in range(first + 1, len(ranking)):
            left = position.get(str(ranking[first]))
            right = position.get(str(ranking[second]))
            if left is None or right is None:
                continue
            pairs += 1
            if left < right:
                agreements += 1
    points += agreements / pairs if pairs else 1.0
    return ScenarioScore(scenario["name"], points, 3.0, tuple(notes))


def score_width(scenario: Mapping[str, Any], answer: Answer) -> ScenarioScore:
    """Score one width answer against the repo solver's own verdict.

    Args:
        scenario: The committed width fixture.
        answer: The contestant's parsed answer or failure.

    Returns:
        One point for the mode, one for the exact half width in ticks.
    """
    truth = baseline_width(scenario["observations"])
    if answer.payload is None:
        return ScenarioScore(scenario["name"], 0.0, 2.0, (answer.failure,))
    notes: list[str] = []
    points = 0.0
    mode = answer.payload.get("mode")
    if mode == truth["mode"]:
        points += 1.0
    else:
        notes.append(f"mode {mode!r} != {truth['mode']!r}")
    ticks = _decimal_or_none(answer.payload.get("half_width_ticks"))
    if ticks is not None and ticks == Decimal(truth["half_width_ticks"]):
        points += 1.0
    else:
        notes.append(f"half_width_ticks {ticks!r} != {truth['half_width_ticks']!r}")
    return ScenarioScore(scenario["name"], points, 2.0, tuple(notes))


REGIME_SYSTEM_PROMPT = (
    "You are analyzing one price window for an emissions-farming bot. "
    "Answer with exactly one JSON object and no other text: "
    '{"label": string, "return_percent": number, "swing_percent": number}. '
    "label is one of trending_up, trending_down, volatile, steady. "
    "return_percent is the window's total return in percent; swing_percent "
    "is (max-min)/first in percent. Ground both numbers in the provided "
    "prices; never invent values."
)

BOARD_SYSTEM_PROMPT = (
    "You are ranking an Aerodrome B20 pool board for an emissions-farming "
    "bot. Answer with exactly one JSON object and no other text: "
    '{"ranking": [string], "top_apr": number}. ranking orders every '
    "qualified pool (and only qualified pools) by expected farming value; "
    "you may disagree with a pure APR sort, but the qualified set must "
    "survive exactly. top_apr echoes the top pool's emissions_apr from the "
    "board's numbers."
)

WIDTH_SYSTEM_PROMPT = (
    "You are judging a concentrated-liquidity range-width solve for an "
    "emissions-farming bot. Answer with exactly one JSON object and no "
    "other text: "
    '{"mode": string, "half_width_ticks": integer}. mode is "solved" when '
    "the tightest tick-aligned candidate (one spacing per side) meets the "
    'target net daily yield, else "target_unreachable". half_width_ticks '
    "is the selected half width in whole ticks - read the pool's "
    "tick_spacing from the inputs; the tightest candidate is exactly one "
    "spacing. Judge the economics from the provided observables: emissions "
    "APR is per staked liquidity, and a tighter range earns a larger share "
    "of it."
)


def regime_user_prompt(scenario: Mapping[str, Any]) -> str:
    """Render one regime scenario as the task prompt."""
    return "Classify this window and compute its return and swing percents.\n\n" + json.dumps(
        {
            "name": scenario["name"],
            "window": list(scenario["window"]),
            "contract": {"label": "one allowed label", "numbers": "from the window"},
        },
        indent=2,
    )


def board_user_prompt(scenario: Mapping[str, Any]) -> str:
    """Render one board scenario as the task prompt."""
    return "Rank this board per the contract.\n\n" + json.dumps(
        {
            "name": scenario["name"],
            "pools": list(scenario["pools"]),
        },
        indent=2,
    )


def width_user_prompt(scenario: Mapping[str, Any]) -> str:
    """Render one width scenario as the task prompt."""
    return "Judge this width solve per the contract.\n\n" + json.dumps(
        {
            "name": scenario["name"],
            "observations": dict(scenario["observations"]),
        },
        indent=2,
    )


class ModelContestant:
    """Serve one model's answers from the inference plane."""

    def __init__(self, config: AdvisorConfig, timeout_seconds: float) -> None:
        """Bind one model to its plane transport.

        Args:
            config: The sealed plane configuration (URL and model).
            timeout_seconds: The per-request wall-clock bound.
        """
        self._config = config
        self._transport = HttpxAdvisorTransport()
        self._timeout_seconds = timeout_seconds

    def ask(self, system_prompt: str, user_prompt: str) -> Answer:
        """Deliver one task question and parse the JSON answer.

        Args:
            system_prompt: The task contract.
            user_prompt: The scenario prompt.

        Returns:
            The parsed payload, or a typed failure string.
        """
        endpoint = self._config.url.rstrip("/") + "/v1/chat/completions"
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": self._config.max_tokens,
        }
        try:
            response = self._transport.complete(endpoint, payload, self._timeout_seconds)
        except AdvisorTimeoutError:
            return Answer(failure="timeout")
        except AdvisorUnreachableError:
            return Answer(failure="unreachable")
        if response.status_code != 200:
            return Answer(failure=f"http_status_{response.status_code}")
        try:
            body = json.loads(response.body)
            content = body["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return Answer(failure="malformed_body")
        if not isinstance(content, str) or not content.strip():
            return Answer(failure="empty_content")
        try:
            parsed = json.loads(extract_json_object(content))
        except json.JSONDecodeError:
            return Answer(failure="malformed_json")
        if not isinstance(parsed, dict):
            return Answer(failure="not_an_object")
        return Answer(payload=parsed)


class BaselineContestant:
    """Serve the deterministic baseline's answers from the same truths."""

    def ask(self, system_prompt: str, user_prompt: str) -> Answer:
        """Answer from the deterministic rules embedded in the scenario prompts.

        Args:
            system_prompt: The task contract (names the current task).
            user_prompt: The scenario prompt carrying the fixture JSON.

        Returns:
            The baseline's deterministic payload.
        """
        scenario = json.loads(user_prompt.split("\n\n", 1)[1])
        if system_prompt is REGIME_SYSTEM_PROMPT:
            payload: dict[str, Any] = baseline_regime(scenario["window"])
        elif system_prompt is BOARD_SYSTEM_PROMPT:
            payload = baseline_board(scenario["pools"])
        else:
            payload = baseline_width(scenario["observations"])
        return Answer(payload=payload)


@dataclass(frozen=True)
class TaskBinding:
    """Bind one task's prompts and scorer to its fixture tuple."""

    title: str
    system_prompt: str
    prompt_builder: Callable[[Mapping[str, Any]], str]
    scorer: Callable[[Mapping[str, Any], Answer], ScenarioScore]
    scenarios: tuple[Mapping[str, Any], ...]


def build_contest(fixtures: Fixtures) -> tuple[TaskBinding, ...]:
    """Bind the three tasks to their fixture tuples.

    Args:
        fixtures: The committed scenario fixtures.

    Returns:
        One binding per task, in regime, board, width order.
    """
    return (
        TaskBinding(
            "regime", REGIME_SYSTEM_PROMPT, regime_user_prompt, score_regime, fixtures.regime
        ),
        TaskBinding("board", BOARD_SYSTEM_PROMPT, board_user_prompt, score_board, fixtures.board),
        TaskBinding("width", WIDTH_SYSTEM_PROMPT, width_user_prompt, score_width, fixtures.width),
    )


def run_contest(
    name: str,
    kind: str,
    ask: Callable[[str, str], Answer],
    tasks: Sequence[TaskBinding],
) -> ContestantResult:
    """Score one contestant over every task and scenario.

    Args:
        name: The contestant's display name.
        kind: baseline or model.
        ask: The contestant's ask(system, user) answer contract.
        tasks: The bound task list.

    Returns:
        The contestant's complete result.
    """
    started = time.monotonic()
    scores: list[ScenarioScore] = []
    for task in tasks:
        for scenario in task.scenarios:
            answer = ask(task.system_prompt, task.prompt_builder(scenario))
            scores.append(task.scorer(scenario, answer))
    return ContestantResult(
        name=name,
        kind=kind,
        scenarios=tuple(scores),
        elapsed_seconds=time.monotonic() - started,
    )


def render_markdown(results: Sequence[ContestantResult], created_at: datetime) -> str:
    """Render the bake-off report as markdown.

    Args:
        results: Every contestant's result, baseline first.
        created_at: The report's creation stamp.

    Returns:
        The complete report text.
    """
    lines = [
        "# Advisor bake-off report",
        "",
        f"Created {created_at.isoformat()}.",
        "Deterministic truths: the stated regime rule, APR-descending order, "
        "and the repo's own `solve_range_width`.",
        "",
        "| Contestant | Points | Ceiling | Fraction | Elapsed s |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.name} ({result.kind}) | {result.points:g} | {result.ceiling:g} "
            f"| {result.fraction:.3f} | {result.elapsed_seconds:.1f} |"
        )
    for result in results:
        lines += ["", f"## {result.name} ({result.kind})", ""]
        lines += ["| Scenario | Points | Ceiling | Notes |", "| --- | --- | --- | --- |"]
        for score in result.scenarios:
            notes = "; ".join(score.notes) if score.notes else ""
            lines.append(f"| {score.name} | {score.points:g} | {score.ceiling:g} | {notes} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bake-off.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero when every named contestant ran, one on
        fixture or configuration failures.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-bakeoff",
        description=(
            "Score candidate advisor models against the deterministic "
            "baseline over committed fixtures. Replay-only: no live reads, "
            "no signing, no cycle state."
        ),
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE_PATH,
        help="Committed fixture JSON path.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="One model contestant's name; repeat for several.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get(ADVISOR_URL_ENV, ""),
        help="The inference plane's base URL (default: the advisor environment).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_CONTEST_TIMEOUT_SECONDS,
        help="Per-request wall-clock bound (default 120; research calls may think).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Report path (default: run/bakeoff/report-<stamp>.md).",
    )
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.timeout_seconds <= 600:
        parser.error("--timeout-seconds must be between 1 and 600")
    fixtures = load_fixtures(arguments.fixture)
    tasks = build_contest(fixtures)
    results: list[ContestantResult] = [
        run_contest("deterministic-baseline", "baseline", BaselineContestant().ask, tasks)
    ]
    if arguments.model:
        if not arguments.url:
            print(
                "no plane URL: pass --url or seal the advisor environment",
                file=sys.stderr,
            )
            return 1
        for model in arguments.model:
            # The research timeout rides the contestant directly; the advisor
            # surface's own 120-second operational cap does not bind here.
            config = AdvisorConfig(url=arguments.url, model=model)
            contestant = ModelContestant(config, arguments.timeout_seconds)
            print(
                f"contestant {model}: running over {sum(len(t.scenarios) for t in tasks)} scenarios"
            )
            results.append(run_contest(model, "model", contestant.ask, tasks))
    created_at = datetime.now(UTC)
    report = render_markdown(results, created_at)
    out_path = (
        arguments.out
        if arguments.out is not None
        else DEFAULT_REPORT_DIRECTORY / f"report-{created_at.strftime('%Y%m%dT%H%M%SZ')}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
