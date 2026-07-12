"""payipa-contracts 冒烟/契约测试：schema 完整、标注就位、判别联合、版本握手、错误码。"""

from __future__ import annotations

import payipa_contracts as c
import pytest
from pydantic import TypeAdapter


def test_contract_version_handshake() -> None:
    assert c.CONTRACT_VERSION == 4  # v4：attempt fencing + TaskAck（P0-10）
    assert c.MIN_SUPPORTED_CONTRACT_VERSION == 4
    assert c.is_compatible(c.CONTRACT_VERSION)
    assert not c.is_compatible(1)
    assert not c.is_compatible(c.CONTRACT_VERSION + 1)
    with pytest.raises(ValueError):
        c.assert_compatible(0)


def test_error_codes_first_batch_are_negative() -> None:
    for code in c.ErrorCode:
        assert code < 0
    assert int(c.ErrorCode.ACCESS_PAUSED) == -3
    assert c.label(-3) == "访问暂停"
    assert int(c.ErrorCode.SOFT_FAIL) == -4
    assert c.label(-4) == "软失败"
    assert c.label(-999).startswith("未知")


def test_request_state_positive() -> None:
    for state in c.enums.RequestState:
        assert state >= 0


@pytest.mark.parametrize(
    "model",
    [
        c.TaskSpec,
        c.RulePack,
        c.RuleManifest,
        c.Artifact,
        c.ArtifactRef,
        c.ResultBatch,
        c.FieldMeta,
        c.NodeSnapshot,
        c.BatchProgress,
        c.QualityMetric,
        c.AuditEvent,
        c.LifecycleEvent,
        c.RegisterReq,
        c.Heartbeat,
        c.StatusReport,
        c.TaskAssign,
        c.Cancel,
        c.RegisterAck,
        c.ResultAck,
        c.ErrorFrame,
    ],
)
def test_every_model_has_json_schema(model: type) -> None:
    schema = model.model_json_schema()
    assert schema["type"] == "object"


def test_fields_carry_effective_annotation() -> None:
    # 契约红线：字段必须诚实标注「已生效/未生效」
    props = c.TaskSpec.model_json_schema()["properties"]
    assert props["task_id"]["x-effective"] is True
    assert props["group"]["x-effective"] is True
    assert props["group"]["description"].startswith("[已生效]")
    assert props["account"]["x-effective"] is False
    assert props["account"]["description"].startswith("[未生效]")
    assert props["task_id"]["description"].startswith("[已生效]")


def test_client_frame_discriminated_union_roundtrip() -> None:
    adapter = TypeAdapter(c.ClientFrame)
    frame = adapter.validate_python({"type": "status", "req_id": "r1", "state": 3})
    assert isinstance(frame, c.StatusReport)
    assert frame.state == 3
    # 错误码作为负数 state 也能承载
    err = adapter.validate_python({"type": "status", "req_id": "r2", "state": int(c.ErrorCode.ACCESS_PAUSED)})
    assert err.state == -3


def test_monitor_success_rate_nullable() -> None:
    # P0-25：无样本成功率必须是 None（暂无样本），绝不默认 1.0 装健康
    for model, kw in [
        (c.NodeMetric, {"agent_id": "a1", "online": True, "slot_n": 4}),
        (c.SourceHealth, {"source": "s1"}),
        (c.SystemOverview, {}),
    ]:
        m = model(**kw)
        assert m.success_rate is None
        assert "暂无样本" in model.model_json_schema()["properties"]["success_rate"]["description"]
    assert c.SourceHealth(source="s1", success_rate=0.5).success_rate == 0.5


def test_task_assign_carries_task_spec() -> None:
    task = c.TaskSpec(
        task_id="t1",
        req_id="rq1",
        batch_id="b1",
        source="demo",
        target="https://example.com",
        rule_ptr=c.RulePointer(rule_id="r1", version=1, content_hash="deadbeef"),
    )
    assign = c.TaskAssign(task=task)
    # 序列化 round-trip
    dumped = assign.model_dump_json()
    restored = c.TaskAssign.model_validate_json(dumped)
    assert restored.task.rule_ptr.content_hash == "deadbeef"
    assert restored.task.channel == c.Channel.PROD


def test_rule_regexes_are_validated_at_publish_time() -> None:
    with pytest.raises(ValueError):
        c.LayoutMatch(url_regex="[")
    with pytest.raises(ValueError):
        c.FailWhen(body_regex=["("])
    with pytest.raises(ValueError):
        c.FailWhen(status_in=[42])
