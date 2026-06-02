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
# STATE MACHINE
# ─────────────────────────────────────────────────────────

#  START
#    ↓ (unconditional)
#  CONFIRM_IDENTITY
#    ↓ identity_confirmed      →  SHIPMENT_STATUS
#    ↓ wrong_person            →  POLITE_HANGUP       [terminal]
#  SHIPMENT_STATUS
#    ↓ status_obtained         →  OPTIONS
#  OPTIONS
#    ↓ schedule_followup       →  SCHEDULE_FOLLOWUP   [terminal]
#    ↓ request_human_callback  →  HUMAN_CALLBACK      [terminal]

def _build_instructions(state: str, contractor_name: str, shipment_id: str) -> str:
    if state == "CONFIRM_IDENTITY":
        return (
            f"You are Al, calling on behalf of {COMPANY_NAME} to follow up on a shipment.\n"
            f"Your only job in this step:\n"
            f"- Confirm you are speaking with {contractor_name}\n"
            f"- Wait for their confirmation\n"
            f"If they confirm it's them → call the identity_confirmed tool\n"
            f"If they say it's not them or wrong number → call the wrong_person tool\n"
            f"Be warm and brief. One sentence is enough."
        )
    if state == "SHIPMENT_STATUS":
        return (
            f"You are Al from {COMPANY_NAME}. The caller is confirmed to be {contractor_name}.\n"
            f"Shipment ID: {shipment_id}\n"
            f"Your only job in this step:\n"
            f"- Ask about the shipment status\n"
            f"- Listen for their answer (shipped, not shipped, delayed, etc.)\n"
            f"- Briefly acknowledge what they said\n"
            f"- Do NOT pressure them or repeat the question\n"
            f"Once they have shared the status → call the status_obtained tool.\n"
            f"Keep responses under 2 sentences. Be professional and brief."
        )
    if state == "OPTIONS":
        return (
            f"You are Al from {COMPANY_NAME}. The caller is confirmed to be {contractor_name}.\n"
            f"CONTEXT (do not invent details beyond this):\n"
            f"- Shipment ID: {shipment_id}\n"
            f"- This shipment is past its expected ship date\n"
            f"- You do NOT have tracking info — you are calling to GET that info\n"
            f"Your only job in this step:\n"
            f"- Ask whether shipment {shipment_id} has shipped yet\n"
            f"- If yes, ask for the ship date\n"
            f"- If no, ask when they expect to ship; if they can give a date, call the schedule_followup tool\n"
            f"- If they cannot give a date or prefer a person, offer a human follow-up and call the request_human_callback tool\n"
            f"- Do NOT invent tracking numbers or details you weren't given\n"
            f"Tools available in this step: schedule_followup, request_human_callback\n"
            f"Keep responses under 2 sentences."
        )
    if state == "SCHEDULE_FOLLOWUP":
        return (
            f"The caller has agreed to schedule a specific follow-up date.\n"
            f"Your only job in this step:\n"
            f"- Ask for a specific date (e.g., 'next Tuesday', 'May 30th', 'June 20th 2026')\n"
            f"- Listen carefully for the date they give\n"
            f"- Repeat the date back to them to confirm\n"
            f"- Once you have a clear confirmed date → call the confirm_followup_date tool with that date\n"
            f"Do not move forward until you have a clear date."
        )
    if state == "HUMAN_CALLBACK":
        return (
            f"The caller has requested a human callback.\n"
            f"Say exactly: 'Perfect, I've noted that you'd like a human to call you back. "
            f"Thanks for your time, {contractor_name}. Have a great day!'\n"
            f"Then call the end_call tool. Do not ask anything else."
        )
    if state == "POLITE_HANGUP":
        return (
            f"You have reached the wrong person.\n"
            f"Say exactly: 'Apologies for the confusion. I must have the wrong number. Have a great day!'\n"
            f"Then call the end_call tool. Do not say anything else."
        )
    return f"You are Al from {COMPANY_NAME}."


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
        self._state = "CONFIRM_IDENTITY"
        self._session = None
        self._captured = {
            "shipment_status": None,
            "follow_up_date": None,
            "wants_human": False,
            "outcome": None,
        }
        super().__init__(
            instructions=_build_instructions("CONFIRM_IDENTITY", contractor_name, shipment_id)
        )

    async def _go_to(self, new_state: str):
        """Transition to new_state: update instructions then drive the first reply."""
        self._state = new_state
        new_instr = _build_instructions(new_state, self._contractor_name, self._shipment_id)
        self.instructions = new_instr
        if self._session is not None:
            # Wait for the tool-return speech to finish before generating the next turn.
            await asyncio.sleep(1.0)
            await self._session.generate_reply(instructions=new_instr)

    async def _disconnect_after(self, delay: float = 4.0):
        await asyncio.sleep(delay)
        await self._ctx.room.disconnect()

    # ── Tool 1 ──────────────────────────────────────────────
    @function_tool
    async def identity_confirmed(self, _context: RunContext):
        """Call this when the caller confirms they are the correct person (e.g. 'yes', 'speaking')."""
        print("[WORKFLOW] identity_confirmed → SHIPMENT_STATUS")
        self._captured["outcome"] = "identity_confirmed"
        asyncio.ensure_future(self._go_to("SHIPMENT_STATUS"))
        return ""

    # ── Tool 2 ──────────────────────────────────────────────
    @function_tool
    async def wrong_person(self, context: RunContext):
        """Call this when the caller says this is the wrong person or wrong number."""
        print("[WORKFLOW] wrong_person → POLITE_HANGUP")
        self._captured["outcome"] = "wrong_person"
        asyncio.ensure_future(self._go_to("POLITE_HANGUP"))
        return ""

    # ── Tool 3 ──────────────────────────────────────────────
    @function_tool
    async def status_obtained(self, context: RunContext, status: str):
        """
        Call this once the caller has shared information about the shipment status.
        'status' is a brief summary of what they said (e.g., 'shipped May 20', 'not yet', 'delayed').
        """
        print(f"[WORKFLOW] status_obtained: {status!r} → OPTIONS")
        self._captured["shipment_status"] = status
        asyncio.ensure_future(self._go_to("OPTIONS"))
        return ""

    # ── Tool 4 ──────────────────────────────────────────────
    @function_tool
    async def schedule_followup(self, context: RunContext):
        """Call this when the caller has chosen to schedule a specific follow-up date."""
        print("[WORKFLOW] schedule_followup → SCHEDULE_FOLLOWUP")
        asyncio.ensure_future(self._go_to("SCHEDULE_FOLLOWUP"))
        return ""

    # ── Tool 5 ──────────────────────────────────────────────
    @function_tool
    async def confirm_followup_date(self, context: RunContext, date: str):
        """
        Call this once a specific follow-up date has been confirmed with the caller.
        'date' is exactly what they said (e.g., 'next Friday', 'June 20th').
        """
        print(f"[WORKFLOW] confirm_followup_date: {date!r} — ending call")
        self._captured["follow_up_date"] = date
        self._captured["outcome"] = "follow_up_scheduled"
        # Drive an explicit farewell before disconnecting.
        async def _farewell_and_disconnect():
            if self._session is not None:
                await self._session.generate_reply(
                    instructions=(
                        f"The follow-up has been confirmed for {date}. "
                        f"Say exactly: 'Great, I've noted the follow-up for {date}. "
                        f"Thanks for your time, {self._contractor_name}. Have a great day!' "
                        f"Then stop speaking."
                    )
                )
            await self._disconnect_after(4.0)
        asyncio.ensure_future(_farewell_and_disconnect())
        return ""

    # ── Tool 6 ──────────────────────────────────────────────
    @function_tool
    async def request_human_callback(self, context: RunContext):
        """Call this when the caller wants a human to call them back."""
        print("[WORKFLOW] request_human_callback → HUMAN_CALLBACK")
        self._captured["wants_human"] = True
        self._captured["outcome"] = "human_callback_requested"
        asyncio.ensure_future(self._go_to("HUMAN_CALLBACK"))
        return ""

    # ── Tool 7 ──────────────────────────────────────────────
    @function_tool
    async def end_call(self, context: RunContext):
        """Call this after delivering the final message in a terminal state to hang up."""
        print(f"[WORKFLOW] end_call — state={self._state}")
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
    agent._session = session

    @session.on("metrics_collected")
    def on_metrics(event):
        nonlocal total_llm_input_tokens, total_llm_output_tokens, total_tts_chars
        m = event.metrics
        if hasattr(m, "prompt_tokens"):
            total_llm_input_tokens  += m.prompt_tokens
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
                parsed = dateparser.parse(
                    agent._captured["follow_up_date"],
                    settings={"RETURN_AS_TIMEZONE_AWARE": False, "TO_TIMEZONE": "UTC"},
                )
                follow_up_dt = parsed
            except Exception as e:
                print(f"[FOLLOWUP PARSE ERROR] {e}")

        try:
            conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
            await conn.execute(
                """
                INSERT INTO calls
                    (caller_number, direction, purpose, transcript,
                     follow_up_time, birthday, cost, ended_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                phone_number or ctx.room.name,
                direction,
                shipment_id,
                json.dumps(transcript),
                follow_up_dt,
                None,
                total_cost,
            )
            await conn.close()
            print(f"[DB] Call saved. cost=${total_cost:.4f}")
        except Exception as e:
            print(f"[DB ERROR] {e}")


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="shipment-followup-agent",
    ))
