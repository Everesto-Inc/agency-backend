# Glossary

| Term | Meaning |
|---|---|
| **Topic** | One assistant (EV Helper, Product Finder, Finance Analyst) = one service |
| **Run** | One question answered by a topic (`agent_runs`, identified by `request_id`) |
| **Session** | Conversation (`session_id`): runs sharing memory for follow-ups |
| **Edge gateway** | The public API service (auth, `/chat`, usage, dashboards, admin) |
| **Model gateway** | Internal service every LLM call goes through; the only holder of the Gemini key |
| **Tier** | Model class: low / mid / high — chosen per task, capped by the plan |
| **Task** | What an LLM call is for (`intent_parsing`, `synthesis`, …) → mapped to a tier in `models.yaml` |
| **Plan** | Subscription (free / pro / internal): topics, max tier, daily token + cost limits |
| **Call token** | Short-lived token a topic sends to the model gateway (user, plan, request id) — attribution |
| **Canary** | A new model getting a small % of a tier's calls, compared before promoting it |
| **Grounding** | Every number in an answer comes from a verified source (crawled page, tool), not model memory |
| **Offline eval** | Dataset of questions + checks + LLM judge, run against the live services |
| **Gate** | Offline eval compared to the committed baseline; a regression fails the PR |
| **Drift** | Quality/cost signals of the last day vs the previous week |
| **PII redaction** | Personal data replaced (`[EMAIL]`, `[CARD]` …) before LLM calls and storage |
| **Space** (Backstage) | DevOps space `orchid`: the team + its apps, CI and deployments |
