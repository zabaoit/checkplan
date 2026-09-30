import os, sys
from pathlib import Path
sys.path.insert(0, "/app/gpt_signup_hybrid")
sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).parent.parent))

from gpt_signup_hybrid.db.engine import DatabaseEngine
from gpt_signup_hybrid.db.repositories import SettingsRepository

def main():
    db = DatabaseEngine("runtime/data.db")
    repo = SettingsRepository(db)
    print("Host twoproxy.enabled:", repo.get("twoproxy.enabled"))
    print("Host twoproxy.keys:", repo.get("twoproxy.keys"))

if __name__ == "__main__":
    main()
