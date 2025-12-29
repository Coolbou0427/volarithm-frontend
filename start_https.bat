@echo off
cd /d "d:\.Volarithm\prototype\web-interface"

echo Starting Volarithm Server with HTTPS...
echo.

REM Check if SSL certificates exist
if not exist "cert.pem" (
    echo SSL certificates not found. Generating...
    python generate_ssl.py
    if errorlevel 1 (
        echo Failed to generate SSL certificates. Starting with HTTP...
        python server.py
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

python server.py
