import asyncio
import json
import os
import time
from dotenv import load_dotenv
from livekit import api

load_dotenv()

async def make_outbound_call(to_number: str, purpose: str):
    lk = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    room_name = f"outbound-{to_number}-{int(time.time())}"

    # dispatch agent with metadata — agent will dial the number itself
    dispatch = await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            room=room_name,
            agent_name="outbound-agent",
            metadata=json.dumps({
                "direction": "outbound",
                "purpose": purpose,
                "phone_number": to_number,
                "trunk_id": "ST_rsf8HGUzUuFx"
            })
        )
    )

    print(f"[OUTBOUND] Dispatch created: {dispatch} | room={room_name}")
    await lk.aclose()

if __name__ == "__main__":
    asyncio.run(make_outbound_call(
        to_number="+15307616112",
        purpose="Hello there, this is a test call, my objective is to find out your birhtday."
    ))
