"""Pin the bake-off harness's deterministic scoring contract."""

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from aero_bot.advisor import ADVISOR_URL_ENV, AdvisorHttpResponse

BAKEOFF_PATH = Path(__file__).resolve().parents[1] / "tools" / "bakeoff.py"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "bakeoff" / "fixtures.json"


def _load_harness() -> ModuleType:
    """Import the harness module from its non-package tools path."""
    spec = importlib.util.spec_from_file_location("aero_bot_bakeoff", BAKEOFF_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bakeoff = _load_harness()


class ScriptedContestant:
    """Serve one scripted answer per scenario, recording every prompt."""

    def __init__(self, answers: Sequence[object]) -> None:
        """Hold the ordered scripted answers."""
        self.answers = list(answers)
        self.prompts: list[str] = []

    def ask(self, system_prompt: str, user_prompt: str) -> object:
        """Record the prompt and serve the next scripted answer."""
        self.prompts.append(user_prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, bakeoff.Answer):
            return answer
        return bakeoff.Answer(payload=answer)


def perfect_answers() -> list[dict[str, object]]:
    """Build the deterministic truths for every scenario in run order."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    answers: list[dict[str, object]] = []
    for scenario in fixtures.regime:
        answers.append(bakeoff.baseline_regime(scenario["window"]))
    for scenario in fixtures.board:
        answers.append(bakeoff.baseline_board(scenario["pools"]))
    for scenario in fixtures.width:
        answers.append(bakeoff.baseline_width(scenario["observations"]))
    return answers


def test_fixtures_load_with_every_task_populated() -> None:
    """The committed fixture file carries all three task lists."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    assert len(fixtures.regime) >= 3
    assert len(fixtures.board) >= 2
    assert len(fixtures.width) >= 2


def test_width_fixtures_solve_to_distinct_truths() -> None:
    """The three width scenarios discriminate: modes and tick grids differ."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    truths = [
        (truth["mode"], truth["half_width_ticks"])
        for truth in (
            bakeoff.baseline_width(scenario["observations"]) for scenario in fixtures.width
        )
    ]
    assert truths == [
        ("solved", 10),
        ("target_unreachable", 10),
        ("target_unreachable", 200),
    ]


def test_baseline_contestant_scores_perfectly() -> None:
    """The deterministic baseline earns every point over its own truths."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    tasks = bakeoff.build_contest(fixtures)
    result = bakeoff.run_contest(
        "deterministic-baseline", "baseline", bakeoff.BaselineContestant().ask, tasks
    )
    assert result.points == result.ceiling == 18.0
    assert result.fraction == 1.0
    assert all(not score.notes for score in result.scenarios)


def test_a_perfect_model_contestant_matches_the_baseline() -> None:
    """A model echoing the truths exactly scores identically to the baseline."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    tasks = bakeoff.build_contest(fixtures)
    contestant = ScriptedContestant(perfect_answers())
    result = bakeoff.run_contest("perfect-model", "model", contestant.ask, tasks)
    assert result.points == 18.0
    # Every scenario prompt carried the fixture JSON the answers grounded in.
    assert all("{" in prompt for prompt in contestant.prompts)


def test_regime_scoring_separates_label_and_grounding() -> None:
    """The label point and the grounded-evidence point score independently."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    scenario = fixtures.regime[0]
    truth = bakeoff.baseline_regime(scenario["window"])
    both = bakeoff.score_regime(scenario, bakeoff.Answer(payload=dict(truth)))
    assert both.points == 2.0
    wrong_label = bakeoff.score_regime(
        scenario,
        bakeoff.Answer(
            payload={
                "label": "trending_up",
                "return_percent": truth["return_percent"],
                "swing_percent": truth["swing_percent"],
            }
        ),
    )
    assert wrong_label.points == 1.0
    fabricated = bakeoff.score_regime(
        scenario,
        bakeoff.Answer(
            payload={
                "label": truth["label"],
                "return_percent": truth["return_percent"] + 5.0,
                "swing_percent": truth["swing_percent"],
            }
        ),
    )
    assert fabricated.points == 1.0
    failed = bakeoff.score_regime(scenario, bakeoff.Answer(failure="timeout"))
    assert failed.points == 0.0
    assert failed.notes == ("timeout",)


def test_board_scoring_requires_the_qualified_permutation() -> None:
    """A ranking that drops or admits pools scores zero regardless of order."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    scenario = fixtures.board[0]
    truth = bakeoff.baseline_board(scenario["pools"])
    perfect = bakeoff.Answer(payload={"ranking": truth["ranking"], "top_apr": truth["top_apr"]})
    assert bakeoff.score_board(scenario, perfect).points == 3.0
    dropped = bakeoff.Answer(
        payload={"ranking": truth["ranking"][:-1], "top_apr": truth["top_apr"]}
    )
    assert bakeoff.score_board(scenario, dropped).points == 0.0
    unqualified_admitted = bakeoff.Answer(
        payload={
            "ranking": ["LMTc", *truth["ranking"][:-1]],
            "top_apr": truth["top_apr"],
        }
    )
    assert bakeoff.score_board(scenario, unqualified_admitted).points == 0.0
    # A reversed order still preserves the set: top-pick, echo, and order
    # points all fall while the permutation stays valid.
    reversed_ranking = bakeoff.Answer(
        payload={"ranking": list(reversed(truth["ranking"])), "top_apr": 999.0}
    )
    reversed_score = bakeoff.score_board(scenario, reversed_ranking)
    assert reversed_score.points == 0.0


def test_width_scoring_scores_mode_and_ticks_separately() -> None:
    """Each of the solver's two facts carries its own point."""
    fixtures = bakeoff.load_fixtures(FIXTURE_PATH)
    scenario = fixtures.width[0]
    truth = bakeoff.baseline_width(scenario["observations"])
    perfect = bakeoff.Answer(payload=dict(truth))
    assert bakeoff.score_width(scenario, perfect).points == 2.0
    wrong_mode = bakeoff.Answer(
        payload={"mode": "target_unreachable", "half_width_ticks": truth["half_width_ticks"]}
    )
    assert bakeoff.score_width(scenario, wrong_mode).points == 1.0
    wrong_ticks = bakeoff.Answer(payload={"mode": truth["mode"], "half_width_ticks": 999})
    assert bakeoff.score_width(scenario, wrong_ticks).points == 1.0


def test_main_writes_the_baseline_report_and_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no models named, one baseline-only report lands at --out."""
    out = tmp_path / "report.md"
    monkeypatch.delenv(ADVISOR_URL_ENV, raising=False)
    assert bakeoff.main(["--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "deterministic-baseline" in text
    assert "| 18 | 18 | 1.000 |" in text
    assert "report written" in capsys.readouterr().out


def test_main_refuses_models_without_a_plane_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming a model with no URL fails closed instead of hanging."""
    monkeypatch.delenv(ADVISOR_URL_ENV, raising=False)
    assert bakeoff.main(["--model", "qwen3.6:35b-a3b", "--out", str(tmp_path / "r.md")]) == 1
    assert "no plane URL" in capsys.readouterr().err


def test_model_contestant_parses_plane_answers() -> None:
    """The model contestant maps transport answers through the same parser."""

    class FakeTransport:
        """Serve one scripted completion body."""

        def __init__(self, body: str, status: int = 200) -> None:
            self.body = body
            self.status = status

        def complete(
            self, url: str, payload: dict[str, object], timeout_seconds: float
        ) -> AdvisorHttpResponse:
            """Return the scripted body."""
            return AdvisorHttpResponse(status_code=self.status, body=self.body)

    content = json.dumps({"label": "steady", "return_percent": 0.4, "swing_percent": 0.5})
    body = json.dumps({"choices": [{"message": {"content": "```json\n" + content + "\n```"}}]})
    contestant = bakeoff.ModelContestant(
        bakeoff.AdvisorConfig(url="http://plane", model="m"), 120.0
    )
    contestant._transport = FakeTransport(body)  # noqa: SLF001 - harness test seam
    answer = contestant.ask("system", "user")
    assert answer.payload == {"label": "steady", "return_percent": 0.4, "swing_percent": 0.5}

    failing = bakeoff.ModelContestant(bakeoff.AdvisorConfig(url="http://plane", model="m"), 120.0)
    failing._transport = FakeTransport("broken", status=500)  # noqa: SLF001 - harness test seam
    assert failing.ask("system", "user").failure == "http_status_500"
