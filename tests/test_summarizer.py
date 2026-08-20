import logging
from types import SimpleNamespace

import pytest

from walkie_dokie.agents.security import sensitive_environment_overrides
from walkie_dokie.main_agent.summarizer import ClaudeAgentSummarizer, validate_entries


def test_valid_entry_passes_and_is_rebuilt_as_plain_dict():
    accepted, rejected = validate_entries(
        [{"fact": "用户在整理下周的活动安排", "evidence": ["帮我把下周的活动安排整理一下"]}],
        source_texts=("帮我把下周的活动安排整理一下，谢谢",),
    )
    assert accepted == ({"fact": "用户在整理下周的活动安排", "evidence": ["帮我把下周的活动安排整理一下"]},)
    assert rejected == ()


@pytest.mark.parametrize(
    "candidate,reason_keyword",
    [
        ({"fact": "", "evidence": ["x"]}, "fact"),
        ({"fact": "长" * 201, "evidence": ["x"]}, "200"),
        ({"fact": "f", "evidence": []}, "evidence"),
        ({"fact": "f", "evidence": [""]}, "evidence"),
        ({"fact": "f", "evidence": ["原文里没有这句"]}, "逐字"),
        ("not-a-dict", "dict"),
        ({"fact": "f"}, "evidence"),
    ],
)
def test_invalid_entries_are_rejected_with_reason(candidate, reason_keyword):
    accepted, rejected = validate_entries(
        [candidate], source_texts=("x 就是全部原文",)
    )
    assert accepted == ()
    assert len(rejected) == 1 and reason_keyword in rejected[0]


def test_evidence_must_be_verbatim_substring_of_any_source():
    accepted, _ = validate_entries(
        [{"fact": "f", "evidence": ["第二条的内容"]}],
        source_texts=("第一条", "这里有第二条的内容在其中"),
    )
    assert len(accepted) == 1


def test_max_entries_truncates_with_reason():
    cands = [{"fact": f"事实{i}", "evidence": ["源"]} for i in range(8)]
    accepted, rejected = validate_entries(cands, source_texts=("源",), max_entries=6)
    assert len(accepted) == 6
    assert any("6" in r for r in rejected)


def test_merge_mode_uses_evidence_union_as_sources():
    # 二级合并语义靠同一个函数：source_texts 换成旧条目 evidence 并集即可
    old_evidence_pool = ("他孙女叫小雨", "下周三要交材料")
    accepted, rejected = validate_entries(
        [
            {"fact": "孙女小雨、周三交材料", "evidence": ["他孙女叫小雨", "下周三要交材料"]},
            {"fact": "新编造的事实", "evidence": ["这句不在任何旧 evidence 里"]},
        ],
        source_texts=old_evidence_pool,
    )
    assert len(accepted) == 1 and len(rejected) == 1


def _fake_query(payload: dict):
    """伪 SDK：只产出一条形如 ResultMessage 的消息（结构化结果走 structured_output）。"""

    async def query_fn(*, prompt, options):
        yield SimpleNamespace(structured_output=payload, is_error=False, subtype="success")

    return query_fn


async def test_summarize_parses_entries_from_structured_output():
    summarizer = ClaudeAgentSummarizer()
    entries = await summarizer.summarize(
        ({"role": "user", "content": "我孙女叫小雨"},),
        query_fn=_fake_query({"entries": [{"fact": "孙女叫小雨", "evidence": ["我孙女叫小雨"]}]}),
    )
    assert entries == ({"fact": "孙女叫小雨", "evidence": ["我孙女叫小雨"]},)


async def test_merge_sends_entries_payload_and_parses_entries():
    captured = {}

    async def query_fn(*, prompt, options):
        captured["prompt"] = prompt
        yield SimpleNamespace(
            structured_output={"entries": [{"fact": "合并后", "evidence": ["旧片段"]}]},
            is_error=False,
            subtype="success",
        )

    summarizer = ClaudeAgentSummarizer()
    entries = await summarizer.merge(
        ({"fact": "旧事实", "evidence": ["旧片段"]},), query_fn=query_fn
    )
    assert entries == ({"fact": "合并后", "evidence": ["旧片段"]},)
    assert "旧事实" in captured["prompt"]


SUMMARIZER_LOGGER = "walkie_dokie.main_agent.summarizer"


async def test_summarize_logs_token_usage(caplog):
    """token 用量是压缩这条后台链路唯一的成本可观测点：必须落 INFO 日志。"""

    async def query_fn(*, prompt, options):
        yield SimpleNamespace(usage={"input_tokens": 1234, "output_tokens": 56})
        yield SimpleNamespace(
            structured_output={"entries": []}, is_error=False, subtype="success"
        )

    with caplog.at_level(logging.INFO, logger=SUMMARIZER_LOGGER):
        await ClaudeAgentSummarizer().summarize((), query_fn=query_fn)

    lines = [record.getMessage() for record in caplog.records]
    assert any("mode=summarize" in line and "1234" in line for line in lines)


async def test_merge_logs_token_usage_with_merge_mode(caplog):
    async def query_fn(*, prompt, options):
        yield SimpleNamespace(
            structured_output={"entries": []},
            is_error=False,
            subtype="success",
            usage={"input_tokens": 77},
        )

    with caplog.at_level(logging.INFO, logger=SUMMARIZER_LOGGER):
        await ClaudeAgentSummarizer().merge((), query_fn=query_fn)

    lines = [record.getMessage() for record in caplog.records]
    assert any("mode=merge" in line and "77" in line for line in lines)


async def test_error_message_raises_runtime_error():
    async def query_fn(*, prompt, options):
        yield SimpleNamespace(structured_output=None, is_error=True, subtype="error_max_turns")

    summarizer = ClaudeAgentSummarizer()
    with pytest.raises(RuntimeError, match="error_max_turns"):
        await summarizer.merge((), query_fn=query_fn)


async def test_missing_structured_output_raises():
    async def query_fn(*, prompt, options):
        yield SimpleNamespace(text="思考中")

    summarizer = ClaudeAgentSummarizer()
    with pytest.raises(RuntimeError, match="没有返回结构化结果"):
        await summarizer.summarize((), query_fn=query_fn)


def test_summarizer_options_are_isolated():
    """options 隔离五件套 tripwire（对齐 evals/judge.py 的 _judge_options）。"""

    from walkie_dokie.main_agent.summarizer import _summarizer_options

    options = _summarizer_options("sys", "haiku")
    assert options.model == "haiku"
    assert options.system_prompt == "sys"
    assert options.allowed_tools == []
    assert options.max_turns == 6
    assert options.output_format["type"] == "json_schema"
    assert options.setting_sources == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.skills == []
    assert options.env == sensitive_environment_overrides()
