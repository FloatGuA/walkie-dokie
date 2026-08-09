import pytest

from walkie_dokie.orchestrator import memory


@pytest.fixture(autouse=True)
def _isolated_memory_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path)


def test_load_facts_missing_file_returns_empty_dict():
    assert memory.load_facts("test", "u1") == {}


def test_save_then_load_roundtrips():
    memory.save_facts("test", "u1", {"姓名": "张三"})
    assert memory.load_facts("test", "u1") == {"姓名": "张三"}


def test_save_facts_merges_with_existing_by_key():
    memory.save_facts("test", "u1", {"姓名": "张三", "部门": "人事部"})
    memory.save_facts("test", "u1", {"部门": "产品部"})  # 覆盖同一个 key
    assert memory.load_facts("test", "u1") == {"姓名": "张三", "部门": "产品部"}


def test_save_facts_with_empty_dict_does_nothing():
    memory.save_facts("test", "u1", {})
    assert memory.load_facts("test", "u1") == {}


def test_different_users_are_isolated():
    memory.save_facts("test", "u1", {"姓名": "张三"})
    memory.save_facts("test", "u2", {"姓名": "李四"})
    assert memory.load_facts("test", "u1") == {"姓名": "张三"}
    assert memory.load_facts("test", "u2") == {"姓名": "李四"}


async def test_extract_facts_without_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert await memory.extract_facts("我叫张三") == {}
