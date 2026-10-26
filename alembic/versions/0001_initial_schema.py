"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-26

Generated with `alembic revision --autogenerate`, then REVIEWED BY HAND (spec §15).
Manual changes after review:
  1. Added the standalone sequence `waitlist_queue_seq` (autogenerate does not emit it).
  2. downgrade() now also drops the sequence and the four ENUM types
     (drop_table alone leaves the types behind, so a re-upgrade would fail).
The result matches the reference SQL in spec §20.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_TYPES = ("registration_status", "event_status", "event_category", "user_role")


def upgrade() -> None:
    # MANUAL: FIFO waitlist numbers (spec §3). Taken only under the event lock.
    op.execute("CREATE SEQUENCE waitlist_queue_seq")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.String(length=100), nullable=False),
        sa.Column(
            "role",
            sa.Enum("student", "organiser", "admin", name="user_role"),
            server_default=sa.text("'student'"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("email = lower(btrim(email))", name=op.f("ck_users_email_normalized")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organiser_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "category",
            sa.Enum("tech", "cultural", "sports", "workshop", "other", name="event_category"),
            nullable=False,
        ),
        sa.Column("venue", sa.String(length=200), nullable=False),
        sa.Column("registration_opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registration_closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("waitlist_limit", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "published", "cancelled", name="event_status"),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL)", name=op.f("ck_events_cancelled_at")
        ),
        sa.CheckConstraint("capacity BETWEEN 1 AND 10000", name=op.f("ck_events_capacity")),
        sa.CheckConstraint("char_length(description) <= 5000", name=op.f("ck_events_description")),
        sa.CheckConstraint(
            "registration_opens_at < registration_closes_at "
            "AND registration_closes_at <= starts_at AND starts_at < ends_at",
            name=op.f("ck_events_time_order"),
        ),
        sa.CheckConstraint("waitlist_limit BETWEEN 0 AND 10000", name=op.f("ck_events_waitlist_limit")),
        sa.ForeignKeyConstraint(["organiser_id"], ["users.id"], name=op.f("fk_events_organiser_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index("ix_events_category", "events", ["category"], unique=False)
    op.create_index("ix_events_organiser_id", "events", ["organiser_id"], unique=False)
    op.create_index("ix_events_status_starts_at", "events", ["status", "starts_at"], unique=False)

    op.create_table(
        "registrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("confirmed", "waitlisted", "cancelled", "checked_in", name="registration_status"),
            nullable=False,
        ),
        sa.Column("queue_no", sa.BigInteger(), nullable=True),
        sa.Column("ticket_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waitlisted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.Uuid(), nullable=True),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_in_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) "
            "OR (status <> 'cancelled' AND cancelled_at IS NULL AND cancelled_by IS NULL)",
            name=op.f("ck_registrations_cancel_fields"),
        ),
        sa.CheckConstraint(
            "(status = 'checked_in' AND checked_in_at IS NOT NULL AND checked_in_by IS NOT NULL) "
            "OR (status <> 'checked_in' AND checked_in_at IS NULL AND checked_in_by IS NULL)",
            name=op.f("ck_registrations_checkin_fields"),
        ),
        sa.CheckConstraint(
            "(status = 'waitlisted' AND queue_no IS NOT NULL AND waitlisted_at IS NOT NULL) "
            "OR (status <> 'waitlisted' AND queue_no IS NULL)",
            name=op.f("ck_registrations_waitlist_fields"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('confirmed', 'checked_in') OR confirmed_at IS NOT NULL",
            name=op.f("ck_registrations_confirmed_fields"),
        ),
        sa.CheckConstraint("ticket_version >= 1", name=op.f("ck_registrations_ticket_version")),
        sa.ForeignKeyConstraint(
            ["cancelled_by"], ["users.id"], name=op.f("fk_registrations_cancelled_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["checked_in_by"], ["users.id"], name=op.f("fk_registrations_checked_in_by_users")
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], name=op.f("fk_registrations_event_id_events")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_registrations_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_registrations")),
        sa.UniqueConstraint("event_id", "user_id", name=op.f("uq_registrations_event_id")),
    )
    op.create_index("ix_registrations_event_status", "registrations", ["event_id", "status"], unique=False)
    op.create_index("ix_registrations_user_id", "registrations", ["user_id"], unique=False)
    op.create_index(
        "ix_registrations_waitlist",
        "registrations",
        ["event_id", "queue_no"],
        unique=False,
        postgresql_where=sa.text("status = 'waitlisted'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_registrations_waitlist",
        table_name="registrations",
        postgresql_where=sa.text("status = 'waitlisted'"),
    )
    op.drop_index("ix_registrations_user_id", table_name="registrations")
    op.drop_index("ix_registrations_event_status", table_name="registrations")
    op.drop_table("registrations")
    op.drop_index("ix_events_status_starts_at", table_name="events")
    op.drop_index("ix_events_organiser_id", table_name="events")
    op.drop_index("ix_events_category", table_name="events")
    op.drop_table("events")
    op.drop_table("users")
    # MANUAL: drop_table leaves ENUM types and the sequence behind; remove them too.
    for enum_name in ENUM_TYPES:
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
    op.execute("DROP SEQUENCE IF EXISTS waitlist_queue_seq")
