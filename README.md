# Voice Agent — Outbound Shipment Follow-up Calls

An AI voice agent that places automated **outbound phone calls** to follow up on
delayed shipments. It confirms the contractor's identity, asks for a shipment
status update, captures a follow-up date (or logs a human-callback request), then
writes the full transcript, outcome, and per-call cost to PostgreSQL.

The project contains **two implementations** of the same use case so they can be
compared head-to-head:

- **Deepgram + LiveKit** (current / recommended) — self-hosted STT → LLM → TTS pipeline, ~3–4× cheaper
- **ElevenLabs Conversational AI** — fully managed platform, faster to prototype

> A detailed cost/latency/quality comparison of the two approaches lives in
> [`readme.md`](readme.md).

---

## Architecture

### Deepgram + LiveKit (current)
```
outbound_call_deepgram.py   →  dispatches a LiveKit agent job
        │
        ▼
LiveKit Cloud room  +  Twilio SIP trunk  →  Caller's phone
        │
        ▼
agent_deepgram.py  (worker)
    Deepgram Nova-3  (STT)
    Gemini 2.5 Flash (LLM)
    Deepgram Aura-2  (TTS)
        │
        ▼
PostgreSQL  →  dashboard.py (web UI on :8080)
```

### ElevenLabs (alternative)
```
elevenlabs_oubtoundcall.py  →  ElevenLabs Convai API (managed STT+LLM+TTS)
        │                          → Twilio → Caller
        ▼
webhook_server.py  →  post-call webhook (HMAC-verified)  →  PostgreSQL
        │
        ▼
elevenlabs_dashboard.py
```

---

## Tech Stack

| Layer            | Technology                          |
|------------------|-------------------------------------|
| Media server     | LiveKit Cloud                       |
| Telephony / PSTN | Twilio SIP trunk                    |
| STT              | Deepgram Nova-3                     |
| LLM              | Gemini 2.5 Flash                    |
| TTS              | Deepgram Aura-2 (`aura-2-thalia-en`)|
| Database         | PostgreSQL (`calls` table)          |
| Language         | Python 3.10+                        |

---

## Repository Layout

| File | Purpose |
|------|---------|
| `agent_deepgram.py`         | **Main agent worker.** Defines the call flow, function tools (`confirm_followup_date`, `request_human_callback`, `end_call`), cost tracking, and DB persistence. |
| `outbound_call_deepgram.py` | Triggers an outbound call by dispatching a LiveKit agent job with contractor/shipment metadata. |
| `dashboard.py`              | Zero-dependency web dashboard (port 8080) showing all calls, outcomes, costs, and expandable transcripts. |
| `deepfilter_audio.py`       | Optional DeepFilterNet noise-suppression helper for the audio track. |
| `agent.py`                  | Earlier / experimental agent implementation. |
| `outbound_call.py`          | Earlier outbound trigger. |
| `elevenlabs_oubtoundcall.py`| Triggers an outbound call via the ElevenLabs Convai API. |
| `webhook_server.py`         | FastAPI server that receives ElevenLabs post-call webhooks (HMAC-verified) and writes to PostgreSQL. |
| `elevenlabs_dashboard.py`   | Dashboard for the ElevenLabs call data. |

---

## Call Flow

The agent ("Al" from *Outamation AI*) works through a scripted flow:

1. **Confirm identity** — verify it's the right contractor; hang up politely if wrong number.
2. **Get shipment status** — ask whether the (overdue) shipment has shipped.
   - **Shipped** → ask for the ship date.
   - **Not yet** → ask for an expected date → `confirm_followup_date`.
   - **No date given** → tell them a human will follow up → `request_human_callback`.
3. **Close** — deliver a final message and hang up (`end_call`).

Every call records: transcript, shipment status, follow-up date, human-callback
flag, outcome, duration, and an itemized cost breakdown (STT / TTS / LLM / Twilio).

---

## Getting Started (Deepgram + LiveKit)

### 1. Install dependencies
```bash
pip install "livekit-agents" livekit-plugins-deepgram livekit-plugins-google \
            asyncpg dateparser python-dotenv
```

### 2. Configure environment
Create a `.env` file:
```env
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
DEEPGRAM_API_KEY=...
GOOGLE_API_KEY=...
SIP_TRUNK_ID=ST_xxxxxxxxxxxxxxx
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

### 3. Create the database table
```sql
CREATE TABLE calls (
    id              SERIAL PRIMARY KEY,
    caller_number   TEXT,
    direction       TEXT,
    purpose         TEXT,
    transcript      JSONB,
    follow_up_time  TIMESTAMP,
    birthday        DATE,
    cost            NUMERIC,
    ended_at        TIMESTAMP,
    shipment_status TEXT,
    outcome         TEXT,
    wants_human     BOOLEAN
);
```

### 4. Run
```bash
# Terminal 1 — start the agent worker (keep running)
python agent_deepgram.py dev

# Terminal 2 — trigger an outbound call
python outbound_call_deepgram.py

# Terminal 3 (optional) — view results
python dashboard.py     # → http://localhost:8080
```

To dial a different number, edit the `trigger_shipment_followup(...)` call at the
bottom of `outbound_call_deepgram.py` (`to_number`, `contractor_name`, `shipment_id`).

---

## Getting Started (ElevenLabs alternative)

```bash
pip install requests fastapi uvicorn asyncpg python-dotenv
```

Additional `.env` values:
```env
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
ELEVENLABS_PHONE_ID=...
ELEVENLABS_WEBHOOK_SECRET=wsec_xxxxxx
```

Run:
```bash
# Receive post-call webhooks
uvicorn webhook_server:app --host 0.0.0.0 --port 8000

# Trigger a call
python elevenlabs_oubtoundcall.py
```

---

## Cost (per 5-minute call)

| Component     | ElevenLabs       | Deepgram + LiveKit |
|---------------|------------------|--------------------|
| STT           | included         | $0.022             |
| LLM           | included         | $0.002             |
| TTS           | ~$0.120          | $0.025             |
| Platform fee  | ~$0.25–0.40/min  | $0                 |
| Twilio SIP    | ~$0.10           | $0.10              |
| **Total/call**| **~$0.45–0.60**  | **~$0.15**         |

At 1,000 calls/month: **ElevenLabs ~$450–600** vs **Deepgram ~$150** (~3–4× cheaper).
See [`readme.md`](readme.md) for the full latency and quality comparison.

---

## Notes

additional feature to test
1. bi llm model on to listen and another to guide the reponse. similar to meta voice approach
2. develop a background noise cancellation in house solution while keeping latency in check.
3. red data set to see how secure the model is to prompt injection.

- `.env`, `venv/`, `__pycache__/`, and `*.log` are gitignored.
- The agent removes the SIP participant on hangup to send a proper SIP BYE — otherwise the phone leg stays up after the agent disconnects.
- Costs are computed live from LiveKit metrics (token counts, TTS characters, call duration) and stored per call.
