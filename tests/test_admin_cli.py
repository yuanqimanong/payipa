"""Administrator CLI must not expose passwords in argv."""

from __future__ import annotations

import io

import pytest
from pyp_server import admin


def test_create_user_reads_password_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create(username: str, password: str, *, superuser: bool = False) -> str:
        captured.update(username=username, password=password, superuser=superuser)
        return "created"

    monkeypatch.setattr(admin, "_create_user", fake_create)
    monkeypatch.setattr(admin.sys, "stdin", io.StringIO("not-on-command-line\n"))
    assert admin.main(["create-user", "alice", "--password-stdin", "--superuser"]) == 0
    assert captured == {"username": "alice", "password": "not-on-command-line", "superuser": True}


def test_create_user_rejects_positional_password() -> None:
    with pytest.raises(SystemExit):
        admin.main(["create-user", "alice", "visible-secret"])
