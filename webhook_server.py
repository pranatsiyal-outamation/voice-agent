"""
ElevenLabs Post-Call Webhook Server (with HMAC verification)
=============================================================
Receives webhooks from ElevenLabs after a call ends.
Verifies the request came from ElevenLabs via HMAC signature.
Writes structured call data to PostgreSQL.

Setup:
    pip install fastapi uvicorn asyncpg python-dotenv
    
.env needs:
    DATABASE_URL=postgresql://...
    ELEVENLABS_WEBHOOK_SECRET=wsec_xxxxxx
    
Run:
    uvicorn webhook_server:app --host 0.0.0.0 --port 8000
"""
import os
import json
import hmac
import hashlib
import time
from fastapi import FastAPI, Request, HTTPException, Header
import asyncpg
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# How old a request can be (in seconds) before we reject it as a replay attack
MAX_AGE_SECONDS = 30 * 60   # 30 minutes


# ─────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "service": "elevenlabs-webhook"}


# ─────────────────────────────────────────────────────────
# SIGNATURE VERIFICATION
# ─────────────────────────────────────────────────────────
def verify_signature(signature_header: str, raw_body: bytes, secret: str) -> bool:
    """
    Verifies the ElevenLabs HMAC signature.
    
    Signature header format: "t=1716826800,v0=abc123..."
    We extract the timestamp and the v0 signature, then independently
    compute HMAC-SHA256(secret, "t.body") and compare.
    
    Returns True if the signature is valid AND not expired.
    """
    if not signature_header:
        print("[VERIFY] No signature header on request")
        return False

    # Parse "t=...,v0=..." into a dict
    try:
        parts = dict(item.split("=", 1) for item in signature_header.split(","))
        timestamp = parts.get("t")
        received_sig = parts.get("v0")
    except Exception as e:
        print(f"[VERIFY] Could not parse signature header: {e}")
        return False

    if not timestamp or not received_sig:
        print("[VERIFY] Missing timestamp or signature in header")
        return False

    # Reject very old requests — protects against replay attacks
    try:
        request_age = int(time.time()) - int(timestamp)
        if request_age > MAX_AGE_SECONDS:
            print(f"[VERIFY] Request too old: {request_age}s")
            return False
    except ValueError:
        print(f"[VERIFY] Bad timestamp value: {timestamp}")
        return False

    # Recompute the signature on our side using the same recipe:
    # HMAC-SHA256(secret, "{timestamp}.{raw_body}")
    signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected_sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=signed_payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # hmac.compare_digest is a timing-safe comparison
    # (prevents attackers from learning the secret by measuring response time)
    is_valid = hmac.compare_digest(received_sig, expected_sig)
    if not is_valid:
        print(f"[VERIFY] Signature mismatch")
    return is_valid


# ─────────────────────────────────────────────────────────
# MAIN WEBHOOK ENDPOINT
# ─────────────────────────────────────────────────────────
@app.post("/elevenlabs-webhook")
async def post_call(
    req: Request,
    elevenlabs_signature: str | None = Header(default=None, alias="ElevenLabs-Signature"),
):
    """
    Fires when ElevenLabs finishes a call.
    Verifies signature, parses the payload, saves to PostgreSQL.
    """
    # 1. Read raw body BEFORE parsing JSON (signing operates on raw bytes)
    raw_body = await req.body()

    # 2. Verify the signature
    secret = os.getenv("ELEVENLABS_WEBHOOK_SECRET")
    if not secret:
        print("[WEBHOOK] FATAL: ELEVENLABS_WEBHOOK_SECRET not set in .env")
        raise HTTPException(status_code=500, detail="Server misconfigured")

    if not verify_signature(elevenlabs_signature, raw_body, secret):
        print("[WEBHOOK] Rejected: invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    print(f"[WEBHOOK] Signature OK")

    # 3. NOW parse the JSON — we trust it
    payload = json.loads(raw_body)
    print(f"[WEBHOOK] Event type: {payload.get('type', 'unknown')}")

    # 4. Extract fields
    data = payload.get("data", {})
    conversation_id = data.get("conversation_id")
    transcript      = data.get("transcript", [])

    metadata     = data.get("metadata", {})
    duration     = metadata.get("call_duration_secs", 0)
    phone_call   = metadata.get("phone_call", {})
    phone_number = phone_call.get("external_number", "unknown")

    dyn_vars        = data.get("conversation_initiation_client_data", {}).get("dynamic_variables", {})
    contractor_name = dyn_vars.get("contractor_name")
    shipment_id     = dyn_vars.get("shipment_id")

    analysis  = data.get("analysis", {})
    collected = analysis.get("data_collection_results", {})

    def extract(field_name, default=None):
        field = collected.get(field_name, {})
        if isinstance(field, dict):
            return field.get("value", default)
        return default

    shipment_status = extract("shipment_status")
    follow_up_date  = extract("followup_date")
    human_callback  = extract("requested_human_callback", False)

    print(f"[WEBHOOK] conv_id={conversation_id}")
    print(f"[WEBHOOK]   contractor={contractor_name} shipment={shipment_id}")
    print(f"[WEBHOOK]   duration={duration}s status='{shipment_status}'")
    print(f"[WEBHOOK]   followup={follow_up_date} human_cb={human_callback}")

    # 5. Write to PostgreSQL
    try:
        conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
        await conn.execute(
            """
            INSERT INTO elevenlabs_calls
                (conversation_id, caller_number, contractor_name,
                 shipment_id, shipment_status, follow_up_date,
                 human_callback, duration_secs, transcript, ended_at)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
            ON CONFLICT (conversation_id) DO UPDATE SET
                shipment_status = EXCLUDED.shipment_status,
                follow_up_date  = EXCLUDED.follow_up_date,
                human_callback  = EXCLUDED.human_callback,
                duration_secs   = EXCLUDED.duration_secs,
                transcript      = EXCLUDED.transcript
            """,
            conversation_id,
            phone_number,
            contractor_name,
            shipment_id,
            shipment_status,
            follow_up_date,
            human_callback,
            duration,
            json.dumps(transcript),
        )
        await conn.close()
        print(f"[WEBHOOK] Saved to elevenlabs_calls.")
    except Exception as e:
        print(f"[WEBHOOK ERROR] {type(e).__name__}: {e}")
        return {"status": "error", "message": str(e)}

    return {"status": "saved", "conversation_id": conversation_id}