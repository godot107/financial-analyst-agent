"""Ask follow-up questions about one company without refetching the filing.

Each turn is a full pass of the workflow, so every answer is still built from
placeholders and still checked. What carries across turns is the fetched filing
(so the SEC is hit once), the earlier questions and answers, and the running
cost - the budget guard covers the conversation, not each turn on its own.

Turns are capped because every follow-up resends the answers before it, so each
turn costs a little more than the last.
"""

from pathlib import Path
from typing import Callable

from fin_analyst.config import Settings
from fin_analyst.edgar import Fact, fetch_facts
from fin_analyst.graph import RUNS, AnalysisState, Analyst, run_analysis
from fin_analyst.passages import Passage, fetch_passages
from fin_analyst.trace import Tracer


class ChatSession:
    """One company, a few questions, one shared budget."""

    def __init__(
        self,
        ticker: str,
        analyst: Analyst,
        settings: Settings,
        fetch: Callable[[str], list[Fact]] = fetch_facts,
        runs_dir: Path = RUNS,
        peer_ticker: str | None = None,
        fetch_text: Callable[[str], list[Passage]] | None = fetch_passages,
        tracer: Tracer | None = None,
        describe=None,
    ):
        self.tracer = tracer
        self.ticker = ticker.upper()
        self.peer_ticker = peer_ticker.upper() if peer_ticker else None
        self.analyst = analyst
        self.settings = settings
        self.runs_dir = runs_dir
        # Fetched once, reused by every turn: the filing does not change while
        # you are asking about it.
        self.facts = fetch(self.ticker)
        self.peer_facts = fetch(self.peer_ticker) if self.peer_ticker else []
        self.describe = describe  # a cheap index lookup, so each turn repeats it
        # The narrative is fetched once as well; each turn searches it again for
        # the passages that bear on that question.
        self.passages = fetch_text(self.ticker) if fetch_text else []
        self.history: list[tuple[str, str]] = []
        self.spent_usd = 0.0

    @property
    def turns_left(self) -> int:
        return self.settings.chat_max_turns - len(self.history)

    def ask(self, question: str) -> AnalysisState:
        if self.turns_left <= 0:
            raise RuntimeError(
                f"this session has used its {self.settings.chat_max_turns} turns; "
                "start a new one to keep going"
            )

        state = run_analysis(
            self.ticker,
            question,
            self.analyst,
            self.settings,
            fetch=lambda ticker: self.peer_facts if ticker == self.peer_ticker else self.facts,
            runs_dir=self.runs_dir,
            fetch_text=(lambda ticker: self.passages) if self.passages else None,
            peer_ticker=self.peer_ticker,
            history=self.history,
            cost_so_far=self.spent_usd,
            tracer=self.tracer,
            describe=self.describe,
        )

        self.spent_usd = state.cost_usd
        if state.memo:
            # Only answers that passed the check become context for later turns.
            self.history.append((question, state.memo))
        return state
