"""Dynamic data-table provisioning and reconciliation integration tests."""

from __future__ import annotations

import asyncio

import payipa_contracts as c
import pytest
from payipa.crawl.ingest import build_data_table, drop_data_table
from payipa.crawl.rules import RuleStore
from payipa.db import dynamic_schema
from payipa.db.pyp import DynamicSchema, Source
from payipa.db.settings import get_settings
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

_UUID = "dynschema"


def _rule() -> c.RulePack:
    return c.RulePack(
        fields=[
            c.FieldRule(
                name="title",
                locator=c.Locator(type=c.LocatorType.CSS, expr="h1"),
                index=True,
            )
        ],
        fingerprint=["title"],
    )


def test_failed_provisioning_is_recorded_and_reconciled(require_pg: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        table = build_data_table(_UUID, ["title"])
        real_create = dynamic_schema.create_data_table
        try:
            await drop_data_table(dc, table)
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM dynamic_schemas WHERE object_code=:code"),
                    {"code": _UUID},
                )
                await conn.execute(
                    text("DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:code)"),
                    {"code": _UUID},
                )
                await conn.execute(text("DELETE FROM sources WHERE uuid=:code"), {"code": _UUID})
                source_id = (
                    await conn.execute(
                        pg_insert(Source.__table__)
                        .values(
                            uuid=_UUID,
                            name="Dynamic schema fixture",
                            connector_type="web",
                            access_basis="owned",
                            access_reference="test fixture",
                            access_confirmed_at=func.now(),
                        )
                        .returning(Source.id)
                    )
                ).scalar_one()
            await RuleStore(pyp).put(source_id, _rule())

            async def fail_create(*_args, **_kwargs) -> None:
                raise RuntimeError("simulated DDL failure")

            monkeypatch.setattr(dynamic_schema, "create_data_table", fail_create)
            with pytest.raises(RuntimeError, match="simulated DDL failure"):
                await dynamic_schema.provision_data_schema(pyp, dc, _UUID, ["title"])

            async with pyp.connect() as conn:
                source_state = (
                    await conn.execute(
                        select(Source.provisioning_state, Source.provisioning_error).where(Source.uuid == _UUID)
                    )
                ).one()
                ledger_state = (
                    await conn.execute(
                        select(DynamicSchema.status, DynamicSchema.last_error).where(
                            DynamicSchema.kind == "data",
                            DynamicSchema.object_code == _UUID,
                            DynamicSchema.channel == "prod",
                        )
                    )
                ).one()
            assert source_state.provisioning_state == "error"
            assert "simulated DDL failure" in source_state.provisioning_error
            assert ledger_state.status == "error"
            assert "simulated DDL failure" in ledger_state.last_error

            monkeypatch.setattr(dynamic_schema, "create_data_table", real_create)
            report = await dynamic_schema.reconcile_data_schemas(pyp, dc, source_uuid=_UUID)
            assert report == {"checked": 1, "repaired": 1, "failed": 0}

            async with pyp.connect() as conn:
                source_state = (
                    await conn.execute(
                        select(Source.provisioning_state, Source.provisioning_error).where(Source.uuid == _UUID)
                    )
                ).one()
                ledger = (
                    await conn.execute(
                        select(
                            DynamicSchema.status,
                            DynamicSchema.table_name,
                            DynamicSchema.indexed_fields,
                            DynamicSchema.last_error,
                        ).where(
                            DynamicSchema.kind == "data",
                            DynamicSchema.object_code == _UUID,
                            DynamicSchema.channel == "prod",
                        )
                    )
                ).one()
            async with dc.connect() as conn:
                physical_table = (await conn.execute(text("SELECT to_regclass(:name)"), {"name": table.name})).scalar()
            assert source_state == ("ready", None)
            assert ledger == ("ready", table.name, ["title"], None)
            assert physical_table == table.name
        finally:
            monkeypatch.setattr(dynamic_schema, "create_data_table", real_create)
            async with pyp.begin() as conn:
                await conn.execute(
                    text("DELETE FROM dynamic_schemas WHERE object_code=:code"),
                    {"code": _UUID},
                )
                await conn.execute(
                    text("DELETE FROM rules WHERE source_id IN (SELECT id FROM sources WHERE uuid=:code)"),
                    {"code": _UUID},
                )
                await conn.execute(text("DELETE FROM sources WHERE uuid=:code"), {"code": _UUID})
            await drop_data_table(dc, table)
            await pyp.dispose()
            await dc.dispose()

    asyncio.run(main())


def test_invalid_legacy_source_code_settles_to_error(require_pg: None) -> None:
    legacy = "legacy-code"

    async def main() -> None:
        pyp = create_async_engine(get_settings().async_url("pyp"))
        dc = create_async_engine(get_settings().async_url("data_center"))
        try:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM sources WHERE uuid=:code"), {"code": legacy})
                await conn.execute(
                    pg_insert(Source.__table__).values(
                        uuid=legacy,
                        name="Legacy invalid code",
                        connector_type="web",
                        access_basis="owned",
                        access_reference="test fixture",
                        access_confirmed_at=func.now(),
                        provisioning_state="provisioning",
                    )
                )
            with pytest.raises(ValueError):
                await dynamic_schema.provision_data_schema(pyp, dc, legacy, [])
            async with pyp.connect() as conn:
                state = (
                    await conn.execute(
                        select(Source.provisioning_state, Source.provisioning_error).where(Source.uuid == legacy)
                    )
                ).one()
            assert state.provisioning_state == "error"
            assert "ValueError" in state.provisioning_error
        finally:
            async with pyp.begin() as conn:
                await conn.execute(text("DELETE FROM sources WHERE uuid=:code"), {"code": legacy})
            await pyp.dispose()
            await dc.dispose()

    asyncio.run(main())
