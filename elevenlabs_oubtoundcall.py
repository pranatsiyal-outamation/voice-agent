import requests
import os
from dotenv import load_dotenv

load_dotenv()

def trigger_outbound_call(to_number: str, contractor_name: str, shipment_id: str):
    response = requests.post(
        "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
        headers={
            "xi-api-key": os.getenv("ELEVENLABS_API_KEY"),
            "Content-Type": "application/json",
        },
        json={
            "agent_id": os.getenv("ELEVENLABS_AGENT_ID"),
            "agent_phone_number_id": os.getenv("ELEVENLABS_PHONE_ID"),
            "to_number": to_number,
            "conversation_initiation_client_data": {
                "dynamic_variables": {
                    "contractor_name": contractor_name,
                    "shipment_id": shipment_id,
                },
                "conversation_config_override": {
                    "agent": {
                        "first_message": f"Hi, am I speaking with {contractor_name}?"
                    }
                }
            }
        }
    )
    response.raise_for_status()
    return response.json()

result = trigger_outbound_call(
    to_number="+15307616112",
    contractor_name="John Smith",
    shipment_id="SH-12345"
)
print(result)
