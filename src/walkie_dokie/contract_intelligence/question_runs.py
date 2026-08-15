"""QuestionRun lifecycle helpers shared by contract and price entry points."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.utils import timezone

from .models import IndexBuild, KnowledgeProject, QuestionRun


@dataclass(frozen=True, slots=True)
class QuestionRunContext:
    project_id: str
    index_build_id: str
    question_run_id: str


async def start_question_run(
    *,
    authorized_project_id: str,
    question: str,
    actor_user=None,
    platform: str,
    actor_key: str | None,
) -> QuestionRunContext:
    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("question 不能为空")
    project = await sync_to_async(
        KnowledgeProject.objects.select_related("current_index_build").get,
        thread_sensitive=True,
    )(pk=authorized_project_id, is_active=True)
    build = project.current_index_build
    if build is None or build.status != IndexBuild.Status.PUBLISHED:
        raise LookupError("项目没有 Published IndexBuild")
    run = await sync_to_async(QuestionRun.objects.create, thread_sensitive=True)(
        project=project,
        index_build=build,
        actor_user=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        platform=platform,
        actor_key_hash=(
            hashlib.sha256(actor_key.encode("utf-8")).hexdigest() if actor_key else ""
        ),
        question=normalized_question,
    )
    return QuestionRunContext(str(project.id), str(build.id), str(run.id))


async def complete_question_run(
    context: QuestionRunContext,
    *,
    result: dict,
    workflow_trace: dict,
) -> None:
    status = {
        "answered": QuestionRun.Status.ANSWERED,
        "refused": QuestionRun.Status.REFUSED,
        "clarification": QuestionRun.Status.CLARIFICATION,
    }[result["status"]]
    updated = await sync_to_async(
        QuestionRun.objects.filter(
            pk=context.question_run_id,
            status=QuestionRun.Status.RUNNING,
        ).update,
        thread_sensitive=True,
    )(
        status=status,
        answer_payload=result,
        workflow_trace=workflow_trace,
        finished_at=timezone.now(),
    )
    if updated != 1:
        raise RuntimeError("QuestionRun 不处于可完成的 RUNNING 状态")


async def fail_question_run(context: QuestionRunContext, exc: Exception) -> None:
    updated = await sync_to_async(
        QuestionRun.objects.filter(
            pk=context.question_run_id,
            status=QuestionRun.Status.RUNNING,
        ).update,
        thread_sensitive=True,
    )(
        status=QuestionRun.Status.FAILED,
        error=f"{type(exc).__name__}: {exc}"[:8_000],
        finished_at=timezone.now(),
    )
    if updated != 1:
        raise RuntimeError("QuestionRun 不处于可失败的 RUNNING 状态") from exc
