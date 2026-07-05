<%!
import re
%>"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade(engine_name: str) -> None:
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name: str) -> None:
    globals()["downgrade_%s" % engine_name]()

% for db_name in re.split(r",\s*", config.get_main_option("databases")):

def upgrade_${db_name}() -> None:
    ${context.get("%s_upgrades" % db_name, "pass")}


def downgrade_${db_name}() -> None:
    ${context.get("%s_downgrades" % db_name, "pass")}
% endfor
