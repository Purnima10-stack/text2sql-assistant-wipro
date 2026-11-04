""" import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(base_url=os.environ["OPENAI_COMPAT_BASE_URL"],
                api_key=os.environ["OPENAI_COMPAT_API_KEY"])
for m in client.models.list():
    print(m.id) """

import os, httpx
from dotenv import load_dotenv

load_dotenv()
key = os.environ["OPENAI_COMPAT_API_KEY"]
url = "https://generativelanguage.googleapis.com/v1beta/models"
params, found = {"pageSize": 1000}, []
while True:
    r = httpx.get(url, headers={"x-goog-api-key": key}, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    for m in data.get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            found.append(m["name"])
    if not data.get("nextPageToken"):
        break
    params["pageToken"] = data["nextPageToken"]
print("\n".join(sorted(found)) or "No text-generation models found for this key")