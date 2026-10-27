import pathlib

files = [
    "change_password.py",
    "web/change_password_manager.py",
    "web/static/change_password.js",
    "web/static/index.html",
    "web/static/style.css",
]

for f in files:
    p = pathlib.Path(f)
    if p.exists():
        text = p.read_bytes().replace(b"\r\n", b"\n")
        p.write_bytes(text)
        print(f"Normalized {f} to LF")
