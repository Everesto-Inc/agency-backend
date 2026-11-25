"""Edge gateway entrypoint."""
from fastapi import FastAPI

from agency_platform.auth.deps import get_token_service
from agency_platform.config.registry import plans, topics
from agency_platform.config.settings import get_settings
from agency_platform.service import ServiceSpec, create_service_app
from edge_gateway.quality_gauges import start_quality_gauges, stop_quality_gauges
from edge_gateway.routes_admin import admin_router
from edge_gateway.routes_auth import router as auth_router
from edge_gateway.routes_chat import router as chat_router
from edge_gateway.routes_chat import topics_router
from edge_gateway.routes_conversations import conversations_router
from edge_gateway.routes_dashboard import admin_dashboard_router, dashboard_router
from edge_gateway.routes_images import UPLOAD_BODY_LIMIT, images_router, uploads_router
from edge_gateway.routes_usage import usage_router

settings = get_settings()

DESCRIPTION = """
The public API of the agency: accounts, the `/chat` front door, the topic catalogue and usage.

**Quick start**
1. `POST /api/v1/auth/register` (or `/login`) and copy `access_token`.
2. Click **Authorize** and paste it.
3. `POST /api/v1/chat` with any question, or call a topic directly (`GET /api/v1/topics` lists them).
4. `GET /api/v1/dashboard?month=YYYY-MM` is your monthly usage dashboard (tokens and cost per topic,
   daily chart data, month-over-month change); `GET /api/v1/dashboard/months` fills the month picker.

**Each assistant has its own API and docs** at `/api/v1/<topic>/docs`
(`ev_research`, `product_finder`, `finance`). Locally they run on their own ports:
[EV research](http://localhost:8001/api/v1/ev_research/docs) ·
[Product finder](http://localhost:8002/api/v1/product_finder/docs) ·
[Finance](http://localhost:8003/api/v1/finance/docs).

**Errors** always look like `{"error": {"code", "message", "request_id", "details"}}`.
Branch on `code` (e.g. `SUBSCRIPTION_REQUIRED` -> show an upgrade prompt, `QUOTA_EXCEEDED` -> retry after
the `Retry-After` header).
"""

TAGS = [
    {"name": "Auth", "description": "Register, log in, refresh and revoke tokens."},
    {"name": "Account", "description": "My account, data export and deletion (GDPR)."},
    {"name": "Chat", "description": "One entry point for every assistant: the question is routed to the right topic."},
    {"name": "Conversations", "description": "My chat history: list, open (questions + answers), rename, pin, delete. "
                                             "A conversation id is the `session_id` of POST /chat."},
    {"name": "Topics", "description": "The assistants, what they do, and which ones my plan includes."},
    {"name": "Usage", "description": "My tokens and cost, per topic (live today, plus history)."},
    {"name": "Dashboard", "description": "My monthly usage dashboard: pick a month (GET /dashboard/months), "
                                         "then one call returns everything the screen needs. Own data only."},
    {"name": "Admin: dashboard", "description": "Platform-wide monthly dashboard, and any user's dashboard. Admin only."},
    {"name": "Admin: usage & billing", "description": "Spend per topic and per user. Admin only."},
    {"name": "Admin: models & routing", "description": "Model performance, tier routing, canaries. Admin only."},
    {"name": "Admin: topics & experiments", "description": "Topic outcomes and prompt A/B tests. Admin only."},
    {"name": "Admin: quality & evals", "description": "Eval results, online judge scores, drift, eval-case curation. Admin only."},
    {"name": "Admin: guardrails", "description": "Every time a guardrail fired (quota, injection, PII, grounding, uploads…). Admin only."},
    {"name": "Admin: accounts & runs", "description": "Plans, disabling users, run and LLM-call inspection. Admin only."},
]


async def startup(_: FastAPI) -> None:
    plans()
    topics()
    if not get_token_service().can_issue:
        raise RuntimeError("Edge gateway needs JWT_PRIVATE_KEY (or JWT_SECRET_KEY in dev) to issue tokens")


app = create_service_app(
    settings,
    ServiceSpec(
        title="Autonomous Agency API",
        description=DESCRIPTION,
        tags=TAGS,
        routers=[
            (auth_router, "/api/v1/auth"),
            (chat_router, "/api/v1/chat"),
            (conversations_router, "/api/v1/conversations"),
            (images_router, "/api/v1/images"),
            (uploads_router, "/api/v1/uploads"),
            (topics_router, "/api/v1/topics"),
            (usage_router, "/api/v1/usage"),
            (dashboard_router, "/api/v1/dashboard"),
            (admin_dashboard_router, "/api/v1/admin/dashboard"),
            (admin_router, "/api/v1/admin"),
        ],
        body_limits={"/api/v1/uploads": UPLOAD_BODY_LIMIT},
        on_startup=[startup, start_quality_gauges],
        on_shutdown=[stop_quality_gauges],
    ),
)
