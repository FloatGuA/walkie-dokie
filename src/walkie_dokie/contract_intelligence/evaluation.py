"""Golden Dataset evaluator with explicit unavailable metrics."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation

from asgiref.sync import sync_to_async
from django.utils import timezone

from .agent import ask_intelligence_question
from .domain import normalize_text
from .models import EvaluationRun, GoldenCase, KnowledgeProject, RetrievalTrace

AnswerFunction = Callable[..., Awaitable[dict]]


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _citation_metrics(expected: set[str], actual: set[str]) -> tuple[float | None, float | None]:
    if not expected:
        return (1.0 if not actual else 0.0), None
    overlap = len(expected & actual)
    recall = _ratio(overlap, len(expected))
    precision = _ratio(overlap, len(actual)) if actual else 0.0
    return precision, recall


async def _retrieved_ids(result: dict, *, recall_k: int) -> set[str]:
    trace_ids = result.get("trace", {}).get("retrieval_trace_ids", [])
    if not trace_ids:
        return set()
    traces = await sync_to_async(list, thread_sensitive=True)(
        RetrievalTrace.objects.filter(pk__in=trace_ids).order_by("created_at")
    )
    return {
        candidate["evidence_id"]
        for trace in traces
        for candidate in trace.candidates
        if int(candidate.get("rank", recall_k + 1)) <= recall_k
    }


async def run_golden_evaluation(
    project_id,
    *,
    answer_function: AnswerFunction = ask_intelligence_question,
    recall_k: int = 12,
) -> EvaluationRun:
    project = await sync_to_async(
        KnowledgeProject.objects.select_related("current_index_build").get,
        thread_sensitive=True,
    )(pk=project_id)
    if project.current_index_build is None:
        raise LookupError("项目没有 Published IndexBuild")
    cases = await sync_to_async(list, thread_sensitive=True)(
        GoldenCase.objects.filter(project=project, is_verified=True).filter(
            index_build__isnull=True
        )
        | GoldenCase.objects.filter(
            project=project,
            is_verified=True,
            index_build=project.current_index_build,
        )
    )
    if not cases:
        raise ValueError("项目没有经过人工确认的 GoldenCase")
    run = await sync_to_async(EvaluationRun.objects.create, thread_sensitive=True)(
        project=project,
        index_build=project.current_index_build,
        status=EvaluationRun.Status.RUNNING,
        provider_configuration={"retrieval_recall_k": recall_k},
    )
    case_results: list[dict] = []
    try:
        for case in cases:
            result = await answer_function(
                authorized_project_id=str(project.id),
                question=case.question,
                platform="evaluation",
                actor_key=str(run.id),
            )
            expected_ids = set(case.expected_evidence_ids)
            actual_ids = {
                item["evidence_id"] for item in result.get("evidence", [])
            }
            retrieved_ids = await _retrieved_ids(result, recall_k=recall_k)
            citation_precision, citation_recall = _citation_metrics(
                expected_ids, actual_ids
            )
            retrieval_recall = (
                _ratio(len(expected_ids & retrieved_ids), len(expected_ids))
                if expected_ids
                else None
            )
            answer_text = normalize_text(result.get("answer", ""))
            fact_checks = [
                normalize_text(expected) in answer_text
                for expected in case.expected_answer_contains
            ]
            answer_facts_correct = all(fact_checks)
            numeric_correct = None
            if case.expected_calculation_result:
                actual = (result.get("calculation") or {}).get("result")
                try:
                    numeric_correct = Decimal(str(actual)) == Decimal(
                        case.expected_calculation_result
                    )
                except (InvalidOperation, TypeError):
                    numeric_correct = False
            status_correct = result.get("status") == case.expected_status
            case_results.append(
                {
                    "golden_case_id": str(case.id),
                    "question_run_id": result.get("question_run_id"),
                    "status_correct": status_correct,
                    "answer_facts_correct": answer_facts_correct,
                    "citation_precision": citation_precision,
                    "citation_recall": citation_recall,
                    f"retrieval_recall@{recall_k}": retrieval_recall,
                    "numeric_correct": numeric_correct,
                    "unexpected_answer": (
                        case.expected_status != "answered"
                        and result.get("status") == "answered"
                    ),
                }
            )

        answer_cases = [
            item
            for item, case in zip(case_results, cases, strict=True)
            if case.expected_status == "answered"
        ]
        numeric_cases = [
            item for item in case_results if item["numeric_correct"] is not None
        ]
        retrieval_cases = [
            item
            for item in case_results
            if item[f"retrieval_recall@{recall_k}"] is not None
        ]
        metrics = {
            "case_count": len(case_results),
            "outcome_accuracy": _ratio(
                sum(item["status_correct"] for item in case_results), len(case_results)
            ),
            "answer_accuracy": _ratio(
                sum(
                    item["status_correct"] and item["answer_facts_correct"]
                    for item in case_results
                ),
                len(case_results),
            ),
            "citation_accuracy": _ratio(
                sum(
                    item["citation_precision"] == 1.0
                    and item["citation_recall"] in {1.0, None}
                    for item in answer_cases
                ),
                len(answer_cases),
            ),
            f"retrieval_recall@{recall_k}": (
                round(
                    sum(item[f"retrieval_recall@{recall_k}"] for item in retrieval_cases)
                    / len(retrieval_cases),
                    6,
                )
                if retrieval_cases
                else None
            ),
            "numeric_accuracy": _ratio(
                sum(item["numeric_correct"] for item in numeric_cases),
                len(numeric_cases),
            ),
            "hallucination_rate": _ratio(
                sum(item["unexpected_answer"] for item in case_results),
                len(case_results),
            ),
            "reranker_accuracy": None,
            "reranker_accuracy_reason": "MVP 尚未接入 Reranker，不能伪造该指标",
        }
        await sync_to_async(EvaluationRun.objects.filter(pk=run.pk).update, thread_sensitive=True)(
            status=EvaluationRun.Status.SUCCEEDED,
            metrics=metrics,
            case_results=case_results,
            finished_at=timezone.now(),
        )
    except Exception as exc:
        await sync_to_async(EvaluationRun.objects.filter(pk=run.pk).update, thread_sensitive=True)(
            status=EvaluationRun.Status.FAILED,
            error=f"{type(exc).__name__}: {exc}"[:8_000],
            case_results=case_results,
            finished_at=timezone.now(),
        )
        raise
    await sync_to_async(run.refresh_from_db, thread_sensitive=True)()
    return run
