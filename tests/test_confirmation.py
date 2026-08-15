"""M5.2 机会式确认测试。"""

from spatialmem.confirmation import Confirmation, decide_upgrade


def test_multi_view_agreement_upgrades() -> None:
    sem = [("电风扇", "白色"), ("电风扇", "白色"), ("电风扇", "白色")]
    d = decide_upgrade(view_semantics=sem, min_views=2)
    assert d.upgrade
    assert d.name == "电风扇"
    assert d.color == "白色"
    assert d.sources == ["multi_view"]
    assert d.confidence >= 0.7


def test_single_view_does_not_upgrade_without_other_sources() -> None:
    d = decide_upgrade(view_semantics=[("电风扇", "白色")], min_views=2)
    assert not d.upgrade
    assert d.reason == "no_confirmation"


def test_disagreement_blocks_upgrade() -> None:
    sem = [("马桶", "白色"), ("电风扇", "白色")]
    d = decide_upgrade(view_semantics=sem, min_views=2)
    assert not d.upgrade


def test_interactive_confirmation_upgrades_single_view() -> None:
    """白风扇案例：单帧候选 + 用户交互确认 → 升级。"""
    d = decide_upgrade(
        view_semantics=[("电风扇", "白色")],
        confirmations=[Confirmation(source="interactive", name="电风扇", color="白色")],
        min_views=2,
    )
    assert d.upgrade
    assert "interactive" in d.sources
    assert d.confidence == 1.0


def test_ocr_plus_multi_view_accumulate() -> None:
    d = decide_upgrade(
        view_semantics=[("口红", "红色"), ("口红", "红色")],
        confirmations=[Confirmation(source="ocr", name="口红", color="红色",
                                    detail="印刷字 MOIST")],
        min_views=2,
    )
    assert d.upgrade
    assert set(d.sources) == {"multi_view", "ocr"}
    assert d.confidence == 1.0  # 0.7 + 0.9 封顶


def test_conflicting_interactive_answer_keeps_old_label() -> None:
    """多帧一致叫杯子，但用户纠正说不是 → 以交互为准（此处模拟纠正降级留给 M5.4）。"""
    d = decide_upgrade(
        view_semantics=[("杯子", "白色"), ("杯子", "白色")],
        confirmations=[Confirmation(source="interactive", name="水杯", color="白色")],
        min_views=2,
    )
    # 交互权重 1.0 压倒 multi_view 0.7，最终名称取交互答案（名称族相同则同组）
    assert d.upgrade
    assert d.name == "水杯"
