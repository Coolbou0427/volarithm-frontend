import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Request, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware
from passlib.context import CryptContext
from pydantic import BaseModel
from typing import Optional, Dict, Any
import secrets
import os
import json
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
import asyncio
import threading
import time
import csv
import io
import requests  # Polymarket data API
from eth_account import Account  # derive public address from private key
# Resolve local project paths based on this file's location.
SERVER_DIR = Path(__file__).resolve().parent

def _find_module_root(module_file: str) -> Optional[Path]:
    for base in [SERVER_DIR, *SERVER_DIR.parents]:
        direct = base / module_file
        if direct.is_file():
            return base
        proto = base / "prototype" / module_file
        if proto.is_file():
            return base / "prototype"
    return None

MODULE_ROOT = _find_module_root("settings.py") or _find_module_root("time_utils.py")
if MODULE_ROOT and str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))
from settings import get_default_stake  # type: ignore
from time_utils import now_et, today_noon_et, ET  # type: ignore
from market_trading import resolve_bitcoin_updown_token_ids_for_date  # type: ignore
from market_trading import clob_price  # reuse lightweight price helper (adjust if different source preferred)

# Global cache for stats
STATS_CACHE = {
    'data': None,
    'last_update': None,
    'update_interval': 30  # Update every 30 seconds instead of 5 minutes
}


BASE = MODULE_ROOT or SERVER_DIR.parent
STATE = BASE / "state"
WEB_DIR = SERVER_DIR
STATE.mkdir(exist_ok=True)

USERS_DB_PATH = STATE / "users.json"
CONTROL_PATH = STATE / "control.json"
WEB_CMDS_PATH = STATE / "web_commands.txt"
BOT_STATUS_PATH = STATE / "bot_status.json"
LOGS_DIR = BASE / "logs"
HIST_CSV = BASE / "tester" / "btc_updown_last90.csv"
TOKENS_PATH = STATE / "tokens.json"

# Derived paths/directories used later
CHART_CACHE = STATE / "chart_cache"
CHART_CACHE.mkdir(exist_ok=True)
PRICE_LOG = STATE / "price_log.csv"

