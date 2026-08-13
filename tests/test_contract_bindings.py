import pytest
from django.core.exceptions import ValidationError

from walkie_dokie.contract_intelligence.bindings import (
    resolve_external_project,
    select_user_project,
)
from walkie_dokie.contract_intelligence.models import (
    ExternalProjectBinding,
    IndexBuild,
    KnowledgeProject,
)
from walkie_dokie.platforms.base import InboundEvent


def _published_project(name, slug):
    project = KnowledgeProject.objects.create(name=name, slug=slug)
    build = IndexBuild.objects.create(
        project=project, name="published", status=IndexBuild.Status.PUBLISHED
    )
    project.current_index_build = build
    project.save(update_fields=("current_index_build",))
    return project


@pytest.mark.django_db
def test_private_user_can_select_only_an_authorized_project():
    first = _published_project("项目一", "one")
    second = _published_project("项目二", "two")
    for project in (first, second):
        ExternalProjectBinding.objects.create(
            platform="feishu",
            subject_type=ExternalProjectBinding.SubjectType.USER,
            subject_id="ou_user",
            project=project,
            is_selected=project == first,
        )

    selected = select_user_project(
        platform="feishu", user_id="ou_user", project_slug="two"
    )

    assert selected == second
    assert (
        resolve_external_project(
            platform="feishu",
            user_id="ou_user",
            conversation_type="private",
            conversation_id="chat-private",
        )
        == second
    )
    with pytest.raises(LookupError, match="未获授权"):
        select_user_project(
            platform="feishu", user_id="ou_user", project_slug="unknown"
        )


@pytest.mark.django_db
def test_group_chat_resolves_its_fixed_project():
    project = _published_project("群项目", "group")
    binding = ExternalProjectBinding(
        platform="feishu",
        subject_type=ExternalProjectBinding.SubjectType.CHAT,
        subject_id="oc_chat",
        project=project,
        is_selected=False,
    )
    with pytest.raises(ValidationError, match="群聊固定项目"):
        binding.full_clean()
    binding.is_selected = True
    binding.full_clean()
    binding.save()

    assert (
        resolve_external_project(
            platform="feishu",
            user_id="ou_member",
            conversation_type="group",
            conversation_id="oc_chat",
        )
        == project
    )


def test_group_event_uses_chat_reply_target_without_changing_actor_id():
    event = InboundEvent(
        "feishu",
        "ou_actor",
        "问题",
        None,
        conversation_id="oc_chat",
        conversation_type="group",
    )
    assert event.user_id == "ou_actor"
    assert event.reply_target == "chat:oc_chat"
