# Chat memory

How the assistants remember a conversation — what they read before answering, where it is stored, and for how long.

!!! tip "In one sentence"
    Before every answer, the assistant gets **a short summary of the older part of the chat + the last 4 messages
    word for word + your new question** — the same memory for every assistant in that chat.

## The layers at a glance

| | Layer | What it holds | Where | Kept | Status |
|---|---|---|---|---|---|
| ① | **Recent messages** | the last messages, word for word (assistants read the last **4**) | Redis `sess:<you>:<chat>` (last 20) · durable copy in MySQL `agent_runs` | Redis 24 h · MySQL text 30 days | ✅ live |
| ② | **Chat summary** | ≤ 8 bullets: your goals, facts you gave, key results, open questions | MySQL `conversations.summary` | as long as the chat | ✅ live |
| ③ | **Project** | project instructions + documents | — | — | 🔜 with projects |
| ④ | **About you** | a few facts you confirmed (country, currency…), for every chat | — | until you delete them | 🔜 planned |

## The big picture

```mermaid
flowchart LR
  you([🧑 You<br/>new question]):::user --> edge[Edge gateway<br/>routes to an assistant]:::gateway
  edge --> runner{{Runner<br/>builds the memory}}:::gateway

  subgraph mem[🧠 Memory of THIS chat]
    direction TB
    redis[(① Redis<br/>last 20 messages<br/>24 h)]:::fast
    mysql[(① MySQL agent_runs<br/>every Q&A, 30 days<br/>durable copy)]:::store
    sum[(② MySQL conversations.summary<br/>≤ 8 bullets)]:::store
  end

  runner -- "read (fast)" --> redis
  runner -. "Redis empty? reload" .-> mysql
  runner -- read --> sum
  runner --> ctx["📦 What the assistant gets<br/>summary + last 4 messages + question"]:::ctx
  ctx --> ev[⚡ EV Helper]:::ev
  ctx --> pf[🛒 Product Finder]:::pf
  ctx --> fin[📈 Finance]:::fin
  ev & pf & fin --> llm[🤖 Gemini<br/>via model gateway]:::llm
  llm --> ans([💬 Answer streams to you]):::user

  classDef user fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:2px
  classDef gateway fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px
  classDef fast fill:#fee2e2,stroke:#dc2626,color:#7f1d1d,stroke-width:2px
  classDef store fill:#e2e8f0,stroke:#475569,color:#0f172a,stroke-width:2px
  classDef ctx fill:#ccfbf1,stroke:#0d9488,color:#134e4a,stroke-width:2px
  classDef ev fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
  classDef pf fill:#fce7f3,stroke:#db2777,color:#831843,stroke-width:2px
  classDef fin fill:#ede9fe,stroke:#7c3aed,color:#4c1d95,stroke-width:2px
  classDef llm fill:#ffedd5,stroke:#ea580c,color:#7c2d12,stroke-width:2px
```

## One question, step by step

```mermaid
sequenceDiagram
  autonumber
  participant U as 🧑 You
  participant R as Runner
  participant Re as Redis (fast)
  participant DB as MySQL (durable)
  participant A as Assistant
  participant M as 🤖 Gemini

  rect rgba(59, 130, 246, 0.14)
    Note over R,DB: 1 · Build the memory
    U->>R: "And in winter?"
    R->>Re: last messages of this chat
    alt Redis has them (chat active in the last 24 h)
      Re-->>R: messages
    else Redis empty (older chat, restart)
      R->>DB: last answered Q&A of this chat
      DB-->>R: messages (personal data masked)
      R->>Re: put them back (fast next time)
    end
    R->>DB: chat summary
  end

  rect rgba(34, 197, 94, 0.14)
    Note over R,M: 2 · Answer
    R->>A: summary + last 4 messages + question
    A->>M: understand → fetch facts → write the answer
    M-->>U: answer streams in
  end

  rect rgba(245, 158, 11, 0.14)
    Note over R,DB: 3 · Remember (after the answer)
    R->>Re: + your question + the answer
    R->>DB: Q&A stored (for the history + reloads)
  end

  rect rgba(139, 92, 246, 0.14)
    Note over R,M: 4 · In the background — about every 3 questions
    R->>M: cheap model: "fold these older messages into the summary"
    M-->>R: updated summary (≤ 8 bullets)
    R->>DB: save the summary
  end
```

!!! note "You never wait for step 4"
    The summary is written **after** your answer has finished streaming. If it fails (gateway busy, …), nothing
    breaks — it is simply tried again with the next questions.

## Reopening an old chat (after 24 hours)

```mermaid
flowchart LR
  open([🧑 You open a chat<br/>from yesterday]):::user --> q([ask a follow-up]):::user
  q --> check{Redis has<br/>this chat?}:::gateway
  check -- "yes (< 24 h)" --> use[use it]:::fast
  check -- "no (expired)" --> reload[(reload last Q&A<br/>from MySQL)]:::store
  reload --> cache[put back into Redis]:::fast
  use & cache --> answer([assistant answers<br/>WITH the context]):::ctx

  classDef user fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:2px
  classDef gateway fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px
  classDef fast fill:#fee2e2,stroke:#dc2626,color:#7f1d1d,stroke-width:2px
  classDef store fill:#e2e8f0,stroke:#475569,color:#0f172a,stroke-width:2px
  classDef ctx fill:#ccfbf1,stroke:#0d9488,color:#134e4a,stroke-width:2px
```

