import json
import requests
from app import LEDGER_API_URL, endpoint_url

for path in ["/", "/orders", "/rates", "/export"]:
    print(f"\n{'=' * 20} {path} {'=' * 20}")

    try:
        url = endpoint_url(path)
        response = requests.get(url, timeout=15)

        print("HTTP status:", response.status_code)
        print("Content type:", response.headers.get("content-type"))

        try:
            data = response.json()
            print(json.dumps(data, indent=2)[:8000])
        except ValueError:
            print(response.text[:2000])

    except Exception as error:
        print("ERROR:", error)