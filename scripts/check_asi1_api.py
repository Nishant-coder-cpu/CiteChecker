
import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("ASI_ONE_API_KEY")

if not api_key:
    raise RuntimeError(
        "ASI_ONE_API_KEY is missing. Check your .env file."
    )

response = requests.post(
    "https://api.asi1.ai/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    },
    json={
        "model": "asi1",
        "messages": [
            {
                "role": "user",
                "content": (
                    "Reply with exactly: "
                    "ASI1 API connection successful"
                ),
            }
        ],
    },
    timeout=60,
)

print("HTTP status:", response.status_code)
response.raise_for_status()

data = response.json()
print(data["choices"][0]["message"]["content"])