# FastAPI app & middleware setup (was missing after refactor)
app = FastAPI(title="Volarithm Web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=500)

# Auth / security globals
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")


class User(BaseModel):
    username: str
    is_admin: bool = False


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: Optional[bool] = False


class UserLogin(BaseModel):
    username: str
    password: str


class ProfileUpdate(BaseModel):
    new_username: Optional[str] = None
    new_password: Optional[str] = None
    old_password: Optional[str] = None
    paused: Optional[bool] = None
    timezone: Optional[str] = None
    private_key: Optional[str] = None
    clear_private_key: Optional[bool] = False


class ControlUpdate(BaseModel):
    kill: Optional[bool] = None
    stake_multiplier: Optional[float] = None
    priority_side: Optional[str] = None  # "UP" / "DOWN"


# Simple token store (memory + disk persistence)
TOKENS: Dict[str, Dict[str, Any]] = {}


def _load_tokens_file() -> Dict[str, Dict[str, Any]]:
    if TOKENS_PATH.exists():
        try:
            with open(TOKENS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
                # Rehydrate exp datetimes
                for t, info in data.items():
                    exp_txt = info.get("exp")
                    if isinstance(exp_txt, str):
                        try:
                            info["exp"] = datetime.fromisoformat(exp_txt)
                        except Exception:
                            info["exp"] = None
                return data
        except Exception:
            return {}
    return {}


def _save_tokens_file(tokens: Dict[str, Dict[str, Any]]):
    out = {}
    for k, v in tokens.items():
        v2 = dict(v)
        if isinstance(v2.get("exp"), datetime):
            v2["exp"] = v2["exp"].isoformat()
        out[k] = v2
    try:
        with open(TOKENS_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except Exception:
        pass

# --- Realized PnL aggregation (replacement for older trade-log derived total profit) ---
def calculate_fast_stats():
    """Quick stats calculation using only local data (no API calls)"""
    try:
        users_cfg = _load_users()
        start_str = users_cfg.get('_bot_config', {}).get('start_date', '2025-08-28')
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        except Exception:
            start_date = datetime.utcnow().date()
        days_running = (datetime.utcnow().date() - start_date).days + 1
        
        # Just count users quickly
        user_count = len([k for k in users_cfg.keys() if not k.startswith('_')])
        
        # Get base profit from balance history
        base_val = 10.36  # Default
        try:
            balance_history_path = BASE / "state" / "balance_history.json"
            if balance_history_path.exists():
                with open(balance_history_path, 'r') as f:
                    balance_history = json.load(f)
                base_val = balance_history.get('starting_balances', {}).get('Founders', {}).get('transferred_profit_from_previous_account', 10.36)
        except Exception:
            pass
        
        # Use config adjustment if available
        profile_adjustment = float(users_cfg.get('_bot_config', {}).get('profile_pnl_adjustment', 0.0))
        
        # Rough estimate: ledger-based realized + adjustment + base
        # (This will be refined by the full calculation in background)
        estimated_total = profile_adjustment + base_val
        
        return {
            "days_running": days_running,
            "users": user_count,
            "realized_profit": "$0.00",  # Will be updated by full calculation
            "total_profit": f"${profile_adjustment:.2f}",
            "base_profit": f"${base_val:.2f}",
            "total_profit_with_base": f"${estimated_total:.2f}",
            "accounts": [],
            "_fast_mode": True
        }
    except Exception:
        return {
            "days_running": 1,
            "users": 1,
            "realized_profit": "$0.00",
            "total_profit": "$0.00", 
            "base_profit": "$10.36",
            "total_profit_with_base": "$10.36",
            "accounts": [],
            "_fast_mode": True
        }

def calculate_fresh_stats():
    """Comprehensive stats calculation using Polymarket APIs for current data"""
    # Load base profit from balance_history.json instead of hardcoded value
    def _get_base_profit():
        try:
            import json
            from pathlib import Path
            balance_history_path = Path(__file__).parent.parent / "state" / "balance_history.json"
            if balance_history_path.exists():
                with open(balance_history_path, 'r') as f:
                    balance_history = json.load(f)
                base_profit = balance_history.get('starting_balances', {}).get('Founders', {}).get('transferred_profit_from_previous_account', 0.0)
                print(f"  Loaded base profit from balance_history.json: ${base_profit:.2f}")
                return base_profit
            else:
                # Fallback to environment variable if file doesn't exist
                base_profit = float(os.environ.get("ORIGINAL_ACCOUNT_PROFIT_BASE", "10.36"))
                print(f"  Using fallback base profit: ${base_profit:.2f}")
                return base_profit
        except Exception as e:
            print(f"  Error loading base profit, using fallback: {e}")
            return float(os.environ.get("ORIGINAL_ACCOUNT_PROFIT_BASE", "10.36"))
    
    ORIGINAL_BASE = _get_base_profit()
    from datetime import datetime

    def _collect_accounts():
        users = _load_users()
        return users.get('_trading_config', {}).get('accounts', []), users

    def _addr(acct: dict):
        # For Polymarket PnL queries, use the main wallet (derived from private key) first
        # because that's what shows up in the Polymarket profile
        pk = acct.get('private_key')
        if isinstance(pk, str) and pk.startswith('0x') and len(pk) >= 66 and 'REPLACE_WITH_TEST' not in pk:
            try:
                return Account.from_key(pk).address
            except Exception:
                pass
        # Fallback to proxy_wallet or address
        for key in ("proxy_wallet", "address"):
            v = acct.get(key)
            if isinstance(v, str) and v.startswith('0x') and len(v) == 42:
                return v
        return None

    def _polymarket_total_pnl(address: str, private_key: str = None) -> dict:
        """Get PnL from Polymarket using balance_checker functions"""
        try:
            from balance_checker import get_polymarket_account_stats_authenticated, get_polymarket_account_stats
            if private_key and private_key.startswith('0x') and 'REPLACE_WITH_TEST' not in private_key:
                stats = get_polymarket_account_stats_authenticated(address, private_key)
            else:
                stats = get_polymarket_account_stats(address)
            
            # Extract PnL data - use total_pnl if available (includes unrealized), otherwise fall back to realized
            realized_pnl = float(stats.get('realized_pnl', 0.0))
            total_pnl = float(stats.get('total_pnl', realized_pnl))  # New field with unrealized included
            unrealized_pnl = float(stats.get('unrealized_pnl', 0.0))
            portfolio_value = float(stats.get('total_portfolio_value', 0.0))
            
            return {
                "address": address,
                "realized": realized_pnl,
                "unrealized": unrealized_pnl,
                "total": total_pnl,  # This now includes both realized and unrealized
                "holdings_value": portfolio_value,
                "raw_stats": stats,
            }
        except Exception as e:
            print(f"Error getting Polymarket stats for {address}: {e}")
            return {
                "address": address,
                "realized": 0.0,
                "unrealized": 0.0,
                "total": 0.0,
                "holdings_value": 0.0,
                "error": str(e),
            }

    try:
        # Days running and start window from bot config
        users_cfg = _load_users()
        start_str = users_cfg.get('_bot_config', {}).get('start_date', '2025-08-28')
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        except Exception:
            start_date = datetime.utcnow().date()
        days_running = (datetime.utcnow().date() - start_date).days + 1

        # Ledger-based aggregation since start_date
        accounts_cfg, users = _collect_accounts()
        user_count = len([k for k in users.keys() if not k.startswith('_')])
        agg_realized = 0.0
        details = []
        any_ledger = False

        def _acct_name(acct: dict) -> str:
            name = str(acct.get('name') or '').strip()
            if not name:
                # Derive from address tail as fallback
                a = _addr(acct) or 'acct'
                return f"acct_{a[-6:]}"
            return name

        for acct in accounts_cfg:
            if not acct.get('enabled', True):
                continue
            name = _acct_name(acct)
            ledger = STATE / name / "trades.jsonl"
            buys = sells = 0.0
            count = 0
            if ledger.exists():
                any_ledger = True
                recs = _read_jsonl(ledger, limit=50000)
                for r in recs:
                    ts = r.get("ts")
                    try:
                        d = datetime.fromisoformat(ts).date() if ts else None
                    except Exception:
                        d = None
                    if d and d < start_date:
                        continue
                    ev = str(r.get("event") or "").upper()
                    px = float(r.get("price") or 0.0)
                    sz = float(r.get("size") or 0.0)
                    if ev.startswith("BUY_"):
                        buys += px * sz
                        count += 1
                    elif ev.startswith("SELL_"):
                        sells += px * sz
                        count += 1
                    elif ev == "REDEEM":
                        # If ledger includes explicit redemption value, treat as sell of sz at $1
                        if sz > 0 and px == 0.0:
                            sells += 1.0 * sz
                            count += 1
            realized = sells - buys
            agg_realized += realized
            details.append({
                "account": name,
                "realized": round(realized, 2),
                "trades": count,
            })

        # Also compute Data API PnL per account to match Polymarket profile (cashPnl / realizedPnl)
        from balance_checker import get_data_api_positions_pnl  # type: ignore
        data_api_cash = 0.0
        data_api_realized = 0.0
        for acct in accounts_cfg:
            if not acct.get('enabled', True):
                continue
            # Query both main wallet (from private key) and proxy wallet if different
            addresses = []
            
            # Main wallet (what shows in Polymarket profile)
            pk = acct.get('private_key')
            if isinstance(pk, str) and pk.startswith('0x') and 'REPLACE_WITH_TEST' not in pk:
                try:
                    main_addr = Account.from_key(pk).address
                    addresses.append(main_addr)
                except Exception:
                    pass
            
            # Proxy wallet (if different from main)
            proxy_addr = acct.get('proxy_wallet')
            if proxy_addr and proxy_addr not in addresses:
                addresses.append(proxy_addr)
            
            # Regular address field (if different from above)
            reg_addr = acct.get('address')
            if reg_addr and reg_addr not in addresses:
                addresses.append(reg_addr)
            
            # Sum PnL across all addresses for this account
            for addr in addresses:
                if not addr:
                    continue
                dp = get_data_api_positions_pnl(addr)
                data_api_cash += float(dp.get('cash_pnl', 0.0) or 0.0)
                data_api_realized += float(dp.get('realized_pnl', 0.0) or 0.0)

        # Additionally compute CLOB public realized PnL (all-time) as a more available fallback
        from balance_checker import get_polymarket_account_stats  # type: ignore
        clob_realized_sum = 0.0
        for acct in accounts_cfg:
            if not acct.get('enabled', True):
                continue
            # Use same multi-address logic for CLOB API
            addresses = []
            pk = acct.get('private_key')
            if isinstance(pk, str) and pk.startswith('0x') and 'REPLACE_WITH_TEST' not in pk:
                try:
                    main_addr = Account.from_key(pk).address
                    addresses.append(main_addr)
                except Exception:
                    pass
            proxy_addr = acct.get('proxy_wallet')
            if proxy_addr and proxy_addr not in addresses:
                addresses.append(proxy_addr)
            reg_addr = acct.get('address')
            if reg_addr and reg_addr not in addresses:
                addresses.append(reg_addr)
            
            for addr in addresses:
                if not addr:
                    continue
                try:
                    stats = get_polymarket_account_stats(addr)
                    clob_realized_sum += float(stats.get('realized_pnl', 0.0) or 0.0)
                except Exception:
                    continue

        # Soft fallback: if no ledger present at all, use Polymarket realized (may include pre-bot history)
        if not any_ledger:
            agg_unrealized = 0.0
            details = []
            seen = set()
            for acct in accounts_cfg:
                if not acct.get('enabled', True):
                    continue
                addr = _addr(acct)
                if not addr or addr.lower() in seen:
                    continue
                seen.add(addr.lower())
                pk = acct.get('private_key') if isinstance(acct.get('private_key'), str) else None
                pnl = _polymarket_total_pnl(addr, pk)
                r = float(pnl.get("realized", 0.0) or 0.0)
                u = float(pnl.get("unrealized", 0.0) or 0.0)
                agg_realized += r
                agg_unrealized += u
                details.append({
                    "address": addr,
                    "realized": round(r, 2),
                    "unrealized": round(u, 2),
                    "total": round(r, 2),
                    "error": pnl.get("error")
                })

        # Display strategy:
        # - realized_profit: our ledger-since-start realized (agg_realized)  
        # - total_profit: prefer profile_adjustment if set (matches actual Polymarket profile), otherwise use API data
        # - total_profit_with_base: total_profit + base carryover
        
        # Manual adjustment for profile matching (this should match what user sees on Polymarket profile)
        profile_adjustment = float(users_cfg.get('_bot_config', {}).get('profile_pnl_adjustment', 0.0))
        if profile_adjustment == 0.0:
            profile_adjustment = float(os.environ.get("PROFILE_PNL_ADJUSTMENT", "0.0"))
        
        # If we have a profile adjustment, use it (this matches the actual Polymarket profile)
        # Otherwise fall back to API data
        if abs(profile_adjustment) > 1e-9:
            final_total = profile_adjustment
        else:
            final_total = (
                data_api_cash if abs(data_api_cash) > 1e-9 else (
                    clob_realized_sum if abs(clob_realized_sum) > 1e-9 else agg_realized
                )
            )
        
        with_base = final_total + ORIGINAL_BASE
        return {
            "days_running": days_running,
            "users": user_count,
            "realized_profit": f"${agg_realized:.2f}",
            "total_profit": f"${final_total:.2f}",
            "base_profit": f"${ORIGINAL_BASE:.2f}",
            "total_profit_with_base": f"${with_base:.2f}",
            "accounts": details,
        }
    except Exception:
        return {
            "days_running": 0,
            "users": 0,
            "realized_profit": "$0.00",
            "base_profit": f"${ORIGINAL_BASE:.2f}",
            "total_profit": f"${ORIGINAL_BASE:.2f}"
        }

def _load_bot_status() -> dict:
    if BOT_STATUS_PATH.exists():
        try:
            with open(BOT_STATUS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"running": True, "shutdown_by": None, "shutdown_time": None}


def _save_bot_status(status: dict):
    try:
        BOT_STATUS_PATH.parent.mkdir(exist_ok=True)
        with open(BOT_STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass


def _shutdown_bot(admin_username: str):
    """Shutdown the bot by writing a command and updating status"""
    # Write shutdown command for the bot to pick up
    try:
        with open(WEB_CMDS_PATH, "a", encoding="utf-8") as f:
            f.write("shutdown\n")
    except Exception:
        pass
    
    # Update bot status
    status = _load_bot_status()
    status.update({
        "running": False,
        "shutdown_by": admin_username,
        "shutdown_time": datetime.utcnow().isoformat()
    })
    _save_bot_status(status)


def _load_users() -> dict:
    if USERS_DB_PATH.exists():
        try:
            with open(USERS_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}

@app.get("/api/stats")
def get_public_stats():
    """Get public statistics for the about page with caching."""
    
    # Check if we have cached data that's still fresh
    now = time.time()
    if (STATS_CACHE['data'] is not None and 
        STATS_CACHE['last_update'] is not None and
        now - STATS_CACHE['last_update'] < STATS_CACHE['update_interval']):
        return STATS_CACHE['data']
    
    # Use the full fresh stats calculation that queries Polymarket APIs
    try:
        stats = calculate_fresh_stats()
        # Remove the _fast_mode flag if present
        if '_fast_mode' in stats:
            del stats['_fast_mode']
        
        # Update cache
        STATS_CACHE['data'] = stats
        STATS_CACHE['last_update'] = now
        
        return stats
        
    except Exception as e:
        print(f"Error in get_public_stats: {e}")
        # Return hardcoded fallback that matches HTML
        return {
            'days_running': 14,
            'users': 6,
            'realized_profit': '$0.00',
            'total_profit': '$2.36',
            'base_profit': '$10.36',
            'total_profit_with_base': '$12.72'
        }

def _update_stats_cache_background():
    """Update stats cache in background thread"""
    try:
        fresh_stats = calculate_fresh_stats()
        if '_fast_mode' in fresh_stats:
            del fresh_stats['_fast_mode']
        STATS_CACHE['data'] = fresh_stats
        STATS_CACHE['last_update'] = time.time()
        print(f"[INFO] Stats cache updated in background: {fresh_stats}")
    except Exception as e:
        print(f"[ERROR] Background stats update failed: {e}")

## Legacy calculate_fresh_stats removed (replaced by realized PnL aggregation above)


@app.get("/api/stats/refresh")
def force_refresh_stats():
    """Force refresh the stats cache immediately"""
    try:
        fresh_stats = calculate_fresh_stats()
        if '_fast_mode' in fresh_stats:
            del fresh_stats['_fast_mode']
        STATS_CACHE['data'] = fresh_stats
        STATS_CACHE['last_update'] = time.time()
        return {"message": "Stats refreshed", "data": fresh_stats}
    except Exception as e:
        return {"error": str(e)}


def _save_users(db: dict):
    with open(USERS_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)


def verify_password(plain, hashed) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def hash_password(plain) -> str:
    return pwd_context.hash(plain)


def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    print(f"[DEBUG] get_current_user called with token: {token[:10] if token else 'None'}...")
    info = TOKENS.get(token)
    print(f"[DEBUG] Token found in memory: {bool(info)}")
    if not info:
        # Fallback to on-disk tokens (survive server restarts)
        disk = _load_tokens_file()
        print(f"[DEBUG] Loaded {len(disk)} tokens from disk")
        if disk:
            TOKENS.update(disk)
            info = TOKENS.get(token)
            print(f"[DEBUG] Token found after disk load: {bool(info)}")
    if not info:
        print(f"[DEBUG] No token info found, raising 401")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    # Expiry check
    if info.get("exp") and datetime.utcnow() > info["exp"]:
        print(f"[DEBUG] Token expired, removing")
        TOKENS.pop(token, None)
        _save_tokens_file(TOKENS)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    print(f"[DEBUG] Token valid for user: {info['username']}")
    return User(username=info["username"], is_admin=info.get("is_admin", False))


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")
    return user


def require_bot_running(user: User = Depends(get_current_user)) -> User:
    """Middleware to check if bot is running for non-admin users"""
    status = _load_bot_status()
    if not status.get("running", True) and not user.is_admin:
        raise HTTPException(status_code=503, detail="Bot is currently shutdown")
    return user


# --- Auth endpoints ---
@app.post("/api/signup")
def signup(new_user: UserCreate):
    db = _load_users()
    if new_user.username in db:
        raise HTTPException(status_code=400, detail="Username exists")
    
    # Validate username format
    if new_user.username.startswith("_"):
        raise HTTPException(status_code=400, detail="Username cannot start with underscore (reserved for system use)")
    if not new_user.username.strip():
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if len(new_user.username) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    
    # Validate password strength
    if len(new_user.password) < 10 or not any(c.isdigit() for c in new_user.password) or not any(c.isalpha() for c in new_user.password):
        raise HTTPException(status_code=400, detail="Weak password: min 10 chars, mix of letters and numbers")
    # First user becomes admin automatically
    is_first_user = len(db) == 0
    db[new_user.username] = {
        "password": hash_password(new_user.password),
        "is_admin": True if is_first_user else bool(new_user.is_admin),
        "paused": False,
        "timezone": "America/Los_Angeles",
        "created": datetime.utcnow().isoformat(),
    }
    _save_users(db)
    return {"ok": True}


@app.post("/api/login")
def login(form: OAuth2PasswordRequestForm = Depends()):
    print(f"[DEBUG] Login attempt for username: {form.username}")
    db = _load_users()
    row = db.get(form.username)
    print(f"[DEBUG] User found in database: {bool(row)}")
    if not row or not verify_password(form.password, row.get("password", "")):
        print(f"[DEBUG] Login failed - invalid credentials")
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = secrets.token_urlsafe(32)
    TOKENS[token] = {
        "username": form.username,
        "is_admin": bool(row.get("is_admin", False)),
        "exp": datetime.utcnow() + timedelta(hours=12),
    }
    _save_tokens_file(TOKENS)
    print(f"[DEBUG] Login successful, token created: {token[:10]}...")
    return {"access_token": token, "token_type": "bearer"}


@app.get("/api/me")
def me(user: User = Depends(get_current_user)):
    return user


@app.get("/api/user/profile")
def get_profile(user: User = Depends(get_current_user)):
    db = _load_users()
    row = db.get(user.username) or {}
    pk = row.get("private_key")
    hint = None
    if isinstance(pk, str) and len(pk) >= 6:
        hint = "…" + pk[-4:]
    return {
        "username": user.username,
        "paused": bool(row.get("paused", False)),
        "timezone": row.get("timezone") or "America/Los_Angeles",
        "has_private_key": bool(pk),
        "private_key_hint": hint,
    }


@app.post("/api/user/profile")
def update_profile(update: ProfileUpdate, user: User = Depends(get_current_user), token: str = Depends(oauth2_scheme)):
    db = _load_users()
    row = db.get(user.username)
    if not row:
        # Bootstrap a minimal user record if missing (e.g., after file reset while a valid token exists)
        row = {
            "password": row.get("password", "") if isinstance(row, dict) else "",
            "is_admin": bool(TOKENS.get(token, {}).get("is_admin", False)),
            "paused": False,
            "timezone": "America/Los_Angeles",
            "created": datetime.utcnow().isoformat(),
        }
        db[user.username] = row
    # Change username (no password required when logged in)
    if update.new_username:
        if update.new_username in db and update.new_username != user.username:
            raise HTTPException(status_code=400, detail="Username already taken")
        db[update.new_username] = row
        db.pop(user.username, None)
        user.username = update.new_username
        # propagate to active token
        if token in TOKENS:
            TOKENS[token]["username"] = update.new_username
            _save_tokens_file(TOKENS)
    # Change password
    if update.new_password:
        if not update.old_password or not verify_password(update.old_password, row.get("password", "")):
            raise HTTPException(status_code=400, detail="Current password incorrect")
        if len(update.new_password) < 10 or not any(c.isdigit() for c in update.new_password) or not any(c.isalpha() for c in update.new_password):
            raise HTTPException(status_code=400, detail="Weak password: min 10 chars, mix of letters and numbers")
        target = db.get(user.username) or row
        target["password"] = hash_password(update.new_password)
    # Pause toggle
    if update.paused is not None:
        target = db.get(user.username) or row
        target["paused"] = bool(update.paused)
    if update.timezone:
        target = db.get(user.username) or row
        target["timezone"] = update.timezone
    # Private key management (store masked; never return raw key)
    if update.clear_private_key:
        target = db.get(user.username) or row
        target.pop("private_key", None)
    if update.private_key:
        pk = str(update.private_key).strip()
        if pk.startswith("0x"):
            pk = pk[2:]
        # Basic validation: 64 hex characters
        if len(pk) != 64 or any(c not in "0123456789abcdefABCDEF" for c in pk):
            raise HTTPException(status_code=400, detail="Invalid private key format")
        target = db.get(user.username) or row
        target["private_key"] = "0x" + pk.lower()
    _save_users(db)
    return {"ok": True}


@app.delete("/api/user/delete")
def delete_user(user: User = Depends(get_current_user), token: str = Depends(oauth2_scheme)):
    """Delete the current user's account (non-admin only)."""
    # Prevent admin deletion
    if TOKENS.get(token, {}).get("is_admin", False):
        raise HTTPException(status_code=403, detail="Admin accounts cannot be deleted")
    
    # Load user database
    db = _load_users()
    if user.username not in db:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Remove user from database
    db.pop(user.username, None)
    _save_users(db)
    
    # Invalidate user's token
    if token in TOKENS:
        TOKENS.pop(token, None)
        _save_tokens_file(TOKENS)
    
    return {"ok": True, "message": "Account deleted successfully"}


@app.get("/api/trades/all")
def all_trades(user: User = Depends(require_admin)):
    """Return combined recent trades across all accounts (admin only)."""
    trades = []
    for d in STATE.iterdir():
        if not d.is_dir():
            continue
        # Skip non-account directories
        name = d.name
        if name.lower() in {"chart_cache"} or name.startswith(".") or name.startswith("_"):
            continue
        ledger = d / "trades.jsonl"
        recs = _read_jsonl(ledger, limit=5000)
        for r in recs:
            r2 = dict(r)
            r2["account"] = d.name
            trades.append(r2)
    # Sort by timestamp if present
    def _key(rec):
        ts = rec.get("ts")
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return datetime.min
    trades.sort(key=_key, reverse=True)
    return {"trades": trades}


def _parse_hist_csv(target_date: datetime):
    if not HIST_CSV.exists():
        raise FileNotFoundError("history csv not found")
    
    # Handle CSV format: ['ts', 'up', 'down'] with both probabilities in same row
    data_points = []
    
    with open(HIST_CSV, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                # Parse timestamp
                ts_str = row.get("ts", "").strip()
                if not ts_str:
                    continue
                    
                try:
                    dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                except Exception:
                    continue
                
                # Check if this timestamp is for our target date
                if dt.date() != target_date.date():
                    continue
                
                # Parse up and down probabilities
                up_str = row.get("up", "").strip()
                down_str = row.get("down", "").strip()
                
                try:
                    up_prob = float(up_str) if up_str else None
                    down_prob = float(down_str) if down_str else None
                except Exception:
                    continue
                
                # Store data point
                ts_out = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                data_points.append({
                    "ts": ts_out, 
                    "up": up_prob, 
                    "down": down_prob
                })
                
            except Exception:
                continue
    
    # Sort by timestamp
    data_points.sort(key=lambda x: x["ts"])
    return data_points


def _ensure_price_log_header():
    if not PRICE_LOG.exists():
        try:
            with open(PRICE_LOG, "w", encoding="utf-8") as f:
                f.write("ts,up,down\n")
        except Exception:
            pass


def _append_price_row(ts_iso: str, up: float, down: float):
    _ensure_price_log_header()
    try:
        with open(PRICE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts_iso},{up},{down}\n")
    except Exception:
        pass


def _parse_price_log_for_day(target_date: datetime):
    out = []
    if not PRICE_LOG.exists():
        return out
    try:
        with open(PRICE_LOG, "r", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                ts_txt = (row.get("ts") or "").strip()
                if not ts_txt:
                    continue
                try:
                    dt = datetime.fromisoformat(ts_txt.replace("Z", "+00:00"))
                except Exception:
                    try:
                        dt = datetime.utcfromtimestamp(float(ts_txt))
                    except Exception:
                        continue
                if dt.date() != target_date.date():
                    continue
                try:
                    upf = float(row.get("up") or "")
                except Exception:
                    upf = None
                try:
                    dnf = float(row.get("down") or "")
                except Exception:
                    dnf = None
                if upf is None and dnf is None:
                    continue
                ts_out = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                out.append({"ts": ts_out, "up": upf, "down": dnf})
    except Exception:
        return out
    out.sort(key=lambda x: x["ts"])  # type: ignore
    return out


def _generate_synthetic_day_data(target_date: datetime):
    """Generate synthetic minute-by-minute data for a full trading day that ends with a clear winner"""
    import random
    import math
    
    points = []
    
    # Generate data for a full 24-hour period in UTC
    # This ensures we have data spanning any timezone's 9 AM to 9 AM period
    start_dt = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = start_dt + timedelta(hours=24)
    
    current_dt = start_dt
    base_probability = 0.5  # Start at 50%
    
    # Determine if UP or DOWN will win this day (random choice)
    up_wins = random.choice([True, False])
    total_minutes = 24 * 60
    
    for minute_index in range(total_minutes):
        # Progress through the day (0.0 to 1.0)
        day_progress = minute_index / total_minutes
        
        # Add realistic random walk movement
        change = random.uniform(-0.01, 0.01)  # Smaller changes for smoother progression
        base_probability += change
        
        # Strong directional bias toward the end of the day
        if day_progress > 0.7:  # In the last 30% of the day, bias toward the winner
            if up_wins:
                base_probability += 0.003 * (day_progress - 0.7) * 10  # Accelerating toward 100%
            else:
                base_probability -= 0.003 * (day_progress - 0.7) * 10  # Accelerating toward 0%
        
        # In the final 10% of the day, make it even stronger
        if day_progress > 0.9:
            if up_wins:
                base_probability += 0.01 * (day_progress - 0.9) * 10
            else:
                base_probability -= 0.01 * (day_progress - 0.9) * 10
        
        # Keep within bounds but allow more extreme values near the end
        if day_progress < 0.9:
            base_probability = max(0.1, min(0.9, base_probability))
        else:
            # Allow more extreme values in final 10%
            base_probability = max(0.01, min(0.99, base_probability))
        
        # Add some hourly volatility patterns
        hour_factor = 1 + 0.05 * math.sin((current_dt.hour) * math.pi / 12)
        
        up_prob = base_probability * hour_factor
        down_prob = 1.0 - up_prob
        
        # Normalize to ensure they sum to 1
        total = up_prob + down_prob
        up_prob /= total
        down_prob /= total
        
        ts_str = current_dt.isoformat() + "Z"
        
        points.append({
            "ts": ts_str,
            "up": round(up_prob, 4),
            "down": round(down_prob, 4)
        })
        
        current_dt += timedelta(minutes=1)
    
    return points


def _merge_day_points(target_date: datetime):
    """Combine points from bundled HIST_CSV and our recorded PRICE_LOG, dedup by ts."""
    points = []
    # From original CSV (ignore if missing)
    try:
        hist_points = _parse_hist_csv(target_date)
        points.extend(hist_points)
    except FileNotFoundError:
        pass
    # From our own recorder
    log_points = _parse_price_log_for_day(target_date)
    points.extend(log_points)
    
    # If no real data found, generate synthetic data for testing
    if not points:
        points = _generate_synthetic_day_data(target_date)
    
    # Dedup by ts (prefer later entries by sorting)
    by_ts = {}
    for p in sorted(points, key=lambda x: x["ts"]) :  # type: ignore
        by_ts[p["ts"]] = p
    out = list(by_ts.values())
    out.sort(key=lambda x: x["ts"])  # type: ignore
    return out


@app.get("/api/prices/day")
def prices_for_day(date: str, user: User = Depends(get_current_user)):
    try:
        d = datetime.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    points = _merge_day_points(d)
    return {"date": d.date().isoformat(), "points": points}


# --- Bot control and data ---
def _read_jsonl(path: Path, limit: int = 1000):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    # Filter out trades that didn't actually execute
                    if _is_valid_trade(record):
                        out.append(record)
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    return out[-limit:]

def _is_valid_trade(record):
    """Check if a trade record represents an actual executed trade"""
    resp = record.get("resp", {})
    
    # Skip dry run trades
    if resp.get("dry_run", False):
        return False
    
    # For orders, check if they were actually filled/matched
    if "orderID" in resp:
        status = resp.get("status", "").lower()
        # Only include trades that were matched/filled or explicitly marked as filled
        if status in ["matched", "filled"] or record.get("status") == "filled":
            return True
        # Skip live orders that may not have been filled
        if status == "live":
            return False
    
    # For redemptions, always include them
    if record.get("event", "").upper() == "REDEEM":
        return True
    
    # For older records without detailed status, be more lenient
    # but still exclude obvious dry runs
    if not resp or "dry_run" not in resp:
        return True
    
    return False


@app.get("/api/accounts")
def list_accounts(user: User = Depends(get_current_user)):
    # Users only see own account; admins see union of user names and ledger folders
    if not user.is_admin:
        return {"accounts": [user.username] if (STATE / user.username).exists() or True else []}

    names = set()
    # Folders under state
    for d in STATE.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        if name.lower() in {"chart_cache"} or name.startswith(".") or name.startswith("_"):
            continue
        names.add(name)
    # Usernames from users.json (even if no folder yet)
    try:
        db = _load_users()
        for uname in db.keys():
            if isinstance(uname, str) and uname and not uname.startswith("_"):
                names.add(uname)
    except Exception:
        pass
    accounts = sorted(names, key=lambda s: s.lower())
    return {"accounts": accounts}


@app.get("/api/account/{name}/trades")
def account_trades(name: str, user: User = Depends(get_current_user)):
    if not user.is_admin and user.username != name:
        raise HTTPException(status_code=403, detail="Forbidden")
    ledger = STATE / name / "trades.jsonl"
    return {"trades": _read_jsonl(ledger, limit=2000)}


def _active_market_date():
    now = now_et()
    anchor_et = now.astimezone(ET) if now.tzinfo else ET.localize(now)
    return (anchor_et.date() if anchor_et.hour < 12 else (anchor_et + timedelta(days=1)).date())


@app.get("/api/polymarket/chart-image")
async def polymarket_chart_image(slug: Optional[str] = None, w: int = 1000, h: int = 600):
    """Return a PNG screenshot of the Polymarket market page (best-effort cropped to the chart).

    Notes:
    - Requires Playwright to be installed (python -m pip install playwright && playwright install chromium).
    - If Playwright isn't installed, returns a tiny transparent PNG as a graceful fallback.
    - This is best-effort and may change if the upstream site changes its DOM.
    """
    # Resolve slug from active market if not provided
    try:
        if not slug:
            d = _active_market_date()
            _up, _dn, market = resolve_bitcoin_updown_token_ids_for_date(d)
            slug = (market or {}).get("slug")
        if not slug:
            raise HTTPException(status_code=404, detail="No market slug available")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to resolve market slug: {e}")

    # Lazy import Playwright
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception:
        # Return a tiny transparent PNG as a graceful fallback
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDAT\x08\xd7c``\x00\x00\x00\x04\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return StreamingResponse(io.BytesIO(png), media_type="image/png")

    url = f"https://polymarket.com/market/{slug}"
    # Cache key based on slug and dimensions (short TTL: per request for now)
    cache_path = CHART_CACHE / f"{slug.replace('/', '_')}_{w}x{h}.png"
    # Optional: simple cache check (disabled by default as market is live). Uncomment to enable.
    # if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 60:
    #     return StreamingResponse(cache_path.open('rb'), media_type='image/png')

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"], headless=True)
            context = await browser.new_context(viewport={"width": max(w, 1200), "height": max(h, 800)})
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle")
            # Try a few selectors for the chart area
            candidates = [
                "[data-testid='market-chart']",
                "canvas",
                "svg",
            ]
            clip = None
            for sel in candidates:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        box = await el.bounding_box()
                        if box:
                            # Expand slightly and clamp
                            pad = 10
                            clip = {
                                "x": max(0, int(box["x"]) - pad),
                                "y": max(0, int(box["y"]) - pad),
                                "width": int(box["width"]) + 2 * pad,
                                "height": int(box["height"]) + 2 * pad,
                            }
                            break
                except Exception:
                    continue

            if clip:
                png_bytes = await page.screenshot(clip=clip, type="png")
            else:
                # Fallback: viewport screenshot
                png_bytes = await page.screenshot(full_page=False, type="png")

            await context.close()
            await browser.close()

        # Optionally resize/crop to requested w/h could be done via PIL; for now, return raw
        # Save to cache (best-effort)
        try:
            with open(cache_path, "wb") as f:
                f.write(png_bytes)
        except Exception:
            pass

        return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png")
    except HTTPException:
        # Convert to transparent PNG fallback to prevent broken image icon
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDAT\x08\xd7c``\x00\x00\x00\x04\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return StreamingResponse(io.BytesIO(png), media_type="image/png")
    except Exception:
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDAT\x08\xd7c``\x00\x00\x00\x04\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return StreamingResponse(io.BytesIO(png), media_type="image/png")


@app.get("/api/active-market")
def active_market(user: User = Depends(get_current_user)):
    d = _active_market_date()
    up_id, dn_id, market = resolve_bitcoin_updown_token_ids_for_date(d)
    return {"date": d.isoformat(), "up_id": up_id, "down_id": dn_id, "market": market}


@app.get("/api/prices/current")
def current_prices(user: User = Depends(get_current_user)):
    d = _active_market_date()
    up_id, dn_id, market = resolve_bitcoin_updown_token_ids_for_date(d)
    def _px(tid):
        bid = clob_price(tid, "sell")
        ask = clob_price(tid, "buy")
        try:
            bid = float(bid) if bid is not None else None
        except Exception:
            bid = None
        try:
            ask = float(ask) if ask is not None else None
        except Exception:
            ask = None
        # Compute mid robustly; treat 0.0 as valid (avoid truthy checks)
        if (bid is not None) and (ask is not None):
            mid = (bid + ask) / 2.0
        elif ask is not None:
            mid = ask
        else:
            mid = bid
        return {"bid": bid, "ask": ask, "mid": mid}
    up = _px(up_id)
    dn = _px(dn_id)
    return {"ts": datetime.utcnow().isoformat()+"Z", "date": d.isoformat(), "up": up, "down": dn}


def _record_current_prices_once():
    """Fetch current mid prices and append to PRICE_LOG."""
    try:
        d = _active_market_date()
        up_id, dn_id, _ = resolve_bitcoin_updown_token_ids_for_date(d)
        def _mid(t):
            bid = clob_price(t, "sell"); ask = clob_price(t, "buy")
            try: bid = float(bid) if bid is not None else None
            except Exception: bid=None
            try: ask = float(ask) if ask is not None else None
            except Exception: ask=None
            return (bid+ask)/2.0 if (bid and ask) else (ask or bid or 0.0)
        up_mid = _mid(up_id)
        dn_mid = _mid(dn_id)
        ts = datetime.utcnow().replace(microsecond=0).isoformat()+"Z"
        # Keep values bounded (0..1 typical), but store raw float
        _append_price_row(ts, float(up_mid or 0.0), float(dn_mid or 0.0))
    except Exception:
        pass


_recorder_thread: Optional[threading.Thread] = None
_recorder_stop = threading.Event()


def _price_recorder_loop(period_sec: float = 60.0):
    _ensure_price_log_header()
    # Seed: copy last 7 days from HIST_CSV into our log if missing
    try:
        today = datetime.utcnow().date()
        for i in range(1, 8):
            day = today - timedelta(days=i)
            pts = []
            try:
                pts = _parse_hist_csv(datetime(day.year, day.month, day.day))
            except FileNotFoundError:
                pts = []
            if not pts:
                continue
            # Append any missing ts only
            existing = {p["ts"] for p in _parse_price_log_for_day(datetime(day.year, day.month, day.day))}
            for p in pts:
                if p["ts"] not in existing:
                    _append_price_row(p["ts"], float(p.get("up") or 0.0), float(p.get("down") or 0.0))
    except Exception:
        pass
    # Periodic live recording
    while not _recorder_stop.is_set():
        _record_current_prices_once()
        _recorder_stop.wait(period_sec)


@app.on_event("startup")
def _on_startup():
    # Ensure recorder is running even when launched via `uvicorn module:app`
    global _recorder_thread
    _recorder_stop.clear()
    if _recorder_thread is None or not _recorder_thread.is_alive():
        _recorder_thread = threading.Thread(target=_price_recorder_loop, kwargs={"period_sec": 60.0}, daemon=True)
        _recorder_thread.start()
    
    # Initialize stats cache immediately so first load is fast
    try:
        fresh_stats = calculate_fresh_stats()
        STATS_CACHE['data'] = fresh_stats
        STATS_CACHE['last_update'] = time.time()
        print(f"[INFO] Stats cache initialized: {fresh_stats}")
    except Exception as e:
        print(f"[ERROR] Failed to initialize stats cache: {e}")
    
    # No need for async background task - we now use on-demand background updates


@app.on_event("shutdown")
def _on_shutdown():
    _recorder_stop.set()
    try:
        if _recorder_thread and _recorder_thread.is_alive():
            _recorder_thread.join(timeout=2.0)
    except Exception:
        pass


@app.get("/api/settings")
def api_settings(user: User = Depends(get_current_user)):
    return {"default_stake": float(get_default_stake())}


def _position_cost_since(ledger_path: Path, start_dt: datetime):
    lots = {"UP": [], "DOWN": []}
    try:
        with open(ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rts = rec.get("ts")
                try:
                    rdt = datetime.fromisoformat(rts) if rts else None
                except Exception:
                    rdt = None
                if rdt is not None and rdt < start_dt:
                    continue
                ev = str(rec.get("event") or "").upper()
                try:
                    size = float(rec.get("size") or 0.0)
                    price = float(rec.get("price") or 0.0)
                except Exception:
                    size = 0.0; price = 0.0
                if size <= 0:
                    continue
                if ev == "BUY_UP":
                    lots["UP"].append({"size": size, "usd": size * price})
                elif ev == "BUY_DOWN":
                    lots["DOWN"].append({"size": size, "usd": size * price})
                elif ev == "SELL_UP":
                    remaining = size
                    while remaining > 1e-12 and lots["UP"]:
                        lot = lots["UP"][0]
                        take = min(remaining, lot["size"])
                        usd_reduce = lot["usd"] * (take / lot["size"]) if lot["size"] > 0 else 0.0
                        lot["size"] -= take; lot["usd"] -= usd_reduce; remaining -= take
                        if lot["size"] <= 1e-12: lots["UP"].pop(0)
                elif ev == "SELL_DOWN":
                    remaining = size
                    while remaining > 1e-12 and lots["DOWN"]:
                        lot = lots["DOWN"][0]
                        take = min(remaining, lot["size"])
                        usd_reduce = lot["usd"] * (take / lot["size"]) if lot["size"] > 0 else 0.0
                        lot["size"] -= take; lot["usd"] -= usd_reduce; remaining -= take
                        if lot["size"] <= 1e-12: lots["DOWN"].pop(0)
    except FileNotFoundError:
        pass
    up_sz = sum(l["size"] for l in lots["UP"])
    up_usd = sum(l["usd"] for l in lots["UP"])
    dn_sz = sum(l["size"] for l in lots["DOWN"])
    dn_usd = sum(l["usd"] for l in lots["DOWN"])
    return {"UP": {"size": up_sz, "usd_cost": up_usd}, "DOWN": {"size": dn_sz, "usd_cost": dn_usd}}


@app.get("/api/user/summary")
def user_summary(user: User = Depends(get_current_user)):
    # Single-account assumption: username folder
    name = user.username
    ledger = STATE / name / "trades.jsonl"
    start = today_noon_et(now_et())
    cost = _position_cost_since(ledger, start)
    d = _active_market_date()
    up_id, dn_id, _ = resolve_bitcoin_updown_token_ids_for_date(d)
    def _mid(t):
        bid = clob_price(t, "sell"); ask = clob_price(t, "buy")
        try: bid = float(bid) if bid is not None else None
        except Exception: bid=None
        try: ask = float(ask) if ask is not None else None
        except Exception: ask=None
        return (bid+ask)/2.0 if (bid and ask) else (ask or bid or 0.0)
    up_mid = _mid(up_id); dn_mid = _mid(dn_id)
    up_sz = cost["UP"]["size"]; dn_sz = cost["DOWN"]["size"]
    up_val = up_sz * up_mid; dn_val = dn_sz * dn_mid
    
    # Get base profit from balance_history.json instead of hardcoded value
    base_profit = 0.0
    if name.lower() == 'founders':
        try:
            import json
            from pathlib import Path
            balance_history_path = Path(__file__).parent.parent / "state" / "balance_history.json"
            if balance_history_path.exists():
                with open(balance_history_path, 'r') as f:
                    balance_history = json.load(f)
                base_profit = balance_history.get('starting_balances', {}).get('Founders', {}).get('transferred_profit_from_previous_account', 0.0)
                print(f"  Loaded base profit from balance_history.json: ${base_profit:.2f}")
            else:
                # Fallback to environment variable if file doesn't exist
                base_profit = float(os.environ.get("ORIGINAL_ACCOUNT_PROFIT_BASE", "10.36"))
                print(f"  Using fallback base profit: ${base_profit:.2f}")
        except Exception as e:
            print(f"  Error loading base profit, using fallback: {e}")
            base_profit = float(os.environ.get("ORIGINAL_ACCOUNT_PROFIT_BASE", "10.36"))
    
    realized = unrealized = total_pnl = 0.0
    holdings_value = None
    address = None
    try:
        from balance_checker import load_users_config, get_data_api_positions_pnl
        users_cfg = load_users_config()
        acct_cfg = users_cfg.get('_trading_config', {}).get('accounts', [])
        target_priv = None
        
        # Find the account config for this user
        for a in acct_cfg:
            if a.get('name','').lower() == name.lower():
                target_priv = a.get('private_key')
                # Get main wallet address (what shows in Polymarket profile)
                if target_priv and target_priv.startswith('0x') and 'REPLACE_WITH_TEST' not in target_priv:
                    try:
                        address = Account.from_key(target_priv).address
                    except Exception:
                        address = a.get('proxy_wallet') or a.get('address')
                else:
                    address = a.get('proxy_wallet') or a.get('address')
                break
        
        if address:
            # Try authenticated API first for more accurate data
            if target_priv and target_priv.startswith('0x') and 'REPLACE_WITH_TEST' not in target_priv:
                try:
                    from balance_checker import get_polymarket_account_stats_authenticated
                    auth_stats = get_polymarket_account_stats_authenticated(address, target_priv)
                    if auth_stats.get('api_success', False):
                        realized = float(auth_stats.get('realized_pnl', 0.0) or 0.0)
                        total_pnl = float(auth_stats.get('total_pnl', realized) or 0.0)
                        holdings_value = float(auth_stats.get('total_portfolio_value', 0.0) or 0.0)
                        unrealized = total_pnl - realized
                    else:
                        # Fall back to Data API
                        dp = get_data_api_positions_pnl(address)
                        realized = float(dp.get('realized_pnl', 0.0) or 0.0)
                        total_pnl = float(dp.get('cash_pnl', realized) or 0.0)
                        holdings_value = float(dp.get('current_value', 0.0) or 0.0)
                        unrealized = total_pnl - realized
                except Exception:
                    # Fall back to Data API
                    dp = get_data_api_positions_pnl(address)
                    realized = float(dp.get('realized_pnl', 0.0) or 0.0)
                    total_pnl = float(dp.get('cash_pnl', realized) or 0.0)
                    holdings_value = float(dp.get('current_value', 0.0) or 0.0)
                    unrealized = total_pnl - realized
            else:
                # Use Data API only
                dp = get_data_api_positions_pnl(address)
                realized = float(dp.get('realized_pnl', 0.0) or 0.0)
                total_pnl = float(dp.get('cash_pnl', realized) or 0.0)
                holdings_value = float(dp.get('current_value', 0.0) or 0.0)
                unrealized = total_pnl - realized
    except Exception as e:
        print(f"Error user summary polymarket pnl: {e}")
    total_profit = total_pnl + base_profit
    return {
        "date": d.isoformat(),
        "positions": {"UP": cost["UP"], "DOWN": cost["DOWN"]},
        "mark": {"UP": up_val, "DOWN": dn_val},
        "address": address,
        "realized_profit": realized,
        "unrealized_profit": unrealized,
        "total_pnl": total_pnl,
        "holdings_value": holdings_value,
        "base_profit": base_profit,
        "total_profit": total_profit,
    }


@app.get("/api/history/daily")
def daily_history(user: User = Depends(get_current_user)):
    # Show previous days with actual cash flow P&L (money in vs money out per day)
    from collections import defaultdict
    name = user.username if not user.is_admin else None
    folders = [STATE / name] if name else [d for d in STATE.iterdir() if d.is_dir()]
    
    days = defaultdict(lambda: {"cash_out": 0.0, "cash_in": 0.0, "redemptions": 0.0, "trades": 0})
    
    for d in folders:
        ledger = d / "trades.jsonl"
        recs = _read_jsonl(ledger, limit=5000)
        
        for r in recs:
            ev = str(r.get("event") or "").upper()
            ts = r.get("ts")
            sz = float(r.get("size") or 0.0)
            px = float(r.get("price") or 0.0)
            
            try:
                day = (datetime.fromisoformat(ts)).date().isoformat()
            except Exception:
                continue
            
            if ev.startswith("BUY_"):
                # Money going out (cost)
                days[day]["cash_out"] += sz * px
                days[day]["trades"] += 1
                
            elif ev.startswith("SELL_"):
                # Money coming in (revenue)
                days[day]["cash_in"] += sz * px
                days[day]["trades"] += 1
                
            elif ev == "REDEEM":
                # For redemptions, we need to estimate the value
                # Winning shares are worth $1 each, but we don't always have the exact count
                # Look at recent position to estimate
                index_set = r.get("indexSet")
                if str(index_set) in ["1", "2"]:
                    # This is a rough estimate - you might want to track this more precisely
                    # For now, assume redemptions are worth about the same as recent buy amounts
                    estimated_value = max(days[day]["cash_out"], 10.0)  # At least $10 estimated
                    days[day]["redemptions"] += estimated_value
    
    # Calculate daily P&L percentages
    out = []
    for day, data in sorted(days.items(), reverse=True):  # Most recent first
        if data["trades"] == 0:
            continue
            
        total_out = data["cash_out"]  # Money spent buying
        total_in = data["cash_in"] + data["redemptions"]  # Money received from selling + redemptions
        
        if total_out > 0:
            # Calculate P&L percentage: (money_in - money_out) / money_out * 100
            pnl_pct = ((total_in - total_out) / total_out) * 100
            
            # If we have redemptions (winning), cap at 100% since that's doubling money
            if data["redemptions"] > 0:
                display_pnl = min(100.0, pnl_pct)
            else:
                # Normal trading P&L
                display_pnl = pnl_pct
                
        elif total_in > 0 and total_out == 0:
            # We got money but didn't spend any (selling positions from previous days)
            # This should show as a loss since we're selling at lower prices
            # Estimate that we originally paid about 10x what we're selling for
            estimated_original_cost = total_in * 10  # Rough estimate
            pnl_pct = ((total_in - estimated_original_cost) / estimated_original_cost) * 100
            display_pnl = pnl_pct
        else:
            display_pnl = 0.0
        
        out.append({
            "date": day,
            "pnl_pct": round(display_pnl, 2),
            "trades": data["trades"],
            "cash_out": round(total_out, 2),
            "cash_in": round(total_in, 2),
            "net": round(total_in - total_out, 2)
        })
    
    return {"days": out}


@app.get("/api/stats/overview")
def stats_overview(user: User = Depends(require_admin)):
    # Very simple PnL: sum of (SELL usd - BUY usd) per day across all ledgers
    from collections import defaultdict
    pnl_by_day = defaultdict(float)
    trades_total = 0
    for d in STATE.iterdir():
        if not d.is_dir():
            continue
        ledger = d / "trades.jsonl"
        recs = _read_jsonl(ledger, limit=10000)
        for r in recs:
            ev = str(r.get("event") or "").upper()
            ts = r.get("ts")
            try:
                day = (datetime.fromisoformat(ts)).date().isoformat()
            except Exception:
                day = "unknown"
            px = float(r.get("price") or 0.0)
            sz = float(r.get("size") or 0.0)
            if ev.startswith("BUY_"):
                pnl_by_day[day] -= px * sz
                trades_total += 1
            elif ev.startswith("SELL_"):
                pnl_by_day[day] += px * sz
                trades_total += 1
    days = sorted(pnl_by_day.keys())
    series = [{"date": d, "pnl": round(pnl_by_day[d], 4)} for d in days]
    return {"series": series, "trades": trades_total}


@app.get("/api/control")
def get_control(user: User = Depends(require_admin)):
    if CONTROL_PATH.exists():
        try:
            with open(CONTROL_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"kill": False, "stake_multiplier": 1.0, "priority_side": None}


@app.post("/api/control")
def set_control(update: ControlUpdate, user: User = Depends(require_admin)):
    cur = {}
    if CONTROL_PATH.exists():
        try:
            with open(CONTROL_PATH, "r", encoding="utf-8") as f:
                cur = json.load(f) or {}
        except Exception:
            cur = {}
    data = cur.copy()
    for k, v in update.dict(exclude_none=True).items():
        data[k] = v
    with open(CONTROL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return {"ok": True, "control": data}


@app.post("/api/command")
def queue_command(cmd: str, user: User = Depends(require_admin)):
    # Append to web_commands.txt for the bot to pick up
    with open(WEB_CMDS_PATH, "a", encoding="utf-8") as f:
        f.write(cmd.strip() + "\n")
    return {"ok": True}


@app.get("/api/bot/status")
def get_bot_status(user: User = Depends(get_current_user)):
    return _load_bot_status()


@app.post("/api/bot/shutdown")
def shutdown_bot(user: User = Depends(require_admin)):
    _shutdown_bot(user.username)
    return {"message": "Bot shutdown initiated", "shutdown_by": user.username}


@app.post("/api/bot/restart")
def restart_bot(user: User = Depends(require_admin)):
    # Reset bot status to running
    status = _load_bot_status()
    status.update({
        "running": True,
        "shutdown_by": None,
        "shutdown_time": None
    })
    _save_bot_status(status)
    return {"message": "Bot status reset to running"}


def _tail_log_file(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    import time
                    time.sleep(0.5)
                    continue
                yield (line)
    except FileNotFoundError:
        yield "\n"


@app.get("/api/logs/stream")
def stream_logs(user: User = Depends(require_admin)):
    # Stream the latest log file
    files = list(LOGS_DIR.glob("*.log"))
    if not files:
        raise HTTPException(status_code=404, detail="No logs")
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return StreamingResponse(_tail_log_file(latest), media_type="text/plain")


# --- Static files and landing ---
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def root():
    index_path = WEB_DIR / "index.html"
    try:
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    except Exception:
        return HTMLResponse("<h1>Volarithm</h1>")


# Serve SPA for these direct paths as well
@app.get("/home", response_class=HTMLResponse)
@app.get("/user", response_class=HTMLResponse)
@app.get("/profile", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
def spa_paths():
    index_path = WEB_DIR / "index.html"
    try:
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    except Exception:
        return HTMLResponse("<h1>Volarithm</h1>")


@app.get("/{full_path:path}", response_class=HTMLResponse)
def spa_catch_all(full_path: str):
    # Allow API and static to 404 normally
    if full_path.startswith("api") or full_path.startswith("static"):
        raise HTTPException(status_code=404, detail="Not Found")
    index_path = WEB_DIR / "index.html"
    try:
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    except Exception:
        return HTMLResponse("<h1>Volarithm</h1>")


def main():
    port = int(os.environ.get("PORT", "8000"))
    # Start background recorder thread before serving
    global _recorder_thread, _recorder_stop
    _recorder_stop.clear()
    if _recorder_thread is None or not _recorder_thread.is_alive():
        _recorder_thread = threading.Thread(target=_price_recorder_loop, kwargs={"period_sec": 60.0}, daemon=True)
        _recorder_thread.start()
    try:
        # Check for SSL certificate files
        ssl_cert = os.environ.get("SSL_CERT_PATH")
        ssl_key = os.environ.get("SSL_KEY_PATH")
        
        if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
            print(f"Starting HTTPS server on https://10.0.0.147:{port}")
            uvicorn.run(app, host="10.0.0.147", port=port, reload=False, 
                       ssl_keyfile=ssl_key, ssl_certfile=ssl_cert)
        else:
            print(f"Starting HTTP server on http://10.0.0.147:{port}")
            print("To enable HTTPS, set SSL_CERT_PATH and SSL_KEY_PATH environment variables")
            uvicorn.run(app, host="10.0.0.147", port=port, reload=False)
    finally:
        _recorder_stop.set()
        try:
            if _recorder_thread and _recorder_thread.is_alive():
                _recorder_thread.join(timeout=2.0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
