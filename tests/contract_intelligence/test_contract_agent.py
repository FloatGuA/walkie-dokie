import asyncio
from decimal import Decimal

import pytest
from asgiref.sync import async_to_sync

import walkie_dokie.contract_intelligence.agent as agent_module
from walkie_dokie.contract_intelligence.agent import (
    QuestionPlan,
    ask_intelligence_question,
    build_intelligence_agent_graph,
)
from walkie_dokie.contract_intelligence.models import (
    IndexBuild,
    KnowledgeProject,
    QuestionRun,
)
from walkie_dokie.contract_intelligence.pricing import PriceQuery


class FakePlanner:
    name = "fake_planner"
    version = "1"

    def __init__(self, plan):
        self.value = plan

    async def plan(self, *, question):
        return self.value


async def test_agent_routes_price_question_to_structured_price_tool_only():
    called = []

    async def contract_tool(**kwargs):
        raise AssertionError("contract tool should not run")

    async def price_tool(**kwargs):
        called.append(kwargs)
        return {"status": "answered", "answer": "100元", "trace": {}}

    graph = build_intelligence_agent_graph(
        planner=FakePlanner(
            QuestionPlan(
                "query_price",
                "询问精确报价",
                PriceQuery(product_code="P001", quantity=Decimal("2")),
            )
        ),
        contract_tool=contract_tool,
        price_tool=price_tool,
    )
    state = await graph.ainvoke(
        {"authorized_project_id": "p1", "question": "P001买2个多少钱"}
    )

    assert state["result"]["answer"] == "100元"
    assert called[0]["price_query"].product_code == "P001"


async def test_agent_routes_non_price_question_to_contract_tool_only():
    async def contract_tool(**kwargs):
        return {"status": "refused", "answer": "证据不足", "trace": {}}

    async def price_tool(**kwargs):
        raise AssertionError("price tool should not run")

    graph = build_intelligence_agent_graph(
        planner=FakePlanner(QuestionPlan("search_contract", "条款事实")),
        contract_tool=contract_tool,
        price_tool=price_tool,
    )
    state = await graph.ainvoke(
        {"authorized_project_id": "p1", "question": "合同期限多长"}
    )
    assert state["result"]["status"] == "refused"


async def test_agent_graph_surfaces_planner_failure():
    class FailingPlanner:
        name = "failing_planner"
        version = "1"

        async def plan(self, *, question):
            raise RuntimeError("planner unavailable")

    graph = build_intelligence_agent_graph(planner=FailingPlanner())
    with pytest.raises(RuntimeError, match="planner unavailable"):
        await graph.ainvoke(
            {"authorized_project_id": "p1", "question": "合同期限是什么？"}
        )


def _published_project():
    project = KnowledgeProject.objects.create(name="审计项目", slug="agent-audit")
    build = IndexBuild.objects.create(
        project=project,
        name="published",
        status=IndexBuild.Status.PUBLISHED,
    )
    KnowledgeProject.objects.filter(pk=project.pk).update(current_index_build=build)
    return project, build


async def _await_with_sqlite_heartbeat(awaitable):
    task = asyncio.create_task(awaitable)
    while not task.done():
        await asyncio.sleep(0.01)
    return await task


@pytest.mark.django_db(transaction=True)
def test_planner_failure_marks_the_single_question_run_failed():
    project, build = _published_project()

    class FailingPlanner:
        name = "failing_planner"
        version = "1"

        async def plan(self, *, question):
            raise RuntimeError("planner unavailable")

    with pytest.raises(RuntimeError, match="planner unavailable"):
        async_to_sync(_await_with_sqlite_heartbeat)(
            ask_intelligence_question(
                authorized_project_id=str(project.id),
                question="合同期限是什么？",
                planner=FailingPlanner(),
            )
        )

    run = QuestionRun.objects.get()
    assert run.index_build_id == build.id
    assert run.status == QuestionRun.Status.FAILED
    assert "planner unavailable" in run.error


@pytest.mark.django_db(transaction=True)
def test_top_level_persists_planner_trace_on_the_pinned_build(monkeypatch):
    project, build = _published_project()

    class Graph:
        async def ainvoke(self, state):
            assert state["index_build_id"] == str(build.id)
            return {
                "plan": QuestionPlan("search_contract", "合同事实"),
                "result": {
                    "status": "refused",
                    "answer": "证据不足",
                    "evidence": [],
                    "document": [],
                    "page_clause": [],
                    "calculation": None,
                    "confidence": {"level": "not_applicable", "basis": []},
                    "trace": {"retrieval_trace_ids": ["trace-1"]},
                },
            }

    monkeypatch.setattr(
        agent_module,
        "build_intelligence_agent_graph",
        lambda **kwargs: Graph(),
    )
    result = async_to_sync(_await_with_sqlite_heartbeat)(
        ask_intelligence_question(
            authorized_project_id=str(project.id),
            question="合同期限是什么？",
            planner=FakePlanner(QuestionPlan("search_contract", "合同事实")),
        )
    )

    run = QuestionRun.objects.get()
    assert result["question_run_id"] == str(run.id)
    assert run.index_build_id == build.id
    assert run.status == QuestionRun.Status.REFUSED
    assert run.workflow_trace["planner"] == "fake_planner@1"
    assert run.workflow_trace["selected_tool"] == "search_contract"
    assert run.workflow_trace["retrieval_trace_ids"] == ["trace-1"]
