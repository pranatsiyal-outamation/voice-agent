import asyncio
import json
import os
import time
from dotenv import load_dotenv
from livekit import api

load_dotenv()


async def trigger_shipment_followup(
    to_number: str,
    contractor_name: str,
    shipment_id: str,
    trunk_id: str = None,
):
    lk = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    room_name = f"shipment-{shipment_id}-{int(time.time())}"

    dispatch = await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            room=room_name,
            agent_name="shipment-followup-agent",
            metadata=json.dumps({
                "direction":        "outbound",
                "phone_number":     to_number,
                "trunk_id":         trunk_id or os.getenv("SIP_TRUNK_ID"),
                "contractor_name":  contractor_name,
                "shipment_id":      shipment_id,
            }),
        )
    )

    print(f"[DISPATCH] room={room_name}")
    print(f"[DISPATCH] {dispatch}")
    await lk.aclose()


if __name__ == "__main__":
    asyncio.run(trigger_shipment_followup(
        to_number="+15307616112",
        contractor_name="John Smith",
        shipment_id="SH-12345",
    ))
