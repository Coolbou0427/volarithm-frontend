# Volarithm Web Interface

A lightweight FastAPI backend with a responsive frontend to control and visualize the trading bot.

Features
- Auth: signup/login with bcrypt-hashed passwords (local file db)
- Admin: kill switch, priority side (UP/DOWN), stake multiplier, bot commands, live log tail
- User: view own trades only
- Charts: overview daily PnL (admin), time-based stepped live prices like Polymarket

Quick start
1) Install deps (once):
   - pip install fastapi uvicorn passlib[bcrypt]
2) Run server from project root:
   - python -m web-interface.server
3) Open http://localhost:8000

Notes
- Users are stored in state/users.json; passwords are hashed.
- The very first account you create becomes admin automatically. Later signups are normal users unless an admin promotes them by editing state/users.json.
- Control flags are stored in state/control.json and polled by the bot.
- Bot picks up commands from state/web_commands.txt.
- Ensure the bot is running to see live logs and execute commands.
 - The auth dialog Cancel button now closes the dialog without browser validation nags.