Works for as long as the chat's text exists — **30 days** (`config/privacy.yaml` → `retention.run_text_days`).

## How the summary grows

The assistants read the **last 4 messages** word for word. Everything older is folded into the summary — in
batches of 6 messages, so the cheap model runs about **once every 3 questions**, not every time.

```mermaid
flowchart LR
  old["🗂️ <b>Older messages</b><br/>Q1 · A1 · Q2 · A2 · Q3 · A3"]:::old
  recent["🟢 <b>Last 4 messages</b><br/>Q4 · A4 · Q5 · A5"]:::recent
  new(["🧑 <b>New question</b><br/>Q6"]):::user
  old == "cheap model folds them in<br/>≈ every 3 questions" ==> sum[("② <b>Summary</b><br/>• comparing Model Y vs Ioniq 5<br/>• user in Germany, 20,000 km/yr<br/>• Model Y ≈ €640/yr electricity")]:::store
  sum --> ctx["📦 <b>What the assistant gets</b><br/>summary + Q4 A4 Q5 A5 + Q6"]:::ctx
  recent -- "word for word" --> ctx
  new --> ctx

  classDef old fill:#ede9fe,stroke:#7c3aed,color:#4c1d95,stroke-width:2px
  classDef recent fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
  classDef user fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:2px
  classDef store fill:#e2e8f0,stroke:#475569,color:#0f172a,stroke-width:2px
  classDef ctx fill:#ccfbf1,stroke:#0d9488,color:#134e4a,stroke-width:2px
```

**Why:** a 50-message chat costs about the same per question as a 5-message one, and the assistant still knows
what was said 40 messages ago.

## One memory per chat — shared by all assistants

```mermaid
flowchart LR
  q1(["🧑 Model Y range<br/>in Germany?"]):::user --> ev["⚡ <b>EV Helper</b><br/>answers"]:::ev
  ev --> q2(["🧑 Is Tesla stock<br/>expensive?"]):::user --> fin["📈 <b>Finance</b><br/>knows you asked<br/>about the Model Y"]:::fin
  fin --> q3(["🧑 Cheapest Model Y<br/>wallbox?"]):::user --> pf["🛒 <b>Product Finder</b><br/>knows: Germany,<br/>Model Y"]:::pf
  mem[("🧠 <b>One memory for this chat</b><br/>sess:&lt;you&gt;:&lt;chat&gt;<br/>each message notes which assistant wrote it")]:::store
  ev -.-> mem
  fin -.-> mem
  pf -.-> mem

  classDef user fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:2px
  classDef ev fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
  classDef pf fill:#fce7f3,stroke:#db2777,color:#831843,stroke-width:2px
  classDef fin fill:#ede9fe,stroke:#7c3aed,color:#4c1d95,stroke-width:2px
  classDef store fill:#e2e8f0,stroke:#475569,color:#0f172a,stroke-width:2px
```

A **different chat** has its own, separate memory. Memory never crosses users — every key starts with your user id.

## What is stored where, and for how long

| Data | Where | Personal data | Kept | Deleted by |
|---|---|---|---|---|
| Last 20 messages (fast copy) | Redis `orchid-agency-backend:sess:<you>:<chat>` | as you typed them | 24 h after the last message | expiry · *Delete chat* · account deletion |
| Every question + answer | MySQL `agent_runs` | **masked** (emails, phones, IBAN, cards…) | text 30 days, then emptied (tokens/cost stay for billing) | retention job · *Delete chat* · account deletion |
| Chat title, pinned, summary | MySQL `conversations` | title masked; summary from masked text | as long as the chat | *Delete chat* · account deletion |

## Knobs you can turn

| What | Today | Where |
|---|---|---|
| Messages read word for word | 4 | `config/memory.yaml` → `recent` |
| Fold into the summary after | 6 older messages (≈ 3 questions) | `config/memory.yaml` → `fold_at_least` |
| Fast copy size / lifetime | 20 messages / 24 h | `config/memory.yaml` → `max_turns`, `ttl_hours` |
| How the summary is written | ≤ 8 bullets, < 150 words | `prompts/memory.yaml` (versioned, editable) |
| Model for the summary | low tier | `config/models.yaml` → `tasks.summarization` |
| How long text is kept | 30 days | `config/privacy.yaml` → `retention.run_text_days` |

**Admin → Settings → Chat memory** changes the `config/memory.yaml` values live (≈ 10 s), with history and rollback.

## FAQ

??? question "Why does it not remember what I said in another chat?"
    Each chat is separate on purpose (like starting a new conversation). Facts that should follow you everywhere —
    your country, currency — come with layer ④ *About you* (planned), which you will see and control on your
    Account page.

??? question "I deleted a chat — is it really gone?"
    Yes: the Redis memory, the questions and answers, the title and the summary are removed. Only tokens and cost
    stay (anonymous numbers for usage and billing).

??? question "Can the assistant see other users' chats?"
    No. Every memory key and every database query is scoped to your user id.

??? question "Does the assistant remember a picture I sent?"
    Yes — as **text**. When you send a picture, a cheap vision model writes one line about it (e.g. *"white Tesla
    Model Y, German plates"*). That line goes into the question and the memory, so follow-ups know what the picture
    showed without paying for the picture again. The picture itself stays in storage and is shown in your chat
    history; it is deleted with the chat.

??? question "Does memory make answers slower?"
    No. Reading memory is one Redis lookup (plus one MySQL read when it was expired). The summary is written in
    the background after your answer.
