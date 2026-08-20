from walkie_dokie.evals.cases import FinalExpect, TurnExpect
from walkie_dokie.evals.checks import TurnObservation, check_final, check_turn


def _obs(**kwargs):
    defaults = dict(action="reply", intent=None, executed=False, replies=("好的",))
    defaults.update(kwargs)
    return TurnObservation(**defaults)


def test_action_mismatch_reports_expected_and_actual():
    failures = check_turn(TurnExpect(action="propose_task"), _obs(action="reply"), 0)
    assert len(failures) == 1
    assert "propose_task" in failures[0] and "reply" in failures[0]


def test_intent_checked_only_when_observable():
    obs = _obs(action="propose_task", intent="document_task")
    assert check_turn(TurnExpect(action="propose_task", intent="document_task"), obs, 0) == ()
    failures = check_turn(
        TurnExpect(action="propose_task", intent="chat"), obs, 0
    )
    assert "chat" in failures[0]


def test_executed_and_reply_keywords():
    obs = _obs(executed=True, replies=("已经处理完成，Claude 帮你搞定了",))
    failures = check_turn(
        TurnExpect(executed=False, reply_must_not_contain=("Claude",)), obs, 1
    )
    assert len(failures) == 2
    assert check_turn(TurnExpect(executed=True, reply_contains=("处理完成",)), obs, 1) == ()


def test_final_memory_and_blacklist():
    expect = FinalExpect(
        memory_must_contain={"name": "浮瓜"},
        memory_must_not_contain={"name": "小帮"},
        reply_must_not_contain=("dev@example.com",),
    )
    assert check_final(expect, {"name": "浮瓜"}, ("你好",)) == ()
    failures = check_final(expect, {"name": "小帮"}, ("联系 dev@example.com",))
    assert len(failures) == 3  # must_contain 不满足 + must_not 命中 + 黑名单命中


def test_final_blacklist_failure_reports_actual_reply():
    expect = FinalExpect(reply_must_not_contain=("dev@example.com",))
    (failure,) = check_final(expect, {}, ("你好", "联系 dev@example.com 问问"))
    assert "dev@example.com" in failure
    assert "联系 dev@example.com 问问" in failure


def test_memory_must_be_empty():
    expect = FinalExpect(memory_must_be_empty=True)
    assert check_final(expect, {}, ()) == ()
    failures = check_final(expect, {"name": "谁"}, ())
    assert "name" in failures[0]
