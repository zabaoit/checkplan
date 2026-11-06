"""Read-only DNS check of observed ChatGPT and Cloudflare challenge hosts."""
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
hosts = {"chatgpt.com", "challenges.cloudflare.com"}
diagnostics = sorted((ROOT / "test" / "out").glob("account_probe_*.json"), key=lambda path: path.stat().st_mtime)
for path in diagnostics[-3:]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for failure in data.get("failures", []):
        host = failure[0]
        if host and host.endswith(".challenges.cloudflare.com"):
            hosts.add(host)


def resolve(host):
    try:
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        return {"host": host, "addresses": addresses}
    except OSError as exc:
        return {"host": host, "error": str(exc), "errno": exc.errno}


with ThreadPoolExecutor(max_workers=4) as pool:
    for result in pool.map(resolve, sorted(hosts)):
        print(json.dumps(result))

import urllib.parse
import urllib.request

public_host = "brunhild.challenges.cloudflare.com"
for record_type in ("A", "AAAA"):
    try:
        url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": public_host, "type": record_type})
        with urllib.request.urlopen(url, timeout=15) as response:
            answer = json.load(response)
        print("PUBLIC_DNS", json.dumps({"host": public_host, "type": record_type,
            "status": answer.get("Status"), "answers": answer.get("Answer", []),
            "authority": answer.get("Authority", [])}))
    except OSError as exc:
        print("PUBLIC_DNS", json.dumps({"host": public_host, "type": record_type, "error": str(exc)}))
