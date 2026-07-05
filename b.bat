@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Khong thay .venv\Scripts\python.exe
    echo Hay tao/active venv va cai dependencies truoc.
    exit /b 1
)

".venv\Scripts\python.exe" -m gpt_signup_hybrid web --host 127.0.0.1 --port 8083
