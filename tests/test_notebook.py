"""The walkthrough notebook stays runnable, and stays free to run.

It ships with its outputs so GitHub renders it, so a stale or broken run would
otherwise sit there unnoticed. Parsed as JSON: no notebook library needed, and
the test never executes it.
"""

import json
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).resolve().parent.parent / "notebooks" / "walkthrough.ipynb"


@pytest.fixture(scope="module")
def cells():
    return json.loads(NOTEBOOK.read_text())["cells"]


def sources(cells):
    return ["".join(cell["source"]) for cell in cells if cell["cell_type"] == "code"]


def test_no_cell_ended_in_an_error(cells):
    for cell in cells:
        for output in cell.get("outputs", []):
            assert output["output_type"] != "error", (
                f"{output.get('ename')}: {output.get('evalue')} - re-run the notebook"
            )


def test_it_was_run_with_the_live_call_switched_off(cells):
    live = [s for s in sources(cells) if "LIVE" in s]
    assert live and "LIVE = False" in live[0], "the shipped notebook must not spend anything"
    printed = "".join(
        "".join(o.get("text", "")) for cell in cells for o in cell.get("outputs", [])
    )
    assert "nothing was spent" in printed


def test_the_run_it_shows_produced_a_memo(cells):
    rendered = [
        "".join(o["data"]["text/markdown"])
        for cell in cells for o in cell.get("outputs", [])
        if "text/markdown" in o.get("data", {})
    ]
    assert any("fell 0.12x to 1.23x" in memo for memo in rendered), "the memo output is missing"
