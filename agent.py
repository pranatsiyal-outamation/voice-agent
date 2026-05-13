import asyncio
import asyncpg
import json
import os
from datetime import datetime, date
import dateparser
from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import AgentSession, Agent, JobContext, function_tool, RunContext
from livekit.plugins import google

load_dotenv()

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────

HUMAN_REP_PHONE = "+17202275031"
HUMAN_REP_NAME  = "Al from the team"
COMPANY_NAME    = "Outamation AI"
SECRET_DATE     = date(1999, 1, 10)

# ─────────────────────────────────────────────────────────
# AGENT CLASS
# ─────────────────────────────────────────────────────────

class Assistant(Agent):
    def __init__(
        self,
        direction: str = "inbound",
        purpose: str = "",
        captured_fields: dict = None,
        ctx: JobContext = None,
        trunk_id: str = None,
    ):
        if captured_fields is None:
            captured_fields = {}

        self._captured_fields = captured_fields
        self._ctx = ctx
        self._trunk_id = trunk_id
        self._verify_attempts = 0
        self._verified = False
        self._session = None

        if direction == "outbound":
            instructions = f"""
                You are Aria, a professional AI assistant calling on behalf of {COMPANY_NAME}.
                Your goal for this call: {purpose}

                Your personality:
                - Warm, confident, and concise — never robotic
                - You respect the person's time and get to the point quickly
                - You listen actively and adapt to their tone

                Call flow:
                1. Greet them briefly, introduce yourself as Aria from {COMPANY_NAME}
                2. Before anything else, tell them you need to verify their identity for security
                3. Ask for their secret date (do not hint at the format or the value)
                4. Call verify_secret_date with exactly what they say
                5. If verification fails, follow the instruction returned by the tool exactly — either ask once more or end the call
                6. Only after successful verification: state the purpose of your call in one clear sentence
                7. If they want to speak with a human, use the transfer_to_human tool
                8. If they want a callback later, use the note_followup_time tool and confirm the time back to them
                9. If they mention their birthday, use the note_birthday tool to record it
                10. If they're not interested, thank them politely and end the call 

                Rules:
                - Never be pushy or repeat yourself more than once
                - Keep responses under 3 sentences unless they ask a detailed question
                - If you don't know something, say "Let me get a specialist on the line for you"
                  and immediately use the transfer_to_human tool
            """
        else:
            instructions = f"""
                You are Aria, the AI receptionist for {COMPANY_NAME}.
                Answer the caller's questions clearly and professionally.
                If they want to speak with a human or the situation requires expertise,
                use the transfer_to_human tool.
                If they mention scheduling or a follow-up time,
                use the note_followup_time tool to record it.
                If they mention their birthday, use the note_birthday tool to record it.
            """

        super().__init__(instructions=instructions)

    # ── Tool 1: Note a follow-up time ──────────────────────
    @function_tool
    async def note_followup_time(self, context: RunContext, time: str):
        """
        Call this when the person specifies a callback time.
        'time' should be exactly what they said, e.g. 'tomorrow at 3pm'.
        """
        print(f"[FOLLOWUP NOTED] {time}")
        self._captured_fields["follow_up_time"] = time
        return f"Perfect, I've noted the follow-up for {time} and we'll be in touch then."

    # ── Tool 2: Transfer to human rep ──────────────────────
    @function_tool
    async def transfer_to_human(self, context: RunContext, reason: str):
        """
        Call this when the person wants to speak with a human,
        or when their question requires a specialist.
        'reason' is a brief note about why the transfer is needed.
        """
        trunk = self._trunk_id or os.getenv("OUTBOUND_TRUNK_ID", "ST_rsf8HGUzUuFx")
        print(f"[TRANSFER] Initiating. trunk={trunk} rep={HUMAN_REP_PHONE} reason={reason}")

        self._captured_fields["transferred"] = True
        self._captured_fields["transfer_reason"] = reason

        try:
            await self._ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=trunk,
                    sip_call_to=HUMAN_REP_PHONE,
                    room_name=self._ctx.room.name,
                    participant_identity="human_rep",
                    participant_name=HUMAN_REP_NAME,
                    wait_until_answered=False,
                )
            )
            print(f"[TRANSFER] Dial initiated. Rep's phone is ringing.")

            async def _brief_supervisor():
                # Poll until human_rep appears as a remote participant (up to 30 s)
                for _ in range(30):
                    if any(
                        p.identity == "human_rep"
                        for p in self._ctx.room.remote_participants.values()
                    ):
                        break
                    await asyncio.sleep(1)
                else:
                    print("[TRANSFER] Human rep did not join within 30 s — skipping briefing.")
                    return

                await asyncio.sleep(1)  # brief buffer for audio to stabilise
                print("[TRANSFER] Human rep joined — delivering briefing.")
                if self._session:
                    await self._session.say(
                        f"Hi {HUMAN_REP_NAME}, this is Aria. "
                        f"I'm passing you this call — {reason}. "
                        f"I'll step back now and let you take it from here.",
                        allow_interruptions=False,
                    )
                    print("[TRANSFER] Briefing delivered — agent going silent.")

            asyncio.create_task(_brief_supervisor())

            return (
                f"I'm connecting you with {HUMAN_REP_NAME} right now — please hold for just a moment. "
                f"They'll be with you shortly!"
            )

        except Exception as e:
            print(f"[TRANSFER ERROR] {type(e).__name__}: {e}")
            return (
                "I'm sorry, I wasn't able to reach them right now. "
                "Would you like me to schedule a callback instead?"
            )

    # ── Tool 3: Verify caller identity ─────────────────────
    @function_tool
    async def verify_secret_date(self, context: RunContext, date_given: str):
        """
        Call this to verify the caller's identity using their secret date.
        'date_given' is exactly what the caller said, e.g. 'January 10th 1999'.
        """
        parsed = dateparser.parse(date_given, settings={"RETURN_AS_TIMEZONE_AWARE": False})

        if parsed and parsed.date() == SECRET_DATE:
            self._verified = True
            self._captured_fields["verified"] = True
            print(f"[VERIFY] Identity confirmed.")
            return "Identity verified. Proceed with the call purpose."

        self._verify_attempts += 1
        print(f"[VERIFY] Failed attempt {self._verify_attempts}. Given: {date_given!r} → parsed: {parsed}")

        if self._verify_attempts >= 2:
            self._captured_fields["verified"] = False

            async def _end_call():
                await asyncio.sleep(5)
                await self._ctx.room.disconnect()

            asyncio.create_task(_end_call())
            return (
                "Verification failed twice. Tell the caller their identity could not be confirmed "
                "and the call must end. Say a polite goodbye, then stop speaking."
            )

        return "Incorrect date. Ask the caller to try one more time — they have one attempt remaining."

    # ── Tool 4: Note birthday ───────────────────────────────
    @function_tool
    async def note_birthday(self, context: RunContext, birthday: str):
        """
        Call this when the person mentions their birthday.
        'birthday' should be exactly what they said,
        e.g. 'March 5th 1990' or '05/03/1990' or 'the 3rd of March'.
        """
        print(f"[BIRTHDAY NOTED] {birthday}")
        self._captured_fields["birthday"] = birthday
        return f"Got it, I've noted your birthday as {birthday}."


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

    direction    = meta.get("direction", "inbound")
    purpose      = meta.get("purpose", "")
    phone_number = meta.get("phone_number", None)
    trunk_id     = meta.get("trunk_id", None)

    print(f"[SESSION] direction={direction} purpose={purpose}")

    captured_fields = {
        "follow_up_time": None,
        "transferred": False,
        "transfer_reason": None,
        "birthday": None,          # new field
    }
    transcript = []

    agent = Assistant(
        direction=direction,
        purpose=purpose,
        captured_fields=captured_fields,
        ctx=ctx,
        trunk_id=trunk_id,
    )

    session = AgentSession(
        llm=google.beta.realtime.RealtimeModel(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Puck",
        )
    )
    agent._session = session

    @session.on("conversation_item_added")
    def on_item(event):
        try:
            item = event.item
            # Skip internal LiveKit events that have no role (e.g. AgentHandoff)
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
                participant_name=f"Phone {phone_number}",
                wait_until_answered=True,
            )
        )
        print(f"[OUTBOUND] Call answered.")

    try:
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)
    finally:
        # Cost calculation
        ended_at = datetime.utcnow()
        duration_minutes = (ended_at - started_at).total_seconds() / 60
        estimated_cost = duration_minutes * 0.45
        print(f"[COST] Duration: {duration_minutes:.2f} min | Estimated cost: ${estimated_cost:.4f}")
        print(f"[DEBUG] transferred={captured_fields['transferred']} "
              f"follow_up={captured_fields['follow_up_time']} "
              f"birthday={captured_fields['birthday']} "
              f"transcript={len(transcript)} items")

        # Parse follow-up time string → UTC datetime
        follow_up_dt = None
        if captured_fields["follow_up_time"]:
            try:
                follow_up_dt = dateparser.parse(
                    captured_fields["follow_up_time"],
                    settings={"RETURN_AS_TIMEZONE_AWARE": False, "TO_TIMEZONE": "UTC"}
                )
            except Exception as e:
                print(f"[FOLLOWUP PARSE ERROR] {e}")

        # Parse birthday string → Python date object
        # dateparser handles natural language like "March 5th 1990" → datetime
        # .date() strips the time component since we only need the date
        birthday_dt = None
        if captured_fields["birthday"]:
            try:
                parsed = dateparser.parse(
                    captured_fields["birthday"],
                    settings={"RETURN_AS_TIMEZONE_AWARE": False}
                )
                birthday_dt = parsed.date() if parsed else None
                print(f"[BIRTHDAY PARSED] {birthday_dt}")
            except Exception as e:
                print(f"[BIRTHDAY PARSE ERROR] {e}")

        # Save to DB
        try:
            conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
            await conn.execute(
                """
                INSERT INTO calls
                    (caller_number, direction, purpose, transcript,
                     follow_up_time, birthday, ended_at)
                VALUES
                    ($1, $2, $3, $4, $5, $6, NOW())
                """,
                phone_number or ctx.room.name,
                direction,
                purpose,
                json.dumps(transcript),
                follow_up_dt,
                birthday_dt,          # $6 — None if not captured, NULL in DB
            )
            await conn.close()
            print(f"[DB] Call saved. birthday={birthday_dt}")
        except Exception as e:
            print(f"[DB ERROR] {e}")


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="outbound-agent"
    ))
