from walkie_dokie.contract_intelligence.evaluation import (
    _citation_metrics,
    _ratio,
)


def test_evaluation_metric_helpers_treat_refusal_and_empty_citations_explicitly():
    precision, recall = _citation_metrics(set(), set())
    assert precision == 1.0
    assert recall is None
    assert _ratio(1, 1) == 1.0
    assert _ratio(0, 0) is None
