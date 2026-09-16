"""Is the claim checker any good?

The verify node is an AI judge, and judges get things wrong (Huyen Ch. 4), so it
needs its own grading. Each case below is a claim paired with a real passage from
Microsoft's 10-K, labelled by hand. The hard ones are not the opposites - they are
claims that are plausible, probably even true, but simply absent from the passage.

One call, about a cent.

    python evals/claim_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from fin_analyst.config import load_settings
from fin_analyst.llm import ClaudeAnalyst
from fin_analyst.passages import load_passages

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "msft_passages.json"

# (claim, a phrase identifying the passage it cites, supported?)
#
# The passage is pinned by its own words, not chosen by search: this grades the
# judge, and a retrieval miss would otherwise look like a judging error. That
# happened on the first run, and it was the search that was wrong, not the judge.
CASES = [
    # Straight paraphrase of what the passage says.
    ("Operating expenses rose on investment in R&D compute capacity and AI talent [P].",
     "Operating expenses increased $4.9 billion", True),
    # The passage's own reason, put the other way round.
    ("The filing attributes higher operating expenses partly to AI talent costs [P].",
     "Operating expenses increased $4.9 billion", True),
    # Plausible, probably true, and not in the passage. This is the hard case.
    ("Operating expenses rose because Microsoft raised Azure prices [P].",
     "Operating expenses increased $4.9 billion", False),
    # States the opposite direction.
    ("Operating expenses fell as the company cut research spending [P].",
     "Operating expenses increased $4.9 billion", False),
    # Adds a ranking the passage never makes.
    ("AI talent was the single largest driver of the increase [P].",
     "Operating expenses increased $4.9 billion", False),
    # Gross margin passage, faithfully.
    ("Gross margin percentage decreased on AI infrastructure investment and a mix shift to Azure [P].",
     "sales mix shift to Azure", True),
    # Attributes it to something the passage does not mention.
    ("Gross margin percentage decreased because of currency movements [P].",
     "sales mix shift to Azure", False),
    # A claim carrying a placeholder: the words are what is judged.
    ("Gross margin {{gross_margin:2025->2026}}, which the filing attributes to AI infrastructure investment [P].",
     "sales mix shift to Azure", True),
]


def main() -> int:
    load_dotenv()
    settings = load_settings()
    passages = load_passages(FIXTURE)
    analyst = ClaudeAnalyst(settings)

    claims = []
    for claim, phrase, _ in CASES:
        passage = next(p for p in passages if phrase in p.text)
        claims.append((claim.replace("[P]", f"[{passage.id}]"), [passage]))

    verdicts, cost = analyst.verify_claims(claims)

    agreed = 0
    for (claim, _), verdict, (_, _, expected) in zip(claims, verdicts, CASES):
        ok = verdict.supported == expected
        agreed += ok
        label = "agrees" if ok else "DISAGREES"
        print(f"{label:10s} judged {'supported' if verdict.supported else 'unsupported':12s} "
              f"(hand label: {'supported' if expected else 'unsupported'})")
        print(f"           {claim[:110]}")
        print(f"           judge: {verdict.reason}")

    print(f"\n{agreed}/{len(CASES)} agree with the hand labels. Spent ${cost:.4f}.")
    return 0 if agreed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
