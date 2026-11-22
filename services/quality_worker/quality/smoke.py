"""Live end-to-end smoke test against a running stack (needs real provider keys).

As the eval service account: log in -> run each enabled topic through the edge
/chat (explicit topic) -> confirm per-topic tokens & cost show up in
/usage/me -> leave feedback. Prints a table; exit code 1 on any failure.
"""
import asyncio

import httpx

from agency_platform.config.registry import topics
from quality.client import AgencyClient
from quality.settings import QualitySettings

SMOKE_PROMPTS = {
    "ev_research": "What is the battery capacity and range of the 2024 Hyundai Ioniq 5 in Germany?",
    "product_finder": "Cheapest new Sony WH-1000XM5 headphones in the US",
    "finance": "What is the 1-year volatility and max drawdown of VTI?",
}


async def run_smoke(settings: QualitySettings, http: httpx.AsyncClient) -> int:
    client = AgencyClient(settings, http)
    session = await client.login()
    auth = {"Authorization": f"Bearer {session.access_token}"}
    failures = 0
    print(f"{'topic':<16} {'status':<10} {'tokens':>8} {'cost $':>10}  note")
    for topic in topics().enabled():
        prompt = SMOKE_PROMPTS.get(topic, "hello")
        try:
            r = await http.post(
                f"{settings.edge_url}/api/v1/chat",
                json={"prompt": prompt, "topic": topic, "stream": False, "session_id": "smoke"},
                headers=auth, timeout=topics().topics[topic].run_timeout_s + 60,
            )
            data = r.json()
            usage = data.get("usage") or {}
            ok = r.status_code == 200 and data.get("status") == "success" and data.get("answer")
            note = "" if ok else str(data.get("errors") or data.get("error"))[:80]
            if ok and data.get("request_id"):
                await http.post(
                    f"{str(topics().topics[topic].service_url).rstrip('/')}/api/v1/{topic}/runs/{data['request_id']}/feedback",
                    json={"score": 1, "comment": "smoke"}, headers=auth,
                )
        except httpx.HTTPError as exc:
            ok, usage, note, data = False, {}, type(exc).__name__, {}
        failures += not ok
        print(f"{topic:<16} {'OK' if ok else 'FAIL':<10} {usage.get('total_tokens', 0):>8} "
              f"{usage.get('cost_usd', 0):>10.5f}  {note}")

    await asyncio.sleep(2)  # gateway flushes usage_daily in ~1s batches
    me = (await http.get(f"{settings.edge_url}/api/v1/usage/me", headers=auth)).json()
    by_topic = me.get("today", {}).get("by_topic", {})
    tracked = [t for t, v in by_topic.items() if v.get("tokens", 0) > 0]
    print(f"\nper-topic usage tracked today: {tracked}")
    if not tracked:
        print("FAIL: no per-topic token usage recorded")
        failures += 1
    return 1 if failures else 0
