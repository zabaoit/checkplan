"""Probe real endpoint from user screenshot: https://api.proxyxoay.org/api/key_xoay.php."""
import json
import urllib.request
import urllib.parse

key = "tf2MlarE09Mi17GxvX22"

urls = [
    f"https://api.proxyxoay.org/api/key_xoay.php?key={key}&live=2",
    f"https://api.proxyxoay.org/api/key_xoay.php?key={key}&live=1",
    f"https://app.2proxy.vn/api/key_xoay.php?key={key}&live=2",
]

for url in urls:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore").strip()
            print(f"[{resp.status}] {url} -> {body}", flush=True)
    except Exception as ex:
        print(f"[ERR] {url} -> {ex}", flush=True)
