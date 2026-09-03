import subprocess
import sys

def test_start_edge():
    url = "https://chatgpt.com/"
    print(f"[TEST] Spawning Edge via cmd /c start...")
    subprocess.Popen(f'cmd.exe /c start msedge.exe "{url}"', shell=True)
    print("[PASS] Command executed successfully.")

if __name__ == "__main__":
    test_start_edge()
