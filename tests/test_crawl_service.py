"""采集应用服务的行为边界；无需 PG 验证校验顺序、双通道 provisioning 与 HTTP 注入。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import payipa_contracts as c
import pytest
from fastapi.testclient import TestClient
from payipa.crawl import service as core_service
from payipa.crawl.service import CrawlService, SourceRun
from pyp_server.main import create_app
from pyp_server.service import get_crawl_service


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[c.FieldRule(name="title", locator=c.Locator(type=c.LocatorType.CSS, expr="h1"), index=True)],
        fingerprint=["title"],
    )


def _dependencies(monkeypatch):
    setup = AsyncMock(return_value=(10, 20))
    pointer = c.RulePointer(rule_id="30", version=1, content_hash="hash")
    put = AsyncMock(return_value=pointer)
    provision = AsyncMock()
    batch = AsyncMock(return_value=(40, [object(), object()]))
    store = type("Store", (), {"put": put})()
    monkeypatch.setattr(core_service, "setup_source", setup)
    monkeypatch.setattr(core_service, "RuleStore", lambda engine: store)
    monkeypatch.setattr(core_service, "ensure_data_table", provision)
    monkeypatch.setattr(core_service, "create_batch_with_requests", batch)
    return setup, put, provision, batch, pointer


@pytest.mark.parametrize("channel", [c.Channel.PROD, c.Channel.TEST])
def test_service_provisions_channels_before_creating_batch(monkeypatch, channel) -> None:
    setup, put, provision, batch, pointer = _dependencies(monkeypatch)
    pyp, dc = object(), object()
    service = CrawlService(pyp=pyp, data_center=dc)

    async def create_batch(*args, **kwargs):
        assert [call.kwargs["channel"] for call in provision.await_args_list] == (
            [c.Channel.PROD] if channel is c.Channel.PROD else [c.Channel.PROD, c.Channel.TEST]
        )
        return 40, [object(), object()]

    batch.side_effect = create_batch

    async def run():
        return await service.run_source(
            uuid="demo",
            name="Demo",
            seed_urls=["https://example.com"],
            rule=_rule(),
            channel=channel,
            access_basis="owned",
            access_reference="fixture",
            access_confirmed=True,
            rate_limit=2,
        )

    result = anyio.run(run)
    assert result.as_dict() == {"batch_id": 40, "requests": 2, "dispatched": 0}
    assert setup.await_args.args[0] is pyp
    assert setup.await_args.kwargs["rate_limit"] == 2
    put.assert_awaited_once()
    assert provision.await_args.args == (dc, "demo", ["title"])
    assert provision.await_args.kwargs["engine_pyp"] is pyp
    assert batch.await_args.kwargs["rule_ptr"] is pointer
    assert batch.await_args.kwargs["channel"] is channel


@pytest.mark.parametrize(
    "overrides", [{"uuid": "bad-name!"}, {"indexed_fields": ["bad field"]}, {"channel": "unknown"}]
)
def test_invalid_identifiers_cannot_write_source_or_rule(monkeypatch, overrides) -> None:
    setup, put, provision, batch, _ = _dependencies(monkeypatch)
    options = {"uuid": "demo", "name": "Demo", "seed_urls": ["https://example.com"], "rule": _rule()}
    options.update(overrides)
    with pytest.raises(ValueError):
        anyio.run(lambda: CrawlService(object(), object()).run_source(**options))
    for dependency in (setup, put, provision, batch):
        dependency.assert_not_awaited()


def test_provisioning_failure_cannot_create_dispatchable_requests(monkeypatch) -> None:
    _, _, provision, batch, _ = _dependencies(monkeypatch)
    provision.side_effect = RuntimeError("data database unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        anyio.run(
            lambda: CrawlService(object(), object()).run_source(
                uuid="demo",
                name="Demo",
                seed_urls=["https://example.com"],
                rule=_rule(),
                channel=c.Channel.TEST,
            )
        )
    batch.assert_not_awaited()


def test_api_service_dependency_is_replaceable_without_database() -> None:
    fake = type("Service", (), {"run_source": AsyncMock(return_value=SourceRun(40, 2))})()
    app = create_app()
    app.dependency_overrides[get_crawl_service] = lambda: fake
    with TestClient(app) as client:
        response = client.post(
            "/api/sources/demo/run",
            json={"seed_urls": ["https://example.com"], "rule": _rule().model_dump(mode="json")},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"batch_id": 40, "requests": 2, "dispatched": 0}
        operation = client.get("/openapi.json").json()["paths"]["/api/sources/{uuid}/run"]["post"]
        assert all(parameter["name"] != "service" for parameter in operation.get("parameters", []))
    fake.run_source.assert_awaited_once()
    assert fake.run_source.await_args.kwargs["uuid"] == "demo"


def test_legacy_crawl_exports_keep_the_original_public_surface() -> None:
    from payipa.crawl import run

    expected = [
        "ACCESS_BASES",
        "ResultCommit",
        "advance_schedule",
        "auth_node",
        "batch_progress",
        "batch_trigger_context",
        "cancel_batch",
        "claim_queued_for_dispatch",
        "claim_schedule",
        "commit_result",
        "create_batch_for_task",
        "create_batch_with_requests",
        "defer_request_for_retry",
        "disable_schedule",
        "due_schedules",
        "enqueue_discovered",
        "enroll_agent",
        "ensure_data_table",
        "fence_ok",
        "finalize_batch_if_done",
        "finalize_request_batch",
        "handle_result",
        "issue_agent_enrollment",
        "mark_assigned",
        "mark_running",
        "pause_source_for_request",
        "queue_depth",
        "register_agent",
        "requeue_agent_inflight",
        "requeue_expired_leases",
        "requeue_request",
        "rerun_source",
        "resolve_ingest_context",
        "review_source_access",
        "revoke_agent_credential",
        "set_agent_offline",
        "set_request_state",
        "setup_source",
        "source_field_names",
        "source_of_request",
        "source_rate_limits",
        "sweep_canceling_batches",
        "touch_agent",
        "url_fingerprint",
    ]
    assert set(expected) <= set(run.__all__)
    for name in expected:
        value = getattr(run, name)
        if callable(value):
            assert value.__module__ != "payipa.crawl.run", name
