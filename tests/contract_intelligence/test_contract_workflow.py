from walkie_dokie.contract_intelligence.answering import (
    AnswerDraft,
    AtomicClaim,
    ClaimVerdict,
    EvidenceItem,
    VerificationResult,
)
from walkie_dokie.contract_intelligence.workflow import (
    SearchBundle,
    build_contract_qa_graph,
)


def _evidence(evidence_id="ev_price"):
    return EvidenceItem(
        evidence_id=evidence_id,
        document="服务合同",
        document_version_id="v1",
        source_file="contract.pdf",
        text="第一条 服务费为人民币100元。",
        clause_ref="第一条",
        page_physical=3,
        source_anchor={"type": "pdf_page_text", "page": 3, "segment": 0},
    )


class FakeSearch:
    def __init__(self, bundles):
        self.bundles = list(bundles)
        self.queries = []

    async def search(self, *, project_id, query, top_k):
        self.queries.append((project_id, query, top_k))
        return self.bundles.pop(0)


class FakeAnswer:
    name = "fake_answer"
    version = "1"

    def __init__(self, drafts, rewrites=()):
        self.drafts = list(drafts)
        self.rewrites = list(rewrites)

    async def draft_answer(self, *, question, evidence):
        return self.drafts.pop(0)

    async def rewrite_query(
        self, *, question, previous_queries, failure_reason, evidence
    ):
        return self.rewrites.pop(0)


class FakeVerifier:
    name = "fake_verifier"
    version = "1"

    def __init__(self, results):
        self.results = list(results)

    async def verify(self, *, question, draft, evidence):
        return self.results.pop(0)


async def test_contract_workflow_answers_only_after_atomic_claim_verification():
    evidence = _evidence()
    draft = AnswerDraft(
        "answered",
        "服务费为人民币100元。",
        (AtomicClaim("服务费为人民币100元。", (evidence.evidence_id,)),),
    )
    graph = build_contract_qa_graph(
        search_tool=FakeSearch((SearchBundle((evidence,), "trace-1"),)),
        answer_provider=FakeAnswer((draft,)),
        verifier=FakeVerifier(
            (
                VerificationResult(
                    True,
                    (ClaimVerdict(draft.claims[0].claim, True, "原文直接支持"),),
                    "全部支持",
                ),
            )
        ),
    )

    state = await graph.ainvoke(
        {
            "authorized_project_id": "p1",
            "question": "服务费多少钱？",
            "current_query": "服务费多少钱？",
            "previous_queries": [],
            "attempt": 0,
            "max_attempts": 2,
            "top_k": 5,
            "current_evidence": [],
            "accumulated_evidence": [],
            "retrieval_trace_ids": [],
        }
    )

    assert state["result"]["status"] == "answered"
    assert state["result"]["evidence"][0]["evidence_id"] == "ev_price"
    assert state["result"]["page_clause"][0]["page_physical"] == 3
    assert state["result"]["confidence"]["level"] == "high"


async def test_contract_workflow_rewrites_once_then_refuses_when_verifier_fails():
    evidence = _evidence()
    draft = AnswerDraft(
        "answered",
        "服务费含税100元。",
        (AtomicClaim("服务费含税100元。", (evidence.evidence_id,)),),
    )
    verifier_failure = VerificationResult(
        False,
        (ClaimVerdict(draft.claims[0].claim, False, "证据未说明含税"),),
        "税口径缺失",
    )
    search = FakeSearch(
        (
            SearchBundle((evidence,), "trace-1"),
            SearchBundle((evidence,), "trace-2"),
        )
    )
    graph = build_contract_qa_graph(
        search_tool=search,
        answer_provider=FakeAnswer((draft, draft), ("服务费 税口径",)),
        verifier=FakeVerifier((verifier_failure, verifier_failure)),
    )

    state = await graph.ainvoke(
        {
            "authorized_project_id": "p1",
            "question": "服务费含税多少钱？",
            "current_query": "服务费含税多少钱？",
            "previous_queries": [],
            "attempt": 0,
            "max_attempts": 2,
            "top_k": 5,
            "current_evidence": [],
            "accumulated_evidence": [],
            "retrieval_trace_ids": [],
        }
    )

    assert len(search.queries) == 2
    assert search.queries[1][1] == "服务费 税口径"
    assert state["result"]["status"] == "refused"
    assert state["result"]["evidence"] == []
    assert "税口径缺失" in state["result"]["answer"]


async def test_contract_workflow_returns_clarification_without_verifier():
    graph = build_contract_qa_graph(
        search_tool=FakeSearch((SearchBundle((_evidence(),), "trace-1"),)),
        answer_provider=FakeAnswer(
            (
                AnswerDraft(
                    "clarification",
                    "",
                    clarification_question="请问要查询哪个地区的价格？",
                ),
            )
        ),
        verifier=FakeVerifier(()),
    )

    state = await graph.ainvoke(
        {
            "authorized_project_id": "p1",
            "question": "价格是多少？",
            "current_query": "价格是多少？",
            "previous_queries": [],
            "attempt": 0,
            "max_attempts": 2,
            "top_k": 5,
            "current_evidence": [],
            "accumulated_evidence": [],
            "retrieval_trace_ids": [],
        }
    )

    assert state["result"]["status"] == "clarification"
    assert state["result"]["answer"] == "请问要查询哪个地区的价格？"
