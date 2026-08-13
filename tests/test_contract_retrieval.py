from walkie_dokie.contract_intelligence.retrieval import (
    LocalChineseBm25Retriever,
    RetrievalDocument,
    chinese_search_tokens,
)


def test_local_bm25_exposes_tokens_scores_and_stable_ids():
    documents = (
        RetrievalDocument("rt_1", "ev_1", "服务合同\n第一条 服务费为人民币100元", {}),
        RetrievalDocument("rt_2", "ev_2", "服务合同\n第二条 合同期限为一年", {}),
        RetrievalDocument("rt_3", "ev_3", "服务合同\n第三条 联系地址为上海", {}),
    )

    result = LocalChineseBm25Retriever().search(
        query="服务费多少钱", documents=documents, top_k=3
    )

    assert result.candidates[0].evidence_id == "ev_1"
    assert result.candidates[0].stage_scores == {
        "bm25": result.candidates[0].score
    }
    assert "服务费" in result.query_tokens
    assert chinese_search_tokens("根据第一条，100元是否含税？").count("第一条") == 1
