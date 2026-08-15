"""M5.3 检索优先问答测试。"""

import numpy as np

from spatialmem.retrieval import ConfirmedMemory, MemoryNode


def memory() -> ConfirmedMemory:
    return ConfirmedMemory(
        [
            MemoryNode("c1", "电风扇", "白色", source="interactive", confidence=1.0,
                       center=[2.0, 3.0, 0.0]),
            MemoryNode("c2", "键盘", "灰色", source="multi_view", confidence=0.8,
                       center=[1.0, 1.0, 0.5]),
            MemoryNode("c3", "葡萄", "紫色", source="multi_view", confidence=0.9),
        ]
    )


def test_memory_first_answer() -> None:
    ans = memory().query("电风扇在哪")
    assert ans.found
    assert not ans.fallback_used
    assert "根据记忆" in ans.text
    assert "电风扇" in ans.text


def test_query_with_question_word_cleaned() -> None:
    ans = memory().query("电风扇在哪里")
    assert ans.found
    assert "电风扇" in ans.text
    ans2 = memory().query("鼠标在哪儿")
    assert not ans2.found  # 记忆中无鼠标


def test_egocentric_direction_when_pose_given() -> None:
    # 观察者原点朝 +x，物体 (2,3,0) → 左前方/前方
    pose = np.eye(4)
    ans = memory().query("电风扇在哪", viewer_pose=pose)
    assert ans.found
    assert "米" in ans.text
    assert "根据记忆" in ans.text


def test_color_query_lists_white_items() -> None:
    ans = memory().query("白色")
    assert ans.found
    assert "电风扇" in ans.text


def test_no_hit_falls_back() -> None:
    ans = memory().query("不存在的蓝色恐龙")
    assert not ans.found
    assert ans.fallback_used
    assert "记忆中没找到" in ans.text


def test_named_query_does_not_leak_color_matches() -> None:
    # 查询含名称时，颜色匹配的无关项不得混入
    ans = memory().query("白色电风扇什么颜色")
    assert ans.found
    assert "电风扇" in ans.text
    assert "布料" not in ans.text


def test_substring_and_synonym_match() -> None:
    ans = memory().query("风扇")
    assert ans.found
    assert "电风扇" in ans.text
