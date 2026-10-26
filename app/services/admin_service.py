"""User administration with self- and last-admin guards (spec §5)."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import User, UserRole
from app.schemas.admin import AdminUserUpdate
from app.schemas.auth import UserOut
from app.schemas.common import Page, PageParams


def list_users(
    db: Session, *, role: UserRole | None, is_active: bool | None, page: PageParams
) -> Page[UserOut]:
    filters = []
    if role is not None:
        filters.append(User.role == role)
    if is_active is not None:
        filters.append(User.is_active.is_(is_active))
    total = db.scalar(select(func.count()).select_from(User).where(*filters)) or 0
    users = db.scalars(
        select(User).where(*filters).order_by(User.created_at, User.id).offset(page.offset).limit(page.page_size)
    ).all()
    return Page[UserOut](
        items=[UserOut.model_validate(u) for u in users],
        page=page.page,
        page_size=page.page_size,
        total=total,
    )


def update_user(db: Session, actor: User, user_id: uuid.UUID, data: AdminUserUpdate) -> UserOut:
    changes = data.model_dump(exclude_unset=True)
    with db.begin():
        # One global user-row lock order: lock all currently active admins AND the target,
        # de-duplicated by the WHERE clause and ordered by UUID. Promotion/check-in may lock
        # user rows after their event/registration locks, also in UUID order; locking the
        # target separately after admins would invert that order and could deadlock.
        locked_users = db.scalars(
            select(User)
            .where(
                or_(
                    and_(User.role == UserRole.ADMIN, User.is_active.is_(True)),
                    User.id.in_({actor.id, user_id}),
                )
            )
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        target = next((u for u in locked_users if u.id == user_id), None)
        current_actor = next((u for u in locked_users if u.id == actor.id), None)
        if target is None:
            raise AppError(404, "USER_NOT_FOUND", "User not found.")
        if current_actor is None or not current_actor.is_active or current_actor.role != UserRole.ADMIN:
            raise AppError(403, "FORBIDDEN", "An active administrator account is required.")
        # Refresh/re-check after lock waits: a concurrent admin update can change a row's
        # role or active state. The target AND actor are included in the query even if they
        # cease to match the active-admin predicate while this transaction waits.
        active_admins = [u for u in locked_users if u.role == UserRole.ADMIN and u.is_active]

        new_role = changes.get("role", target.role)
        new_active = changes.get("is_active", target.is_active)
        was_active_admin = target.role == UserRole.ADMIN and target.is_active
        stays_active_admin = new_role == UserRole.ADMIN and new_active

        if was_active_admin and not stays_active_admin:
            if target.id == current_actor.id:
                raise AppError(409, "CANNOT_MODIFY_SELF", "You cannot demote or disable yourself.")
            # Same pattern as seat booking: lock, THEN count.
            if len(active_admins) <= 1:
                raise AppError(409, "LAST_ADMIN", "At least one active admin must remain.")

        target.role = new_role
        target.is_active = new_active
        db.flush()
        out = UserOut.model_validate(target)
    return out
