from decimal import Decimal

from walkie_dokie.contract_intelligence.agent import (
    QuestionPlan,
    build_intelligence_agent_graph,
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
