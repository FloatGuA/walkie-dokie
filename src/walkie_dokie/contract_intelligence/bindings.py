"""Deterministic external-subject to authorized-project resolution."""

from __future__ import annotations

from django.db import transaction

from .models import ExternalProjectBinding


def list_user_projects(*, platform: str, user_id: str):
    return ExternalProjectBinding.objects.filter(
        platform=platform,
        subject_type=ExternalProjectBinding.SubjectType.USER,
        subject_id=user_id,
        is_active=True,
        project__is_active=True,
    ).select_related("project", "project__current_index_build")


def select_user_project(*, platform: str, user_id: str, project_slug: str):
    with transaction.atomic():
        bindings = list_user_projects(platform=platform, user_id=user_id).select_for_update()
        selected = bindings.filter(project__slug=project_slug).first()
        if selected is None:
            raise LookupError("用户未获授权访问该项目")
        bindings.update(is_selected=False)
        selected.is_selected = True
        selected.save(update_fields=("is_selected",))
        return selected.project


def resolve_external_project(
    *, platform: str, user_id: str, conversation_type: str, conversation_id: str | None
):
    if conversation_type == "group":
        if not conversation_id:
            raise LookupError("群聊事件缺少 chat_id")
        bindings = ExternalProjectBinding.objects.filter(
            platform=platform,
            subject_type=ExternalProjectBinding.SubjectType.CHAT,
            subject_id=conversation_id,
            is_active=True,
            is_selected=True,
            project__is_active=True,
        ).select_related("project", "project__current_index_build")
    else:
        bindings = list_user_projects(platform=platform, user_id=user_id).filter(
            is_selected=True
        )
    matches = list(bindings[:2])
    if len(matches) != 1:
        raise LookupError("当前会话没有唯一选中的授权项目")
    project = matches[0].project
    if project.current_index_build_id is None:
        raise LookupError("绑定项目尚未发布可查询版本")
    return project
