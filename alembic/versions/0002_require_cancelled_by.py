"""Require an actor for every cancelled registration.

Revision ID: 0002
Revises: 0001

The first migration may already have been applied by a developer. Keep its history
immutable and strengthen the constraint with a forward migration instead of editing
0001 in place.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `op.f` marks this as the final database name. Without it, the project's
    # CHECK naming convention would prefix the already-prefixed name a second time.
    op.drop_constraint(
        op.f("ck_registrations_cancel_fields"),
        "registrations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_registrations_cancel_fields"),
        "registrations",
        "(status = 'cancelled' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL) "
        "OR (status <> 'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_registrations_cancel_fields"),
        "registrations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_registrations_cancel_fields"),
        "registrations",
        "(status = 'cancelled' AND cancelled_at IS NOT NULL) "
        "OR (status <> 'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)",
    )
