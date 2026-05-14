from pydantic import BaseModel
from typing import Any


class WhatsAppMessage(BaseModel):
    object: str
    entry: list[Any]

    def get_messages(self) -> list[dict]:
        msgs = []
        for entry in self.entry:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    msgs.append({
                        "from": msg.get("from"),
                        "id": msg.get("id"),
                        "type": msg.get("type"),
                        "text": msg.get("text", {}).get("body", ""),
                        "audio_id": msg.get("audio", {}).get("id"),
                        "phone_number_id": value.get("metadata", {}).get("phone_number_id"),
                    })
        return msgs
