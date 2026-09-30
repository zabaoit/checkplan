@echo off
chcp 65001 >nul
echo [Docker] Dang khoi dong container gpt_signup_hybrid tren port 8085...
docker compose up -d --build web
if %errorlevel% neq 0 (
    echo [Loi] Khong the khoi dong container Docker. Vui long kiem tra Docker Desktop.
    pause
    exit /b %errorlevel%
)
echo [Docker] Container dang chay tai: http://127.0.0.1:8085/?token=gptsignup2026
echo [Docker] De xem log: docker compose logs -f web
echo [Docker] De dung container: docker compose down
pause
