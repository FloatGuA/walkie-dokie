import json

import pytest

from walkie_dokie.main_agent.base import MemoryOperation
from walkie_dokie.main_agent.memory import JsonMemoryRepository, render_memory_notice


def test_missing_profile_returns_empty(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    assert repository.load("test", "u1") == {}


def test_set_update_and_delete_roundtrip(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    changes = repository.apply(
        "test",
        "u1",
        (
            MemoryOperation("set", "name", "张三", "我叫张三"),
            MemoryOperation("set", "department", "人事部", "我的部门是人事部"),
        ),
        source_text="我叫张三，我的部门是人事部",
    )
    assert len(changes) == 2
    assert repository.load("test", "u1") == {
        "name": "张三",
        "department": "人事部",
    }

    repository.apply(
        "test",
        "u1",
        (
            MemoryOperation("set", "department", "产品部", "我的部门改成产品部"),
            MemoryOperation("delete", "name", None, "忘记我的名字"),
        ),
        source_text="我的部门改成产品部，请忘记我的名字",
    )
    assert repository.load("test", "u1") == {"department": "产品部"}


def test_legacy_chinese_keys_are_read_and_migrated_on_next_write(tmp_path):
    path = tmp_path / "test_u1.json"
    path.write_text(
        json.dumps({"姓名": "张三", "部门": "人事部"}, ensure_ascii=False),
        encoding="utf-8",
    )
    repository = JsonMemoryRepository(tmp_path)
    assert repository.load("test", "u1") == {
        "name": "张三",
        "department": "人事部",
    }
    repository.apply(
        "test",
        "u1",
        (MemoryOperation("set", "job_title", "经理", "我的职位是经理"),),
        source_text="我的职位是经理",
    )
    migrated = [candidate for candidate in tmp_path.glob("v2_*.json")]
    assert len(migrated) == 1
    assert json.loads(migrated[0].read_text(encoding="utf-8")) == {
        "name": "张三",
        "department": "人事部",
        "job_title": "经理",
    }
    # 迁移采用 copy-on-write，原始文件保留供审计/回滚。
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "姓名": "张三",
        "部门": "人事部",
    }


def test_unknown_or_empty_fields_are_rejected(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    changes = repository.apply(
        "test",
        "u1",
        (
            MemoryOperation("set", "hobby", "跑步", "我喜欢跑步"),  # type: ignore[arg-type]
            MemoryOperation("set", "name", "   ", "我叫   "),
        ),
        source_text="我喜欢跑步，我叫   ",
    )
    assert changes == []
    assert repository.load("test", "u1") == {}


def test_notice_describes_only_applied_changes():
    notice = render_memory_notice(
        [
            {"action": "set", "field": "name", "label": "姓名", "value": "张三"},
            {
                "action": "delete",
                "field": "department",
                "label": "部门",
                "value": None,
            },
        ]
    )
    assert "姓名：张三" in notice
    assert "已忘记：部门" in notice
    assert "修改或忘掉" in notice


def test_profile_key_cannot_escape_memory_directory(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    repository.apply(
        "../platform",
        "../../user",
        (MemoryOperation("set", "name", "张三", "我叫张三"),),
        source_text="我叫张三",
    )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path
    assert ".." not in files[0].name


def test_profile_keys_that_sanitize_the_same_do_not_collide(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    repository.apply(
        "test",
        "a/b",
        (MemoryOperation("set", "name", "甲", "我叫甲"),),
        source_text="我叫甲",
    )
    repository.apply(
        "test",
        "a?b",
        (MemoryOperation("set", "name", "乙", "我叫乙"),),
        source_text="我叫乙",
    )
    assert repository.load("test", "a/b") == {"name": "甲"}
    assert repository.load("test", "a?b") == {"name": "乙"}
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_allowed_name_field_is_rejected_when_evidence_describes_assistant(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    changes = repository.apply(
        "test",
        "u1",
        (MemoryOperation("set", "name", "小帮", "你是小帮"),),
        source_text="你是小帮，是我的智能助手",
    )
    assert changes == []
    assert repository.load("test", "u1") == {}


def test_evidence_must_be_an_exact_quote_from_current_user_turn(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    changes = repository.apply(
        "test",
        "u1",
        (MemoryOperation("set", "name", "张三", "我叫张三"),),
        source_text="你好",
    )
    assert changes == []


def test_common_chinese_identity_and_elliptical_job_clause_are_grounded(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    source = "我是浮瓜，是这个项目的开发者。"

    changes = repository.apply(
        "test",
        "u1",
        (
            MemoryOperation("set", "name", "浮瓜", "我是浮瓜"),
            # Mirrors the real model output: value omits the possessive “的” and
            # the continuation clause inherits its first-person subject.
            MemoryOperation(
                "set", "job_title", "项目开发者", "是这个项目的开发者"
            ),
        ),
        source_text=source,
    )

    assert {change["field"] for change in changes} == {"name", "job_title"}
    assert repository.load("test", "u1") == {
        "name": "浮瓜",
        "job_title": "项目开发者",
    }

    # The stronger full-sentence quote requested by the MainAgent prompt is valid too.
    assert repository.validate(
        (
            MemoryOperation(
                "set",
                "job_title",
                "项目开发者",
                "我是浮瓜，是这个项目的开发者",
            ),
        ),
        source_text=source,
    )


def test_elliptical_clause_requires_same_sentence_first_person_context(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    changes = repository.apply(
        "test",
        "u1",
        (
            MemoryOperation(
                "set", "job_title", "项目开发者", "是这个项目的开发者"
            ),
        ),
        source_text="浮瓜。是这个项目的开发者。",
    )
    assert changes == []


def test_memory_value_must_still_be_grounded_in_evidence(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    changes = repository.apply(
        "test",
        "u1",
        (MemoryOperation("set", "name", "西瓜", "我是浮瓜"),),
        source_text="我是浮瓜",
    )
    assert changes == []


@pytest.mark.parametrize(
    "source,evidence,field,value",
    [
        ("帮我在文档中写一句‘我叫张三’", "我叫张三", "name", "张三"),
        ("请翻译：my name is Alice", "my name is Alice", "name", "Alice"),
        ("例句是：我是经理", "我是经理", "job_title", "经理"),
    ],
)
def test_quoted_document_content_is_not_treated_as_user_identity(
    tmp_path, source, evidence, field, value
):
    repository = JsonMemoryRepository(tmp_path)
    changes = repository.apply(
        "test",
        "u1",
        (MemoryOperation("set", field, value, evidence),),
        source_text=source,
    )
    assert changes == []


@pytest.mark.parametrize(
    "source",
    [
        "请不要忘记我的名字",
        "你忘记我的名字了吗？",
        "不要删除我的姓名",
    ],
)
def test_negated_or_question_delete_is_rejected(tmp_path, source):
    repository = JsonMemoryRepository(tmp_path)
    repository.apply(
        "test",
        "u1",
        (MemoryOperation("set", "name", "张三", "我叫张三"),),
        source_text="我叫张三",
    )
    changes = repository.apply(
        "test",
        "u1",
        (MemoryOperation("delete", "name", None, source),),
        source_text=source,
    )
    assert changes == []
    assert repository.load("test", "u1") == {"name": "张三"}


def test_delete_phrased_as_shan_diao_is_applied(tmp_path):
    repository = JsonMemoryRepository(tmp_path)
    repository.apply(
        "test",
        "u1",
        (MemoryOperation("set", "name", "张三", "我叫张三"),),
        source_text="我叫张三",
    )
    source = "刚才说错了，把我的名字删掉吧"
    changes = repository.apply(
        "test",
        "u1",
        (MemoryOperation("delete", "name", None, "把我的名字删掉"),),
        source_text=source,
    )
    assert [change["action"] for change in changes] == ["delete"]
    assert repository.load("test", "u1") == {}


def test_corrupt_profile_is_never_overwritten_by_apply(tmp_path):
    path = tmp_path / "test_u1.json"
    path.write_text("{not json", encoding="utf-8")
    repository = JsonMemoryRepository(tmp_path)
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        repository.apply(
            "test",
            "u1",
            (MemoryOperation("set", "name", "张三", "我叫张三"),),
            source_text="我叫张三",
        )
    assert path.read_text(encoding="utf-8") == "{not json"


def test_ambiguous_legacy_key_is_not_automatically_shared(tmp_path):
    (tmp_path / "test_a_b.json").write_text(
        json.dumps({"name": "旧用户"}, ensure_ascii=False), encoding="utf-8"
    )
    repository = JsonMemoryRepository(tmp_path)
    assert repository.load("test", "a/b") == {}
    assert repository.load("test", "a?b") == {}
