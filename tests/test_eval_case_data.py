from pathlib import Path

from walkie_dokie.evals.cases import load_cases

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES = REPO_ROOT / "evals" / "cases"
FIXTURES = REPO_ROOT / "evals" / "fixtures"


def test_committed_cases_load_and_cover_all_categories():
    cases = load_cases(CASES, FIXTURES)
    categories = {c.category for c in cases}
    assert categories == {
        "intent_routing",
        "memory_boundary",
        "confirm_semantics",
        "prompt_injection",
    }
    assert len(cases) == 23
    per_category = {cat: sum(1 for c in cases if c.category == cat) for cat in categories}
    assert per_category == {
        "intent_routing": 5,
        "memory_boundary": 5,
        "confirm_semantics": 6,
        "prompt_injection": 7,
    }
