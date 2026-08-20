import pytest

from walkie_dokie.main_agent.summarizer import validate_entries


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
