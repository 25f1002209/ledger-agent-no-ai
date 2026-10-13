import os, requests
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from dotenv import load_dotenv
load_dotenv()
root = os.getenv("LEDGER_API_URL", "").strip()
if not root:
    raise SystemExit("Set LEDGER_API_URL to the complete questionData URL, including path=/")

def make_url(path):
    p = urlparse(root)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["path"] = path
    return urlunparse(p._replace(query=urlencode(q)))

for path in ("/", "/orders", "/rates", "/export"):
    print("\\n###", path)
    try:
        r = requests.get(make_url(path), timeout=15)
        print("URL:", r.url)
        print("HTTP:", r.status_code, "Content-Type:", r.headers.get("content-type"))
        print(r.text[:5000])
    except Exception as e:
        print(type(e).__name__, e)
