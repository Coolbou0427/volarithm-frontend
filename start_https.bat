@echo off
cd /d "d:\.Volarithm\web-interface"

set PYTHON=d:\.Volarithm\prototype\.venv\Scripts\python.exe

echo Starting Volarithm Server with HTTPS...
echo.

REM Check if SSL certificates exist
if not exist "cert.pem" (
    echo SSL certificates not found. Generating...
    %PYTHON% generate_ssl.py
    if errorlevel 1 (
        echo Failed to generate SSL certificates. Starting with HTTP...
        %PYTHON% server.py
        exit /b
    )
    echo.
)

REM Set SSL environment variables and start server
set SSL_CERT_PATH=%cd%\cert.pem
set SSL_KEY_PATH=%cd%\key.pem

echo Starting HTTPS server...
echo Certificate: %SSL_CERT_PATH%
echo Private Key: %SSL_KEY_PATH%
echo.

%PYTHON% server.py
