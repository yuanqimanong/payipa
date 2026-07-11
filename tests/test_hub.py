"""AgentHub 内存记账单测（无需 PG）。

回归锁定（M2 评审修复）：agent 心跳自报不可信（conn.py 占位恒报 slot_n/空），
update_heartbeat 只能刷新存活，**不得**把 free_slots 抹回或清空 inflight，否则会抹掉
on_dispatched/on_finished 的派发记账、导致派发环超发。
"""

from __future__ import annotations

from pyp_server.hub import AgentHub


class _WS:  # 占位：这些用例不触发 send_text
    pass


def test_dispatch_accounting_and_heartbeat_no_clobber() -> None:
    hub = AgentHub()
    hub.register("a", _WS(), slot_n=2)
    assert hub.pick_free().free_slots == 2

    hub.on_dispatched("a", "r1")
    hub.on_dispatched("a", "r2")
    assert hub.pick_free() is None  # 满槽，无空闲

    # 心跳到达（agent 恒报 free_slots=slot_n、inflight=[]）——不得抹掉记账
    hub.update_heartbeat("a")
    snap = hub.snapshots()[0]
    assert snap.slot_used == 2
    assert sorted(snap.inflight) == ["r1", "r2"]
    assert hub.pick_free() is None

    # 完成一个 → 释放一个槽（记账仍准确）
    hub.on_finished("a", "r1")
    free = hub.pick_free()
    assert free is not None and free.free_slots == 1
    assert hub.snapshots()[0].inflight == ["r2"]


def test_pick_free_prefers_most_free() -> None:
    hub = AgentHub()
    hub.register("a", _WS(), slot_n=4)
    hub.register("b", _WS(), slot_n=1)
    hub.on_dispatched("a", "x")  # a: 3 free, b: 1 free
    assert hub.pick_free().agent_id == "a"
    hub.on_dispatched("a", "y")
    hub.on_dispatched("a", "z")  # a: 1 free, b: 1 free（平手取任意，非 None）
    assert hub.pick_free() is not None


def test_pick_free_weight_tiebreak() -> None:
    hub = AgentHub()
    hub.register("light", _WS(), slot_n=2, weight=1)
    hub.register("heavy", _WS(), slot_n=2, weight=5)  # 同空闲槽，权重高者优先
    assert hub.pick_free().agent_id == "heavy"


def test_pick_free_group_affinity() -> None:
    hub = AgentHub()
    hub.register("auto", _WS(), slot_n=2, group_name="automation")
    hub.register("plain", _WS(), slot_n=2, group_name=None)
    # 指定分组：只在同组里选
    assert hub.pick_free("automation").agent_id == "auto"
    # 无该分组的空闲节点 → None（宁可排队等同组，不错派）
    assert hub.pick_free("gpu") is None
    # 不分组任务：任意空闲节点可派
    assert hub.pick_free(None) is not None


def test_pick_free_requires_engine_capability() -> None:
    hub = AgentHub()
    hub.register("http-only", _WS(), slot_n=2, engines=["http"])
    hub.register("browser-node", _WS(), slot_n=1, engines=["http", "browser"])
    assert hub.pick_free(engine="browser").agent_id == "browser-node"
    assert hub.pick_free(engine="http").agent_id == "http-only"
    assert hub.pick_free(engine="unknown") is None
