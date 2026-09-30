"""Probe parameters for https://app.2proxy.vn/api/proxy.php."""
import urllib.request
import urllib.parse
import json

tests = [
    "https://app.2proxy.vn/api/proxy.php",
    "https://app.2proxy.vn/api/proxy.php?key=",
    "https://app.2proxy.vn/api/proxy.php?key=test",
    "https://app.2proxy.vn/api/proxy.php?key=test&action=get",
    "https://app.2proxy.vn/api/proxy.php?key=test&nhamang=all",
    "https://app.2proxy.vn/api/proxy.php?key=test&type=http",
]

for url in tests:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="ignore").strip()
            print(f"[{resp.status}] {url} -> {body}", flush=True)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore").strip()
        print(f"[{e.code}] {url} -> {body[:150]}", flush=True)
    except Exception as ex:
        print(f"[ERR] {url} -> {ex}", flush=True)
