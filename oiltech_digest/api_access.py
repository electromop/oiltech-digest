"""Доступ к API: кто вошёл (сессия по cookie) и только ли администратор. Отдельно от api.py, чтобы маршруты в своих
модулях (журнал ОС, #189) брали те же зависимости без циклического импорта; api.py отдаёт их под прежними именами, и
подмена в тестах (`api.app.dependency_overrides[api.require_user]`) действует на все маршруты."""

from __future__ import annotations

from typing import Any

from fastapi import Cookie, Depends, HTTPException

from oiltech_digest import config
from oiltech_digest.db import repository


def require_user(session_token: str | None = Cookie(default=None, alias=config.AUTH_COOKIE_NAME)) -> dict[str, Any]:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = repository.get_user_by_session(session_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Доступ только администратору (настройка тегов/скоринга/источников, пользователи)."""
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user
