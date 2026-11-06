"""Offline syntax, MFA evidence and direct caller check for email changes."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name in ("email_change.py", "session_phase.py"):
    ast.parse((ROOT / name).read_text(encoding="utf-8-sig"), filename=name)
    print("PASS syntax", name)
for path in ROOT.glob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == "get_session_pure_request":
                print("CALLER", path.name, node.lineno)
source = (ROOT / "session_phase.py").read_text(encoding="utf-8-sig")
assert 'session_data["__mfa_verified"] = mfa_verified' in source
assert 'mfa_verified = False' in source
assert 'mfa_verified = True' in source
print("PASS MFA evidence marker")
