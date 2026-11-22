"""Dashboard APIs (frontend-ready, one call per screen).

    User  (own data only):  GET /api/v1/dashboard/months, GET /api/v1/dashboard?month=YYYY-MM
    Admin (everyone):       GET /api/v1/admin/dashboard/months, GET /api/v1/admin/dashboard?month=YYYY-MM,
                            GET /api/v1/admin/dashboard/users/{user_id}?month=YYYY-MM
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from agency_platform.auth.accounts import AccountRepository
from agency_platform.auth.deps import AdminUser, CurrentUser
from agency_platform.billing.usage import UsageMeter
from agency_platform.cache import get_redis
from agency_platform.db import get_db
from agency_platform.errors import NotFoundError
from agency_platform.service import openapi
from edge_gateway import dashboard
from edge_gateway.schemas import MONTH_PATTERN, AdminDashboard, MonthsResponse, UserDashboard

Month = Annotated[
    str | None,
    Query(pattern=MONTH_PATTERN, description="YYYY-MM (UTC). Omit for the current month. "
                                             "Options come from the /months endpoint.", examples=["2026-11"]),
]

dashboard_router = APIRouter(tags=["Dashboard"], responses=openapi.AUTHED)
admin_dashboard_router = APIRouter(tags=["Admin: dashboard"], responses=openapi.ADMIN)


@dashboard_router.get("/months", summary="Months for my month picker")
async def my_months(user: CurrentUser) -> MonthsResponse:
    return await dashboard.months_for(get_db(), user.id)


@dashboard_router.get("", summary="My usage dashboard for one month", responses=openapi.errors(400, 422))
async def my_dashboard(user: CurrentUser, month: Month = None) -> UserDashboard:
    """Totals, change vs the previous month, per-topic breakdown, a gap-free daily series,
    run stats, recent runs and (for the current month) live quota status. Only your own data."""
    account = await AccountRepository(get_db()).get(user.id)
    if account is None:
        raise NotFoundError("Account not found")
    return await dashboard.user_dashboard(get_db(), UsageMeter(get_redis()), account, month)


@admin_dashboard_router.get("/months", summary="Months with platform usage")
async def all_months(_: AdminUser) -> MonthsResponse:
    return await dashboard.months_for(get_db(), None)


@admin_dashboard_router.get("", summary="Platform dashboard for one month", responses=openapi.errors(400, 422))
async def platform_dashboard(_: AdminUser, month: Month = None) -> AdminDashboard:
    """All users: tokens and cost per topic and per model, daily series, month-over-month change,
    users (active, new, per plan), run outcomes and the top spenders."""
    return await dashboard.admin_dashboard(get_db(), month)


@admin_dashboard_router.get("/users/{user_id}", summary="One user's dashboard (the view that user sees)",
                            responses=openapi.errors(400, 404, 422))
async def user_dashboard(user_id: uuid.UUID, _: AdminUser, month: Month = None) -> UserDashboard:
    account = await AccountRepository(get_db()).get(str(user_id))
    if account is None:
        raise NotFoundError("User not found")
    return await dashboard.user_dashboard(get_db(), UsageMeter(get_redis()), account, month)
