import asyncio
import asyncpg
import json
import os
from datetime import datetime
import dateparser
from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import AgentSession, Agent, JobContext, function_tool, RunContext
from livekit.plugins import deepgram, google

load_dotenv()

COMPANY_NAME = "Outamation AI"

# ─────────────────────────────────────────────────────────
# COST CONSTANTS
# Deepgram Nova-3 STT:   $0.0043 / minute
# Deepgram Aura-2 TTS:   $0.015  / 1000 characters
# Gemini 2.5 Flash text: $0.075  / 1M input tokens, $0.30 / 1M output tokens
# Twilio SIP:            $0.02   / minute
# ─────────────────────────────────────────────────────────

STT_COST_PER_MIN      = 0.0043
TTS_COST_PER_1K_CHARS = 0.015
LLM_INPUT_PER_1M      = 0.075
LLM_OUTPUT_PER_1M     = 0.30
TWILIO_COST_PER_MIN   = 0.02


# ─────────────────────────────────────────────────────────
# AGENT
# ─────────────────────────────────────────────────────────

class ShipmentFollowupAgent(Agent):
    def __init__(
        self,
        contractor_name: str,
        shipment_id: str,
        ctx: JobContext,
        trunk_id: str = None,
    ):
        self._contractor_name = contractor_name
        self._shipment_id = shipment_id
        self._ctx = ctx
        self._trunk_id = trunk_id
        self._captured = {
            "shipment_status": None,
            "follow_up_date": None,
            "wants_human": False,
            "outcome": None,
        }
        self._disconnecting = False

        super().__init__(instructions=f"""
You are Al, a professional outbound caller for {COMPANY_NAME}.
You are calling about shipment {shipment_id}.

CALL FLOW — work through these steps naturally and in order:

STEP 1 — CONFIRM IDENTITY
Greet warmly and confirm you are speaking with {contractor_name}.
- If confirmed: continue to Step 2
- If wrong person or wrong number: say "Apologies for the confusion. I must have the wrong number. Have a great day!" then call end_call

STEP 2 — GET SHIPMENT STATUS
Ask {contractor_name} for an update on shipment {shipment_id} This shipment is past its expected ship date.
You do NOT have tracking info or delivery status — you are calling to GET that info
Your only job in this step:
- Ask whether shipment {shipment_id} has shipped yet
- If yes, ask for the ship date
- If no, ask when they expect to ship it, if they provide a date, confirm it back to them and call confirm_followup_date
if they don't provide a follow up date, if they don't provide a follow up date, then tell them that a human will follow up with them. Then call request_human_callback. 
- Do NOT invent tracking numbers, delivery statuses, or details you weren't given
Keep responses under 2 sentences.

RULES:
- Keep all responses under 2 sentences
- Never ask more than one question at a time
- Be warm, professional, and brief — never robotic
- Do NOT invent tracking numbers, delivery dates, or any details you were not given
""")

    async def _disconnect_after(self, delay: float = 4.0):
        if self._disconnecting:
            return
        self._disconnecting = True
        await asyncio.sleep(delay)
        await self._ctx.room.disconnect()

    # ── Tool 1: Capture shipment status ────────────────────
    # @function_tool
    # async def note_shipment_status(self, _context: RunContext, status: str):
    #     """
    #     Call this once the caller has shared any information about the shipment status.
    #     'status' is a brief summary of what they said (e.g. 'shipped May 20', 'not yet', 'delayed by a week').
    #     """
    #     print(f"[CAPTURED] shipment_status={status!r}")
    #     self._captured["shipment_status"] = status
    #     return ""

    # ── Tool 2: Confirm follow-up date and end call ─────────
    @function_tool
    async def confirm_followup_date(self, _context: RunContext, date: str):
        """
        Call this once a specific follow-up date has been confirmed with the caller.
        'date' is exactly what they said (e.g. 'next Friday', 'June 20th').
        """
        print(f"[CAPTURED] follow_up_date={date!r}")
        self._captured["follow_up_date"] = date
        self._captured["outcome"] = "follow_up_scheduled"
        asyncio.ensure_future(self._disconnect_after(5.0))
        parsed = dateparser.parse(date, settings={"RETURN_AS_TIMEZONE_AWARE": False})
        spoken_date = parsed.strftime("%B %d, %Y") if parsed else date
        return (
            f"Follow-up confirmed for {spoken_date}. "
            f"Say: 'Great, I've noted the follow-up for {spoken_date}. "
            f"Thanks for your time, {self._contractor_name}. Have a great day!' "
            f"Then stop speaking."
        )

    # ── Tool 3: Note human callback request and end call ───
    @function_tool
    async def request_human_callback(self, _context: RunContext):
        """
        Call this when the caller would prefer a human from our team to call them back.
        """
        print("[CAPTURED] wants_human=True")
        self._captured["wants_human"] = True
        self._captured["outcome"] = "human_callback_requested"
        asyncio.ensure_future(self._disconnect_after(5.0))
        return (
            f"Say: 'Perfect, I've noted that you'd like a human to call you back. "
            f"Thanks for your time, {self._contractor_name}. Have a great day!' "
            f"Then stop speaking."
        )

    # ── Tool 4: End call (wrong number / natural close) ────
    @function_tool
    async def end_call(self, _context: RunContext):
        """
        Call this to hang up after delivering a final message.
        """
        print("[CALL] end_call triggered")
        self._captured["outcome"] = self._captured["outcome"] or "ended"
        asyncio.ensure_future(self._disconnect_after(3.0))
        return "Call ending."


