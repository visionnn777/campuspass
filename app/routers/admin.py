"""/api/v1/admin: user management (spec §5)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import AdminUser, DbSession
from app.models import UserRole
from app.schemas.admin import AdminUserUpdate
from app.schemas.auth import UserOut
from app.schemas.common import Page, PageParams, page_params
from app.services import admin_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=Page[UserOut], summary="List users")
def list_users(
    admin: AdminUser,
    db: DbSession,
    page: Annotated[PageParams, Depends(page_params)],
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> Page[UserOut]:
    return admin_service.list_users(db, role=role, is_active=is_active, page=page)


@router.patch("/users/{user_id}", response_model=UserOut, summary="Change a user's role or active status")
def update_user(user_id: uuid.UUID, data: AdminUserUpdate, admin: AdminUser, db: DbSession) -> UserOut:
    return admin_service.update_user(db, admin, user_id, data)
