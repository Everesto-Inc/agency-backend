"""Talks to the running platform as the eval service account: logs in at the
edge, runs topic agents through their public APIs (exactly like a user), and
gets a model-gateway client (client id 'quality') for judge calls."""
import time
import uuid
from dataclasses import dataclass

import httpx

from agency_platform.config.registry import topics
from agency_platform.errors import UnauthorizedError
from agency_platform.llm.call_token import CallClaims, mint_call_token
from agency_platform.llm.client import ModelGatewayClient
from quality.checks import RunOutput
from quality.settings import QualitySettings


@dataclass
class Session:
    access_token: str
    user_id: str
    plan: str


class AgencyClient:
    def __init__(self, settings: QualitySettings, http: httpx.AsyncClient) -> None:
        self._s = settings
        self._http = http
        self._session: Session | None = None

    async def login(self) -> Session:
        if self._session:
            return self._session
        if self._s.eval_user_password is None:
            raise UnauthorizedError("EVAL_USER_PASSWORD is not configured")
        r = await self._http.post(
            f"{self._s.edge_url}/api/v1/auth/login",
            json={"email": self._s.eval_user_email, "password": self._s.eval_user_password.get_secret_value()},
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        me = (await self._http.get(f"{self._s.edge_url}/api/v1/auth/me", headers=self._auth(token))).json()
        self._session = Session(token, me["id"], me["plan"])
        return self._session

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def run_topic(self, topic: str, prompt: str, timeout_s: float) -> RunOutput:
        s = await self.login()
        url = f"{str(topics().topics[topic].service_url).rstrip('/')}/api/v1/{topic}/run"
        started = time.perf_counter()
        r = await self._http.post(
            url, json={"prompt": prompt, "session_id": f"eval-{uuid.uuid4().hex[:12]}", "stream": False},
            headers=self._auth(s.access_token), timeout=timeout_s,
        )
        latency = time.perf_counter() - started
        if r.status_code != 200:
            return RunOutput(status=f"http_{r.status_code}", answer="", latency_s=latency, cost_usd=0.0,
                             errors=[{"message": r.text[:300]}])
        data = r.json()
        usage = data.get("usage") or {}
        return RunOutput(
            status=data.get("status", "error"), answer=data.get("answer", ""), latency_s=latency,
            cost_usd=float(usage.get("cost_usd", 0)), total_tokens=int(usage.get("total_tokens", 0)),
            request_id=data.get("request_id"), artifacts=data.get("artifacts") or {}, errors=data.get("errors") or [],
        )

    async def gateway(self, request_id: str | None = None) -> ModelGatewayClient:
        s = await self.login()
        secret = self._s.model_gateway_client_secret
        if secret is None:
            raise UnauthorizedError("MODEL_GATEWAY_CLIENT_SECRET (client 'quality') is not configured")
        token = mint_call_token(
            CallClaims(client_id="quality", user_id=s.user_id, plan=s.plan,
                       request_id=request_id or f"eval-{uuid.uuid4().hex}"),
            secret.get_secret_value(),
            ttl_s=3600,
        )
        return ModelGatewayClient(self._s.model_gateway_url, token, self._http)