# ─────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────

async def entrypoint(ctx: JobContext):
    await ctx.connect()
    started_at = datetime.utcnow()

    metadata = ctx.job.metadata or "{}"
    try:
        meta = json.loads(metadata)
    except Exception:
        meta = {}

    direction       = meta.get("direction", "inbound")
    phone_number    = meta.get("phone_number", None)
    trunk_id        = meta.get("trunk_id", None)
    contractor_name = meta.get("contractor_name", "there")
    shipment_id     = meta.get("shipment_id", "UNKNOWN")

    print(f"[SESSION] direction={direction} contractor={contractor_name} shipment={shipment_id}")

    total_llm_input_tokens  = 0
    total_llm_output_tokens = 0
    total_tts_chars         = 0
    transcript              = []

    agent = ShipmentFollowupAgent(
        contractor_name=contractor_name,
        shipment_id=shipment_id,
        ctx=ctx,
        trunk_id=trunk_id,
    )

    session = AgentSession(
        stt=deepgram.STT(
            model="nova-3",
            language="en-US",
            smart_format=True,
            endpointing_ms=300,
        ),
        llm=google.LLM(model="gemini-2.5-flash"),
        tts=deepgram.TTS(model="aura-2-thalia-en"),
    )

    @session.on("metrics_collected")
    def on_metrics(event):
        nonlocal total_llm_input_tokens, total_llm_output_tokens, total_tts_chars
        m = event.metrics
        if hasattr(m, "prompt_tokens"):
            total_llm_input_tokens += m.prompt_tokens
        if hasattr(m, "completion_tokens"):
            total_llm_output_tokens += m.completion_tokens
        if hasattr(m, "characters_count"):
            total_tts_chars += m.characters_count

    @session.on("conversation_item_added")
    def on_item(event):
        try:
            item = event.item
            if not hasattr(item, "role"):
                return
            transcript.append({
                "role": item.role,
                "text": item.text_content,
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"[TRANSCRIPT] Could not capture: {e}")

    await session.start(room=ctx.room, agent=agent)

    if direction == "outbound" and phone_number and trunk_id:
        print(f"[OUTBOUND] Dialing {phone_number}...")
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
                sip_call_to=phone_number,
                room_name=ctx.room.name,
                participant_identity=f"phone_{phone_number}",
                participant_name=contractor_name,
                wait_until_answered=True,
            )
        )
        print("[OUTBOUND] Call answered.")
        await session.generate_reply(
            instructions=(
                f"Greet warmly. Say: 'Hi, is this {contractor_name}? "
                f"This is Al calling from {COMPANY_NAME}.'"
            )
        )

    try:
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)
    finally:
        ended_at         = datetime.utcnow()
        duration_minutes = (ended_at - started_at).total_seconds() / 60

        stt_cost    = duration_minutes * STT_COST_PER_MIN
        tts_cost    = (total_tts_chars / 1000) * TTS_COST_PER_1K_CHARS
        llm_cost    = (
            (total_llm_input_tokens  / 1_000_000) * LLM_INPUT_PER_1M
            + (total_llm_output_tokens / 1_000_000) * LLM_OUTPUT_PER_1M
        )
        twilio_cost = duration_minutes * TWILIO_COST_PER_MIN
        total_cost  = stt_cost + tts_cost + llm_cost + twilio_cost

        print(f"[COST] Duration:      {duration_minutes:.2f} min")
        print(f"[COST] STT:           ${stt_cost:.4f}")
        print(f"[COST] TTS:           ${tts_cost:.4f}  ({total_tts_chars} chars)")
        print(f"[COST] LLM:           ${llm_cost:.6f}  (in={total_llm_input_tokens} out={total_llm_output_tokens})")
        print(f"[COST] Twilio:        ${twilio_cost:.4f}")
        print(f"[COST] Total:         ${total_cost:.4f}")
        print(f"[DEBUG] outcome={agent._captured['outcome']} "
              f"status={agent._captured['shipment_status']!r} "
              f"follow_up={agent._captured['follow_up_date']!r} "
              f"wants_human={agent._captured['wants_human']} "
              f"transcript={len(transcript)} items")

        follow_up_dt = None
        if agent._captured["follow_up_date"]:
            try:
                follow_up_dt = dateparser.parse(
                    agent._captured["follow_up_date"],
                    settings={"RETURN_AS_TIMEZONE_AWARE": False, "TO_TIMEZONE": "UTC"},
                )
            except Exception as e:
                print(f"[FOLLOWUP PARSE ERROR] {e}")

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            print("[DB WARNING] DATABASE_URL is not set — call record will not be saved.")
        else:
            conn = await asyncpg.connect(db_url)
            try:
                await conn.execute(
                    """
                    INSERT INTO calls
                        (caller_number, direction, purpose, transcript,
                         follow_up_time, birthday, cost, ended_at,
                         shipment_status, outcome, wants_human)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), $8, $9, $10)
                    """,
                    phone_number or ctx.room.name,
                    direction,
                    shipment_id,
                    json.dumps(transcript),
                    follow_up_dt,
                    None,
                    total_cost,
                    agent._captured["shipment_status"],
                    agent._captured["outcome"],
                    agent._captured["wants_human"],
                )
                print(f"[DB] Call saved. cost=${total_cost:.4f}")
            except Exception as e:
                print(f"[DB ERROR] {e}")
            finally:
                await conn.close()


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="shipment-followup-agent",
    ))
