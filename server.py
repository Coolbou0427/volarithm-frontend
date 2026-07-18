import uvicorn
from fastapi import FastAPI, Depends, HTTPException, status, Request, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware
from passlib.context import CryptContext
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import secrets
import os
import json
import tempfile
import base64
import hashlib
import re
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
from eth_utils import to_checksum_address
from Crypto.Cipher import AES

# Windows/Python 3.13: avoid noisy Proactor transport reset tracebacks on client disconnects.
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    except Exception:
        pass
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
from market_trading import make_client  # validate session-key auth against CLOB
from trade_ledger import is_confirmed_trade_record

# Global cache for stats
STATS_CACHE = {
    'data': None,
    'last_update': None,
    'update_interval': 30  # Update every 30 seconds instead of 5 minutes
}

YAHOO_CRUMB_CACHE: Dict[str, Any] = {"crumb": None, "expires_at": 0.0}

# Cache for /api/prices/current — new data points are recorded every 60s so
# there is no benefit in hitting the CLOB API more often than that.
_CURRENT_PRICES_CACHE: Dict[str, Any] = {"data": None, "ts": 0.0}
_CURRENT_PRICES_TTL = 55.0  # seconds


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
SESSION_MASTER_KEY_PATH = STATE / "session_keys_master.bin"

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


class TradingAccountCreate(BaseModel):
    name: str
    enabled: bool = False
    priority: int = 0
    funder: str
    signature_type: Optional[int] = None
    auth_mode: Optional[str] = None
    wallet_owner: Optional[str] = None
    safe_address: Optional[str] = None
    private_key: Optional[str] = None
    trading_session_key: Optional[str] = None
    fee_session_key: Optional[str] = None


class TradingAccountPatch(BaseModel):
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    funder: Optional[str] = None
    signature_type: Optional[int] = None
    auth_mode: Optional[str] = None
    is_admin: Optional[bool] = None
    wallet_owner: Optional[str] = None
    safe_address: Optional[str] = None
    clear_funder: Optional[bool] = False
    private_key: Optional[str] = None
    clear_private_key: Optional[bool] = False
    trading_session_key: Optional[str] = None
    fee_session_key: Optional[str] = None


class TradingReorderRequest(BaseModel):
    ordered_names: List[str]


class UserTradingAccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    funder: Optional[str] = None
    signature_type: Optional[int] = None
    auth_mode: Optional[str] = None
    wallet_owner: Optional[str] = None
    safe_address: Optional[str] = None
    clear_funder: Optional[bool] = False
    private_key: Optional[str] = None
    clear_private_key: Optional[bool] = False
    trading_session_key: Optional[str] = None
    fee_session_key: Optional[str] = None


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
        dir_ = str(TOKENS_PATH.parent)
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
            os.replace(tmp_path, TOKENS_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
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
        pk = _decrypt_session_key(acct.get('private_key'))
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
            if not _is_account_enabled(acct):
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
            if not _is_account_enabled(acct):
                continue
            # Query both main wallet (from private key) and proxy wallet if different
            addresses = []
            
            # Main wallet (what shows in Polymarket profile)
            pk = _decrypt_session_key(acct.get('private_key'))
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
            if not _is_account_enabled(acct):
                continue
            # Use same multi-address logic for CLOB API
            addresses = []
            pk = _decrypt_session_key(acct.get('private_key'))
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
                if not _is_account_enabled(acct):
                    continue
                addr = _addr(acct)
                if not addr or addr.lower() in seen:
                    continue
                seen.add(addr.lower())
                pk = _decrypt_session_key(acct.get('private_key'))
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


HEX_32B_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
ETH_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ENC_PREFIX = "enc:v1:"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict):
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        # Common on Windows when browser aborts a socket; safe to ignore.
        return
    loop.default_exception_handler(context)


def _normalize_private_key(value: str) -> str:
    v = str(value or "").strip()
    if not HEX_32B_RE.match(v):
        raise HTTPException(status_code=400, detail="Invalid key format: expected 32-byte hex private key")
    if not v.startswith("0x"):
        v = "0x" + v
    return v.lower()


def _generate_private_key_hex() -> str:
    return "0x" + secrets.token_hex(32)


def _clob_api_base() -> str:
    return os.environ.get("CLOB_API_BASE", "https://clob.polymarket.com").strip() or "https://clob.polymarket.com"


def _chain_id_default() -> int:
    txt = os.environ.get("CHAIN_ID", "137").strip()
    try:
        return int(txt)
    except Exception:
        return 137


def _safe_tx_hosts_for_chain(chain_id: int) -> List[str]:
    host_map = {
        1: ["https://api.safe.global/tx-service/eth", "https://safe-transaction-mainnet.safe.global"],
        10: ["https://api.safe.global/tx-service/oeth", "https://safe-transaction-optimism.safe.global"],
        56: ["https://api.safe.global/tx-service/bnb", "https://safe-transaction-bsc.safe.global"],
        100: ["https://api.safe.global/tx-service/gno", "https://safe-transaction-gnosis-chain.safe.global"],
        137: ["https://api.safe.global/tx-service/pol", "https://safe-transaction-polygon.safe.global"],
        42161: ["https://api.safe.global/tx-service/arb1", "https://safe-transaction-arbitrum.safe.global"],
        43114: ["https://api.safe.global/tx-service/avax", "https://safe-transaction-avalanche.safe.global"],
        8453: ["https://api.safe.global/tx-service/base", "https://safe-transaction-base.safe.global"],
    }
    preferred = host_map.get(chain_id, [])
    all_hosts = []
    for values in host_map.values():
        for h in values:
            if h not in all_hosts:
                all_hosts.append(h)
    return preferred + [h for h in all_hosts if h not in preferred]


def _safe_client_gateway_base() -> str:
    return os.environ.get("SAFE_CLIENT_GATEWAY_BASE", "https://safe-client.safe.global").strip() or "https://safe-client.safe.global"


def _polygon_rpc_url() -> str:
    return os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com").strip() or "https://polygon-rpc.com"


def _yahoo_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


def _parse_json_list_maybe(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return None
    return None


def _daily_btc_slug(target_date, include_year: bool = False) -> str:
    base = f"bitcoin-up-or-down-on-{target_date.strftime('%B').lower()}-{target_date.day}"
    return f"{base}-{target_date.year}" if include_year else base


def _market_end_year(market: Optional[dict]) -> Optional[int]:
    if not isinstance(market, dict):
        return None
    end_txt = market.get("endDate") or market.get("end_date") or market.get("endDateIso")
    if not end_txt:
        return None
    try:
        # Handles both ISO datetime and date-only values.
        dt = datetime.fromisoformat(str(end_txt).replace("Z", "+00:00"))
        return dt.year
    except Exception:
        try:
            return int(str(end_txt)[:4])
        except Exception:
            return None


def _extract_up_down_ids_from_market(market: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(market, dict):
        return None, None
    raw_tokens = (
        market.get("clobTokenIds")
        or market.get("clob_token_ids")
        or market.get("tokenIds")
        or market.get("token_ids")
        or market.get("tokens")
    )
    tokens = _parse_json_list_maybe(raw_tokens) or (raw_tokens if isinstance(raw_tokens, list) else None) or []
    tokens = [str(t) for t in tokens if t is not None]
    if len(tokens) < 2:
        return None, None

    raw_outcomes = market.get("outcomes") or market.get("outcomeNames") or market.get("outcome_names")
    outcomes = _parse_json_list_maybe(raw_outcomes) or (raw_outcomes if isinstance(raw_outcomes, list) else None) or []
    outcomes = [str(o or "").strip().upper() for o in outcomes]
    if len(outcomes) >= 2:
        idx_up = next((i for i, v in enumerate(outcomes) if v == "UP"), None)
        idx_dn = next((i for i, v in enumerate(outcomes) if v == "DOWN"), None)
        if idx_up is not None and idx_dn is not None and idx_up < len(tokens) and idx_dn < len(tokens):
            return tokens[idx_up], tokens[idx_dn]
    return tokens[0], tokens[1]


def _fetch_gamma_market_by_slug(slug: str) -> Optional[dict]:
    slug = str(slug or "").strip()
    if not slug:
        return None
    try:
        headers = {"Accept": "application/json", "User-Agent": _yahoo_user_agent()}
        resp = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=6.5, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json() if resp.content else []
        markets = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(markets, list) or not markets:
            return None
        exact = [m for m in markets if isinstance(m, dict) and str(m.get("slug") or "").strip() == slug]
        pool = exact if exact else [m for m in markets if isinstance(m, dict)]
        if not pool:
            return None
        # Prefer currently tradeable rows first.
        pool.sort(key=lambda m: (
            0 if not bool(m.get("closed")) else 1,
            0 if bool(m.get("acceptingOrders")) else 1,
        ))
        return pool[0]
    except Exception:
        return None


def _resolve_market_for_date(target_date):
    """
    Resolve market for a specific day, preferring the year-specific slug so we do not
    accidentally pick a same month/day market from a previous year.
    """
    # Try existing resolver first.
    up_id = dn_id = None
    market = None
    try:
        up_id, dn_id, market = resolve_bitcoin_updown_token_ids_for_date(target_date)
    except Exception:
        up_id = dn_id = None
        market = None

    end_year = _market_end_year(market)
    if up_id and dn_id and end_year == target_date.year:
        return up_id, dn_id, market

    # Fallback to Gamma with year-specific slug.
    year_slug = _daily_btc_slug(target_date, include_year=True)
    gamma_market = _fetch_gamma_market_by_slug(year_slug)
    g_up, g_dn = _extract_up_down_ids_from_market(gamma_market)
    if g_up and g_dn:
        return g_up, g_dn, gamma_market

    # Last fallback: non-year slug from this codebase.
    base_slug = _daily_btc_slug(target_date, include_year=False)
    gamma_market2 = _fetch_gamma_market_by_slug(base_slug)
    g2_up, g2_dn = _extract_up_down_ids_from_market(gamma_market2)
    if g2_up and g2_dn:
        return g2_up, g2_dn, gamma_market2

    # Return whatever original resolver gave (even if stale) to preserve behavior.
    if up_id and dn_id:
        return up_id, dn_id, market
    raise RuntimeError(f"Could not resolve market for {target_date.isoformat()}")


def _yahoo_get_crumb(session: requests.Session) -> Optional[str]:
    now = time.time()
    cached = YAHOO_CRUMB_CACHE.get("crumb")
    exp = float(YAHOO_CRUMB_CACHE.get("expires_at") or 0.0)
    if isinstance(cached, str) and cached and now < exp:
        return cached
    try:
        session.get("https://finance.yahoo.com/", timeout=4.5, headers={"User-Agent": _yahoo_user_agent()})
        r = session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            timeout=4.5,
            headers={"User-Agent": _yahoo_user_agent(), "Accept": "text/plain"},
        )
        if r.status_code == 200:
            crumb = (r.text or "").strip()
            if crumb and " " not in crumb and "<" not in crumb:
                YAHOO_CRUMB_CACHE["crumb"] = crumb
                YAHOO_CRUMB_CACHE["expires_at"] = now + 15 * 60
                return crumb
    except Exception:
        return None
    return None


def _polygon_usdc_balance(safe_addr: Optional[str]) -> tuple[Optional[float], Dict[str, float]]:
    if not isinstance(safe_addr, str) or not ETH_ADDR_RE.match(safe_addr):
        return None, {}
    # Native USDC and legacy USDC.e on Polygon
    token_map = {
        "USDC": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        "USDC.e": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    }
    rpc = _polygon_rpc_url()
    owner_hex = safe_addr.lower().replace("0x", "")
    data = "0x70a08231" + ("0" * 24) + owner_hex
    total = 0.0
    per_token: Dict[str, float] = {}
    for sym, token in token_map.items():
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": token, "data": data},
                "latest",
            ],
        }
        try:
            resp = requests.post(rpc, json=body, timeout=8)
            if resp.status_code != 200:
                continue
            payload = resp.json() if resp.content else {}
            raw = payload.get("result")
            if not isinstance(raw, str) or not raw.startswith("0x"):
                continue
            bal = int(raw, 16) / 1_000_000.0  # USDC is 6 decimals
            if bal > 0:
                per_token[sym] = bal
                total += bal
        except Exception:
            continue
    return total, per_token


def _safe_owners_from_address(safe_addr: Optional[str], chain_id: Optional[int] = None) -> List[str]:
    if not isinstance(safe_addr, str) or not ETH_ADDR_RE.match(safe_addr):
        return []
    cid = int(chain_id if chain_id is not None else _chain_id_default())
    owners: List[str] = []
    seen = set()

    def _add_owner(v: Optional[str]):
        if not isinstance(v, str):
            return
        o = v.strip()
        if not ETH_ADDR_RE.match(o):
            return
        key = o.lower()
        if key in seen:
            return
        seen.add(key)
        owners.append(o)

    def _collect(url: str):
        try:
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                return
            data = resp.json() if resp.content else {}
            # Safe client gateway can nest owners under "owners"
            if isinstance(data, dict):
                olist = data.get("owners")
                if isinstance(olist, list):
                    for item in olist:
                        if isinstance(item, str):
                            _add_owner(item)
                        elif isinstance(item, dict):
                            _add_owner(item.get("value") or item.get("address"))
            # Tx service /api/v1/safes/{safe}/ has owners as list[str]
            if isinstance(data, dict):
                olist2 = data.get("owners")
                if isinstance(olist2, list):
                    for item in olist2:
                        if isinstance(item, str):
                            _add_owner(item)
        except Exception:
            return

    gateway = _safe_client_gateway_base().rstrip("/")
    for url in [
        f"{gateway}/v1/chains/{cid}/safes/{safe_addr}",
        f"{gateway}/v2/chains/{cid}/safes/{safe_addr}",
    ]:
        _collect(url)

    for host in _safe_tx_hosts_for_chain(cid):
        _collect(f"{host}/api/v1/safes/{safe_addr}/")

    return owners


def _normalize_eth_address(value: str, field_name: str = "address") -> str:
    v = str(value or "").strip()
    if not ETH_ADDR_RE.match(v):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}: expected 0x-prefixed 20-byte hex address")
    return v


def _checksum_eth_address(value: str) -> str:
    try:
        return to_checksum_address(value)
    except Exception:
        return value


def _session_master_key() -> Optional[bytes]:
    raw = os.environ.get("TRADING_KEYS_MASTER_KEY") or os.environ.get("SESSION_KEYS_MASTER_KEY")
    if raw:
        txt = raw.strip()
        if len(txt) == 64 and all(c in "0123456789abcdefABCDEF" for c in txt):
            return bytes.fromhex(txt)
        try:
            dec = base64.b64decode(txt, validate=True)
            if len(dec) == 32:
                return dec
        except Exception:
            pass
        return hashlib.sha256(txt.encode("utf-8")).digest()

    # Local fallback: persist a random 32-byte key on disk for this server instance.
    # This keeps encryption working even if env vars are not configured.
    try:
        if SESSION_MASTER_KEY_PATH.exists():
            data = SESSION_MASTER_KEY_PATH.read_bytes()
            if len(data) == 32:
                return data
        mk = secrets.token_bytes(32)
        SESSION_MASTER_KEY_PATH.write_bytes(mk)
        return mk
    except Exception:
        return None


def _encrypt_session_key(raw_key: str) -> str:
    mk = _session_master_key()
    if not mk:
        raise HTTPException(status_code=500, detail="Session-key encryption is not configured on server")
    nonce = secrets.token_bytes(12)
    cipher = AES.new(mk, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(raw_key.encode("utf-8"))
    blob = base64.b64encode(nonce + tag + ct).decode("ascii")
    return ENC_PREFIX + blob


def _decrypt_session_key(stored: Optional[str]) -> Optional[str]:
    if not isinstance(stored, str) or not stored:
        return None
    if not stored.startswith(ENC_PREFIX):
        return stored
    mk = _session_master_key()
    if not mk:
        return None
    try:
        data = base64.b64decode(stored[len(ENC_PREFIX):].encode("ascii"))
        nonce, tag, ct = data[:12], data[12:28], data[28:]
        cipher = AES.new(mk, AES.MODE_GCM, nonce=nonce)
        plain = cipher.decrypt_and_verify(ct, tag)
        return plain.decode("utf-8")
    except Exception:
        return None


def _masked_preview(stored_key: Optional[str]) -> Optional[str]:
    plain = _decrypt_session_key(stored_key)
    if not isinstance(plain, str):
        return None
    v = plain if plain.startswith("0x") else ("0x" + plain)
    if len(v) < 10:
        return None
    return f"{v[:6]}...{v[-4:]}"


def _verify_trading_session_key_for_auth(raw_key: str, funder: str, signature_type: int) -> tuple[bool, str]:
    """Best-effort CLOB auth probe for trading key + funder/signature context."""
    try:
        client = make_client(
            _clob_api_base(),
            raw_key,
            _chain_id_default(),
            proxy_wallet=funder,
            signature_type=signature_type,
        )
    except Exception as e:
        return False, f"client init failed: {e}"

    # Probe an authenticated endpoint. If this fails, key/context is not usable yet.
    try:
        try:
            client.get_orders(status="open")
        except TypeError:
            client.get_orders()
        return True, "ok"
    except Exception as e:
        return False, f"auth probe failed: {e}"


def _trading_accounts_ref(db: dict) -> List[dict]:
    cfg = db.setdefault("_trading_config", {})
    accts = cfg.setdefault("accounts", [])
    if not isinstance(accts, list):
        cfg["accounts"] = []
        accts = cfg["accounts"]
    return accts


def _find_trading_account(accounts: List[dict], name: str) -> Optional[dict]:
    for acct in accounts:
        if str(acct.get("name")) == name:
            return acct
    return None


def _upsert_user_trading_account(db: dict, username: str) -> dict:
    accounts = _trading_accounts_ref(db)
    acct = _find_trading_account(accounts, username)
    if acct:
        _sync_account_enable_flags(acct)
        return acct
    acct = {
        "name": username,
        "enabled": False,
        "user_enabled": False,
        "admin_enabled": False,
        "priority": 0,
        "signature_type": 2,
        "trading_session_verified": False,
        "key_version": 1,
    }
    accounts.append(acct)
    return acct


def _sync_account_enable_flags(acct: dict) -> None:
    """
    Keep legacy `enabled` and split flags in sync.
    Effective trading permission is user_enabled AND admin_enabled.
    """
    legacy_enabled = bool(acct.get("enabled", True))
    if "user_enabled" not in acct:
        acct["user_enabled"] = legacy_enabled
    if "admin_enabled" not in acct:
        acct["admin_enabled"] = legacy_enabled
    acct["enabled"] = bool(acct.get("user_enabled", False)) and bool(acct.get("admin_enabled", False))


def _is_account_enabled(acct: dict) -> bool:
    _sync_account_enable_flags(acct)
    return bool(acct.get("enabled", False))


def _is_admin_or_founders_account(db: dict, name: Optional[str]) -> bool:
    n = str(name or "").strip()
    if not n:
        return False
    if n.lower() == "founders":
        return True
    row = db.get(n)
    return bool(isinstance(row, dict) and row.get("is_admin", False))


def _derive_signer_address(acct: dict, update_key: Optional[str] = None) -> Optional[str]:
    plain = update_key
    if not plain:
        plain = (
            _decrypt_session_key(acct.get("trading_session_key"))
            or _decrypt_session_key(acct.get("session_key"))
            or _decrypt_session_key(acct.get("private_key"))
        )
    if not isinstance(plain, str):
        return None
    try:
        return Account.from_key(plain).address
    except Exception:
        return None


def _normalize_auth_mode(value: Optional[str]) -> str:
    mode = str(value or "session_key").strip().lower().replace("-", "_")
    if mode in {"session", "session_keys", "session_key"}:
        return "session_key"
    if mode in {"private", "private_key", "wallet_private_key"}:
        return "private_key"
    raise HTTPException(status_code=400, detail="auth_mode must be session_key or private_key")


def _account_auth_mode(acct: dict) -> str:
    return _normalize_auth_mode(acct.get("auth_mode") or "session_key")


def _private_key_address(stored_key: Optional[str]) -> Optional[str]:
    plain = _decrypt_session_key(stored_key)
    if not isinstance(plain, str):
        return None
    try:
        return Account.from_key(plain).address
    except Exception:
        return None


def _key_expired(expires_at_iso: Optional[str]) -> bool:
    """Return True if the given ISO expiry timestamp is in the past."""
    if not expires_at_iso:
        return False
    try:
        exp = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > exp
    except Exception:
        return False


def _sanitize_trading_account(acct: dict) -> dict:
    _sync_account_enable_flags(acct)
    auth_mode = _account_auth_mode(acct)
    trading_raw = acct.get("trading_session_key") or acct.get("session_key")
    fee_raw = acct.get("fee_session_key") or acct.get("fees_session_key") or acct.get("fee_key")
    private_raw = acct.get("private_key")
    trading_expired = bool(trading_raw) and _key_expired(acct.get("trading_session_expires_at"))
    fee_expired = bool(fee_raw) and _key_expired(acct.get("fee_session_expires_at"))
    has_private_key = bool(_decrypt_session_key(private_raw))
    return {
        "name": acct.get("name"),
        "enabled": bool(acct.get("enabled", False)),
        "user_enabled": bool(acct.get("user_enabled", False)),
        "admin_enabled": bool(acct.get("admin_enabled", False)),
        "admin_locked": not bool(acct.get("admin_enabled", False)),
        "priority": int(acct.get("priority", 0) or 0),
        "funder": acct.get("funder") or acct.get("proxy_wallet"),
        "wallet_owner": acct.get("wallet_owner"),
        "safe_address": acct.get("safe_address"),
        "signature_type": acct.get("signature_type"),
        "auth_mode": auth_mode,
        "has_private_key": has_private_key,
        "has_active_trading_key": has_private_key if auth_mode == "private_key" else bool(trading_raw),
        "has_trading_session_key": bool(trading_raw),
        "has_fee_session_key": bool(fee_raw),
        "trading_key_expired": trading_expired,
        "fee_key_expired": fee_expired,
        "trading_session_key_preview": _masked_preview(trading_raw),
        "fee_session_key_preview": _masked_preview(fee_raw),
        "trading_session_updated_at": acct.get("trading_session_updated_at"),
        "trading_session_expires_at": acct.get("trading_session_expires_at"),
        "trading_session_verified": bool(acct.get("trading_session_verified", False)),
        "trading_session_verified_at": acct.get("trading_session_verified_at"),
        "trading_session_verification_error": acct.get("trading_session_verification_error"),
        "fee_session_updated_at": acct.get("fee_session_updated_at"),
        "fee_session_expires_at": acct.get("fee_session_expires_at"),
        "key_version": acct.get("key_version", 1),
    }


def verify_password(plain, hashed) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def hash_password(plain) -> str:
    return pwd_context.hash(plain)


def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    info = TOKENS.get(token)
    if not info:
        # Fallback to on-disk tokens (survive server restarts)
        disk = _load_tokens_file()
        if disk:
            TOKENS.update(disk)
            info = TOKENS.get(token)
    if not info:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    # Expiry check
    if info.get("exp") and datetime.utcnow() > info["exp"]:
        TOKENS.pop(token, None)
        _save_tokens_file(TOKENS)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
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


def _validate_username_rules(username: str) -> Optional[str]:
    candidate = str(username or "").strip()
    if len(candidate) < 8:
        return "Username must be at least 8 characters"
    if not re.fullmatch(r"[A-Za-z0-9_]+", candidate):
        return "Username can only use letters, numbers, and underscores"
    return None


def _validate_password_rules(password: str) -> Optional[str]:
    value = str(password or "")
    if len(value) < 10:
        return "Weak password: min 10 chars, mix of letters and numbers"
    if len(value) > 20:
        return "Password must be 20 characters or fewer"
    if not any(c.isdigit() for c in value) or not any(c.isalpha() for c in value):
        return "Weak password: min 10 chars, mix of letters and numbers"
    return None


# --- Auth endpoints ---
@app.post("/api/signup")
def signup(new_user: UserCreate):
    db = _load_users()
    username_norm = str(new_user.username or "").strip()
    rule_error = _validate_username_rules(username_norm)
    if rule_error:
        raise HTTPException(status_code=400, detail=rule_error)
    if any(str(name).lower() == username_norm.lower() for name in db.keys()):
        raise HTTPException(status_code=400, detail="Username unavailable")
    
    # Validate password strength
    password_error = _validate_password_rules(new_user.password)
    if password_error:
        raise HTTPException(status_code=400, detail=password_error)
    # First user becomes admin automatically
    is_first_user = len(db) == 0
    db[username_norm] = {
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
    return {
        "username": user.username,
        "paused": bool(row.get("paused", False)),
        "timezone": row.get("timezone") or "America/Los_Angeles",
        "has_private_key": bool(pk),
    }


@app.get("/api/username-availability")
@app.get("/api/user/username-availability")
def username_availability(username: str, user: User = Depends(get_current_user)):
    username_norm = str(username or "").strip()
    rule_error = _validate_username_rules(username_norm)
    if rule_error:
        return {"available": False, "reason": rule_error}

    current_username = str(user.username or "").strip()
    if username_norm.lower() == current_username.lower():
        return {"available": True, "is_current": True}

    db = _load_users()
    taken = any(str(name).lower() == username_norm.lower() for name in db.keys())
    if taken:
        return {"available": False, "reason": "Username unavailable"}
    return {"available": True}


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
        new_username = str(update.new_username).strip()
        rule_error = _validate_username_rules(new_username)
        if rule_error:
            raise HTTPException(status_code=400, detail=rule_error)
        old_username = user.username
        taken = any(
            str(name).lower() == new_username.lower() and str(name).lower() != str(old_username).lower()
            for name in db.keys()
        )
        if taken:
            raise HTTPException(status_code=400, detail="Username unavailable")
        db[new_username] = row
        db.pop(user.username, None)
        user.username = new_username
        # Keep per-user trading account entry aligned with username changes.
        accounts = _trading_accounts_ref(db)
        acct = _find_trading_account(accounts, old_username)
        if acct:
            acct["name"] = new_username
        # propagate to active token
        if token in TOKENS:
            TOKENS[token]["username"] = new_username
            _save_tokens_file(TOKENS)
    # Change password
    if update.new_password:
        if not update.old_password or not verify_password(update.old_password, row.get("password", "")):
            raise HTTPException(status_code=400, detail="Current password incorrect")
        password_error = _validate_password_rules(update.new_password)
        if password_error:
            raise HTTPException(status_code=400, detail=password_error)
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


@app.get("/api/user/trading-account")
def get_user_trading_account(user: User = Depends(get_current_user)):
    db = _load_users()
    acct = _find_trading_account(_trading_accounts_ref(db), user.username)
    if not acct:
        return {
            "exists": False,
            "account": {
                "name": user.username,
                "enabled": False,
                "user_enabled": False,
                "admin_enabled": False,
                "admin_locked": True,
                "is_admin_or_founders": _is_admin_or_founders_account(db, user.username),
                "requires_fee_key": not _is_admin_or_founders_account(db, user.username),
                "priority": 0,
                "signature_type": 2,
                "auth_mode": "session_key",
                "has_private_key": False,
                "has_active_trading_key": False,
                "has_trading_session_key": False,
                "has_fee_session_key": False,
            },
        }
    out = _sanitize_trading_account(acct)
    privileged = _is_admin_or_founders_account(db, acct.get("name"))
    out["is_admin_or_founders"] = privileged
    out["requires_fee_key"] = not privileged
    return {"exists": True, "account": out}


@app.get("/api/wallet/safes")
def detect_wallet_safes(owner: str, chain_id: Optional[int] = None, user: User = Depends(get_current_user)):
    owner_norm = _checksum_eth_address(_normalize_eth_address(owner, "owner"))
    cid = int(chain_id if chain_id is not None else _chain_id_default())
    found: List[str] = []
    seen = set()

    def _collect_from_url(url: str):
        try:
            resp = requests.get(
                url,
                timeout=2.5,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Volarithm-Web/1.0",
                },
            )
            if resp.status_code != 200:
                return
            data = resp.json() if resp.content else {}
            safes = data.get("safes") if isinstance(data, dict) else None
            if not isinstance(safes, list):
                results = data.get("results") if isinstance(data, dict) else None
                if isinstance(results, list):
                    safes = [r.get("address") for r in results if isinstance(r, dict)]
                else:
                    items = data.get("items") if isinstance(data, dict) else None
                    if isinstance(items, list):
                        safes = [i.get("address") for i in items if isinstance(i, dict)]
                    else:
                        safes = []
            # Some gateway variants return object entries instead of plain strings.
            normalized: List[str] = []
            for s in safes:
                if isinstance(s, str):
                    sv = s.strip()
                elif isinstance(s, dict):
                    sv = str(s.get("address") or s.get("value") or "").strip()
                else:
                    sv = ""
                if not sv or sv.lower() in seen:
                    continue
                seen.add(sv.lower())
                normalized.append(sv)
            if normalized:
                found.extend(normalized)
        except Exception:
            return

    def _lookup_for_chain(chain: int) -> List[str]:
        nonlocal found, seen
        found = []
        seen = set()

        # Fast path first: chain tx-service endpoints.
        for host in _safe_tx_hosts_for_chain(chain)[:2]:
            for u in [
                f"{host}/api/v1/owners/{owner_norm}/safes/",
                f"{host}/api/v1/safes/?owner={owner_norm}",
            ]:
                _collect_from_url(u)
                if found:
                    return found

        # Secondary fallback: Safe Client Gateway patterns.
        gateway = _safe_client_gateway_base().rstrip("/")
        for u in [
            f"{gateway}/v1/chains/{chain}/owners/{owner_norm}/safes",
            f"{gateway}/v2/owners/{owner_norm}/safes?chainIds={chain}",
            f"{gateway}/v2/owners/{owner_norm}/safes",
            f"{gateway}/v1/owners/{owner_norm}/safes",
        ]:
            _collect_from_url(u)
            if found:
                return found
        return []

    checked: List[int] = []
    for chain in [cid, 137, 1]:
        if chain in checked:
            continue
        checked.append(chain)
        got = _lookup_for_chain(chain)
        if got:
            return {
                "owner": owner_norm,
                "chain_id": cid,
                "source_chain": chain,
                "checked_chains": checked,
                "safes": got,
            }

    return {
        "owner": owner_norm,
        "chain_id": cid,
        "source_chain": None,
        "checked_chains": checked,
        "safes": [],
    }


@app.post("/api/user/trading-account")
@app.patch("/api/user/trading-account")
@app.put("/api/user/trading-account")
def update_user_trading_account(payload: UserTradingAccountUpdate, user: User = Depends(get_current_user)):
    db = _load_users()
    acct = _upsert_user_trading_account(db, user.username)
    _sync_account_enable_flags(acct)
    privileged = _is_admin_or_founders_account(db, acct.get("name"))

    if payload.clear_funder:
        acct.pop("funder", None)
        acct.pop("wallet_owner", None)
        acct.pop("safe_address", None)
    if payload.clear_private_key:
        acct.pop("private_key", None)
    if payload.auth_mode is not None:
        acct["auth_mode"] = _normalize_auth_mode(payload.auth_mode)

    if payload.enabled is not None:
        acct["user_enabled"] = bool(payload.enabled)
    if payload.priority is not None:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Priority is admin-only")
        acct["priority"] = int(payload.priority)
    if payload.funder is not None:
        acct["funder"] = _normalize_eth_address(payload.funder, "funder")
    if payload.wallet_owner is not None:
        acct["wallet_owner"] = _normalize_eth_address(payload.wallet_owner, "wallet_owner")
    if payload.safe_address is not None:
        acct["safe_address"] = _normalize_eth_address(payload.safe_address, "safe_address")
    if _account_auth_mode(acct) != "private_key" and payload.wallet_owner is not None and payload.funder is not None:
        if str(acct.get("wallet_owner", "")).lower() == str(acct.get("funder", "")).lower():
            raise HTTPException(status_code=400, detail="Funder cannot be the same as wallet owner EOA. Connect/select a Safe.")

    if payload.private_key is not None:
        normalized_private_key = _normalize_private_key(payload.private_key)
        private_key_address = Account.from_key(normalized_private_key).address
        wallet_owner = acct.get("wallet_owner")
        if isinstance(wallet_owner, str) and wallet_owner and wallet_owner.lower() != private_key_address.lower():
            raise HTTPException(status_code=400, detail="Private key does not match the connected wallet owner")
        acct["private_key"] = _encrypt_session_key(normalized_private_key)
        acct["wallet_owner"] = private_key_address
        if _account_auth_mode(acct) == "private_key":
            acct["funder"] = private_key_address
        acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1

    auth_mode = _account_auth_mode(acct)
    if auth_mode == "private_key":
        private_key_address = _private_key_address(acct.get("private_key"))
        if private_key_address:
            acct["funder"] = private_key_address
            acct["wallet_owner"] = private_key_address
        acct["signature_type"] = 0
    else:
        if payload.signature_type is not None and payload.signature_type != 2:
            raise HTTPException(status_code=400, detail="signature_type is fixed to 2 for profile session-key updates")
        acct["signature_type"] = 2

    if payload.enabled is True:
        if not bool(acct.get("admin_enabled", False)):
            raise HTTPException(status_code=403, detail="Trading is disabled by admin for this account")
        if auth_mode == "private_key":
            if not _private_key_address(acct.get("private_key")):
                raise HTTPException(status_code=400, detail="Private key is required before enabling private-key trading")
        else:
            trading_raw = acct.get("trading_session_key") or acct.get("session_key")
            if not trading_raw:
                raise HTTPException(status_code=400, detail="Trading session key is required before enabling trading")
            if not privileged:
                fee_raw = acct.get("fee_session_key") or acct.get("fees_session_key") or acct.get("fee_key")
                if not fee_raw:
                    raise HTTPException(status_code=400, detail="Fee session key is required before enabling trading")
            if not acct.get("safe_address"):
                raise HTTPException(status_code=400, detail="Safe is required before enabling trading")

    now_iso = _utc_now_iso()
    normalized_trading_key = None
    if payload.trading_session_key is not None:
        normalized_trading_key = _normalize_private_key(payload.trading_session_key)
        funder_for_verify = acct.get("funder")
        if not isinstance(funder_for_verify, str) or not funder_for_verify:
            raise HTTPException(status_code=400, detail="Funder is required before setting trading session key")
        ok, reason = _verify_trading_session_key_for_auth(normalized_trading_key, funder_for_verify, int(acct.get("signature_type", 2) or 2))
        if not ok:
            raise HTTPException(status_code=400, detail=f"Trading session key is not authorized for this funder/signature context ({reason})")
        acct["trading_session_key"] = _encrypt_session_key(normalized_trading_key)
        acct["trading_session_updated_at"] = now_iso
        acct["trading_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1
        acct["trading_session_verified"] = True
        acct["trading_session_verified_at"] = now_iso
        acct["trading_session_verification_error"] = None

    if payload.fee_session_key is not None:
        normalized_fee_key = _normalize_private_key(payload.fee_session_key)
        acct["fee_session_key"] = _encrypt_session_key(normalized_fee_key)
        acct["fee_session_updated_at"] = now_iso
        acct["fee_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1

    funder = acct.get("funder")
    if auth_mode != "private_key" and isinstance(funder, str) and funder:
        signer = _derive_signer_address(acct, normalized_trading_key)
        if (not signer) or signer.lower() != funder.lower():
            acct["signature_type"] = 2

    _sync_account_enable_flags(acct)

    _save_users(db)
    out = _sanitize_trading_account(acct)
    out["is_admin_or_founders"] = privileged
    out["requires_fee_key"] = not privileged
    return {"ok": True, "account": out}


@app.post("/api/user/trading-account/session-keys/{key_type}/generate")
def generate_user_trading_session_key(key_type: str, user: User = Depends(get_current_user)):
    db = _load_users()
    acct = _upsert_user_trading_account(db, user.username)

    kind = (key_type or "").strip().lower()
    if kind not in {"trading", "fee"}:
        raise HTTPException(status_code=400, detail="type must be trading or fee")

    now_iso = _utc_now_iso()
    generated = _generate_private_key_hex()
    if kind == "trading":
        if not acct.get("safe_address"):
            raise HTTPException(status_code=400, detail="Safe is required before setting trading session key")
        funder_for_verify = acct.get("funder")
        if not isinstance(funder_for_verify, str) or not funder_for_verify:
            raise HTTPException(status_code=400, detail="Funder is required before setting trading session key")
        ok, reason = _verify_trading_session_key_for_auth(generated, funder_for_verify, int(acct.get("signature_type", 2) or 2))
        if not ok:
            raise HTTPException(status_code=400, detail=f"Generated trading key is not authorized for this funder/signature context ({reason})")
        acct["trading_session_key"] = _encrypt_session_key(generated)
        acct["trading_session_updated_at"] = now_iso
        acct["trading_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        acct["trading_session_verified"] = True
        acct["trading_session_verified_at"] = now_iso
        acct["trading_session_verification_error"] = None
    else:
        acct["fee_session_key"] = _encrypt_session_key(generated)
        acct["fee_session_updated_at"] = now_iso
        acct["fee_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"

    acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1
    _save_users(db)
    return {"ok": True, "account": _sanitize_trading_account(acct)}


@app.delete("/api/user/trading-account/session-keys/{key_type}")
def clear_user_trading_session_key(key_type: str, user: User = Depends(get_current_user)):
    db = _load_users()
    acct = _upsert_user_trading_account(db, user.username)

    kind = (key_type or "").strip().lower()
    if kind not in {"trading", "fee"}:
        raise HTTPException(status_code=400, detail="type must be trading or fee")

    if kind == "trading":
        acct.pop("trading_session_key", None)
        acct.pop("session_key", None)
        acct["trading_session_updated_at"] = _utc_now_iso()
        acct["trading_session_expires_at"] = None
        acct["trading_session_verified"] = False
        acct["trading_session_verified_at"] = None
        acct["trading_session_verification_error"] = None
    else:
        acct.pop("fee_session_key", None)
        acct.pop("fees_session_key", None)
        acct.pop("fee_key", None)
        acct["fee_session_updated_at"] = _utc_now_iso()
        acct["fee_session_expires_at"] = None

    acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1
    _save_users(db)
    return {"ok": True, "account": _sanitize_trading_account(acct)}


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


@app.get("/api/trading/accounts")
def list_trading_accounts(user: User = Depends(require_admin)):
    db = _load_users()
    accounts = _trading_accounts_ref(db)

    # Ensure every known account/user appears in admin trading list, even if
    # no funder/session keys are configured yet.
    existing_names = {str((a or {}).get("name") or "").strip() for a in accounts}
    existing_names.discard("")
    all_names = set(existing_names)

    for uname in db.keys():
        if isinstance(uname, str) and uname and not uname.startswith("_"):
            all_names.add(uname)
    for d in STATE.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        if name.lower() in {"chart_cache"} or name.startswith(".") or name.startswith("_"):
            continue
        all_names.add(name)

    changed = False
    for name in sorted(all_names, key=lambda s: s.lower()):
        if name not in existing_names:
            _upsert_user_trading_account(db, name)
            changed = True

    if changed:
        _save_users(db)
        accounts = _trading_accounts_ref(db)

    ordered = sorted(accounts, key=lambda a: int((a or {}).get("priority", 0) or 0), reverse=True)
    out = []
    for a in ordered:
        row = _sanitize_trading_account(a)
        privileged = _is_admin_or_founders_account(db, a.get("name"))
        row["is_admin_or_founders"] = privileged
        row["requires_fee_key"] = not privileged
        user_row = db.get(str(a.get("name") or ""))
        row["has_user_record"] = isinstance(user_row, dict)
        row["is_admin"] = bool(isinstance(user_row, dict) and user_row.get("is_admin", False))
        out.append(row)
    return {"accounts": out}


@app.post("/api/trading/accounts")
def create_trading_account(payload: TradingAccountCreate, user: User = Depends(require_admin)):
    db = _load_users()
    accounts = _trading_accounts_ref(db)

    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if _find_trading_account(accounts, name):
        raise HTTPException(status_code=409, detail="Account already exists")

    auth_mode = _normalize_auth_mode(payload.auth_mode)
    funder = _normalize_eth_address(payload.funder, "funder")
    priority = int(payload.priority)
    signature_type = payload.signature_type
    if signature_type is not None and signature_type not in (0, 1, 2):
        raise HTTPException(status_code=400, detail="signature_type must be one of 0, 1, 2")

    now_iso = _utc_now_iso()
    acct = {
        "name": name,
        "enabled": False,
        "user_enabled": False,
        "admin_enabled": bool(payload.enabled),
        "priority": priority,
        "funder": funder,
        "auth_mode": auth_mode,
    }
    if payload.wallet_owner:
        acct["wallet_owner"] = _normalize_eth_address(payload.wallet_owner, "wallet_owner")
    if payload.safe_address:
        acct["safe_address"] = _normalize_eth_address(payload.safe_address, "safe_address")

    normalized_private_key = None
    if payload.private_key:
        normalized_private_key = _normalize_private_key(payload.private_key)
        private_key_address = Account.from_key(normalized_private_key).address
        if auth_mode == "private_key":
            funder = private_key_address
            acct["funder"] = private_key_address
            acct["wallet_owner"] = private_key_address
            signature_type = 0
        acct["private_key"] = _encrypt_session_key(normalized_private_key)

    normalized_trading_key = None
    if payload.trading_session_key:
        normalized_trading_key = _normalize_private_key(payload.trading_session_key)
        sig_for_verify = signature_type if signature_type is not None else 2
        ok, reason = _verify_trading_session_key_for_auth(normalized_trading_key, funder, int(sig_for_verify))
        if not ok:
            raise HTTPException(status_code=400, detail=f"Trading session key is not authorized for this funder/signature context ({reason})")
        acct["trading_session_key"] = _encrypt_session_key(normalized_trading_key)
        acct["trading_session_updated_at"] = now_iso
        acct["trading_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        acct["trading_session_verified"] = True
        acct["trading_session_verified_at"] = now_iso
        acct["trading_session_verification_error"] = None

    if payload.fee_session_key:
        normalized_fee_key = _normalize_private_key(payload.fee_session_key)
        acct["fee_session_key"] = _encrypt_session_key(normalized_fee_key)
        acct["fee_session_updated_at"] = now_iso
        acct["fee_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"

    if auth_mode == "private_key":
        if payload.enabled and not normalized_private_key:
            raise HTTPException(status_code=400, detail="Private key is required when auth_mode is private_key")
        signature_type = 0
    elif signature_type is None:
        signer = _derive_signer_address({}, normalized_trading_key)
        if (not signer) or signer.lower() != funder.lower():
            signature_type = 2
        else:
            signature_type = 0
    acct["signature_type"] = signature_type
    acct["key_version"] = 1
    _sync_account_enable_flags(acct)

    accounts.append(acct)
    _save_users(db)
    out = _sanitize_trading_account(acct)
    privileged = _is_admin_or_founders_account(db, acct.get("name"))
    out["is_admin_or_founders"] = privileged
    out["requires_fee_key"] = not privileged
    return {"ok": True, "account": out}


@app.patch("/api/trading/accounts/{name}")
def patch_trading_account(name: str, payload: TradingAccountPatch, user: User = Depends(require_admin)):
    db = _load_users()
    accounts = _trading_accounts_ref(db)
    acct = _find_trading_account(accounts, name)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")
    _sync_account_enable_flags(acct)

    now_iso = _utc_now_iso()
    if payload.enabled is not None:
        acct["admin_enabled"] = bool(payload.enabled)
    if payload.priority is not None:
        acct["priority"] = int(payload.priority)
    if payload.funder is not None:
        acct["funder"] = _normalize_eth_address(payload.funder, "funder")
    if payload.wallet_owner is not None:
        acct["wallet_owner"] = _normalize_eth_address(payload.wallet_owner, "wallet_owner")
    if payload.safe_address is not None:
        acct["safe_address"] = _normalize_eth_address(payload.safe_address, "safe_address")
    if payload.clear_funder:
        acct.pop("funder", None)
        acct.pop("wallet_owner", None)
        acct.pop("safe_address", None)
    if payload.clear_private_key:
        acct.pop("private_key", None)
    if payload.auth_mode is not None:
        acct["auth_mode"] = _normalize_auth_mode(payload.auth_mode)
    if payload.signature_type is not None:
        if payload.signature_type not in (0, 1, 2):
            raise HTTPException(status_code=400, detail="signature_type must be one of 0, 1, 2")
        acct["signature_type"] = payload.signature_type
    if payload.is_admin is not None:
        target = db.get(name)
        if not isinstance(target, dict):
            raise HTTPException(status_code=400, detail="Cannot change admin role: no user record for this account")
        target["is_admin"] = bool(payload.is_admin)
        # Update active token claims so role changes apply immediately.
        changed = False
        for t, info in TOKENS.items():
            if str(info.get("username", "")) == str(name):
                info["is_admin"] = bool(payload.is_admin)
                changed = True
        if changed:
            _save_tokens_file(TOKENS)

    if payload.private_key is not None:
        normalized_private_key = _normalize_private_key(payload.private_key)
        private_key_address = Account.from_key(normalized_private_key).address
        acct["private_key"] = _encrypt_session_key(normalized_private_key)
        if _account_auth_mode(acct) == "private_key":
            acct["funder"] = private_key_address
            acct["wallet_owner"] = private_key_address
            acct["signature_type"] = 0
        acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1

    if _account_auth_mode(acct) == "private_key":
        private_key_address = _private_key_address(acct.get("private_key"))
        if private_key_address:
            acct["funder"] = private_key_address
            acct["wallet_owner"] = private_key_address
        acct["signature_type"] = 0

    normalized_trading_key = None
    if payload.trading_session_key is not None:
        normalized_trading_key = _normalize_private_key(payload.trading_session_key)
        funder_for_verify = acct.get("funder")
        if not isinstance(funder_for_verify, str) or not funder_for_verify:
            raise HTTPException(status_code=400, detail="Funder is required before setting trading session key")
        sig_for_verify = int(acct.get("signature_type", 0) or 0)
        ok, reason = _verify_trading_session_key_for_auth(normalized_trading_key, funder_for_verify, sig_for_verify)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Trading session key is not authorized for this funder/signature context ({reason})")
        acct["trading_session_key"] = _encrypt_session_key(normalized_trading_key)
        acct["trading_session_updated_at"] = now_iso
        acct["trading_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1
        acct["trading_session_verified"] = True
        acct["trading_session_verified_at"] = now_iso
        acct["trading_session_verification_error"] = None

    if payload.fee_session_key is not None:
        normalized_fee_key = _normalize_private_key(payload.fee_session_key)
        acct["fee_session_key"] = _encrypt_session_key(normalized_fee_key)
        acct["fee_session_updated_at"] = now_iso
        acct["fee_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1

    if _account_auth_mode(acct) != "private_key" and payload.signature_type is None and payload.funder is not None:
        signer = _derive_signer_address(acct, normalized_trading_key)
        funder = acct.get("funder")
        if signer and isinstance(funder, str) and signer.lower() != funder.lower():
            acct["signature_type"] = 2

    _sync_account_enable_flags(acct)

    _save_users(db)
    out = _sanitize_trading_account(acct)
    privileged = _is_admin_or_founders_account(db, acct.get("name"))
    out["is_admin_or_founders"] = privileged
    out["requires_fee_key"] = not privileged
    user_row = db.get(str(acct.get("name") or ""))
    out["has_user_record"] = isinstance(user_row, dict)
    out["is_admin"] = bool(isinstance(user_row, dict) and user_row.get("is_admin", False))
    return {"ok": True, "account": out}


@app.delete("/api/trading/accounts/{name}/session-keys/{key_type}")
def delete_trading_session_key(name: str, key_type: str, user: User = Depends(require_admin)):
    db = _load_users()
    accounts = _trading_accounts_ref(db)
    acct = _find_trading_account(accounts, name)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")

    key_type_norm = (key_type or "").strip().lower()
    if key_type_norm not in {"trading", "fee"}:
        raise HTTPException(status_code=400, detail="type must be trading or fee")

    if key_type_norm == "trading":
        acct.pop("trading_session_key", None)
        acct.pop("session_key", None)
        acct["trading_session_updated_at"] = _utc_now_iso()
        acct["trading_session_expires_at"] = None
        acct["trading_session_verified"] = False
        acct["trading_session_verified_at"] = None
        acct["trading_session_verification_error"] = None
    else:
        acct.pop("fee_session_key", None)
        acct.pop("fees_session_key", None)
        acct.pop("fee_key", None)
        acct["fee_session_updated_at"] = _utc_now_iso()
        acct["fee_session_expires_at"] = None

    acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1
    _save_users(db)
    return {"ok": True, "account": _sanitize_trading_account(acct)}


@app.delete("/api/trading/accounts/{name}")
def delete_trading_account(name: str, user: User = Depends(require_admin)):
    db = _load_users()
    accounts = _trading_accounts_ref(db)
    acct = _find_trading_account(accounts, name)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")

    # Keep at least one trading account configured.
    if len(accounts) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last trading account")

    accounts[:] = [a for a in accounts if str(a.get("name")) != name]

    # If the trading account maps to a user record, remove that user as well.
    if name in db and isinstance(db.get(name), dict) and not str(name).startswith("_"):
        db.pop(name, None)
        # Revoke active tokens for the deleted username.
        stale = [t for t, info in TOKENS.items() if str(info.get("username", "")) == str(name)]
        for t in stale:
            TOKENS.pop(t, None)
        _save_tokens_file(TOKENS)

    _save_users(db)
    return {"ok": True, "deleted": name}


@app.post("/api/trading/accounts/{name}/session-keys/{key_type}/generate")
def generate_trading_session_key(name: str, key_type: str, user: User = Depends(require_admin)):
    db = _load_users()
    accounts = _trading_accounts_ref(db)
    acct = _find_trading_account(accounts, name)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")

    key_type_norm = (key_type or "").strip().lower()
    if key_type_norm not in {"trading", "fee"}:
        raise HTTPException(status_code=400, detail="type must be trading or fee")

    now_iso = _utc_now_iso()
    generated = _generate_private_key_hex()
    if key_type_norm == "trading":
        funder_for_verify = acct.get("funder")
        if not isinstance(funder_for_verify, str) or not funder_for_verify:
            raise HTTPException(status_code=400, detail="Funder is required before setting trading session key")
        sig_for_verify = int(acct.get("signature_type", 0) or 0)
        ok, reason = _verify_trading_session_key_for_auth(generated, funder_for_verify, sig_for_verify)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Generated trading key is not authorized for this funder/signature context ({reason})")
        acct["trading_session_key"] = _encrypt_session_key(generated)
        acct["trading_session_updated_at"] = now_iso
        acct["trading_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"
        acct["trading_session_verified"] = True
        acct["trading_session_verified_at"] = now_iso
        acct["trading_session_verification_error"] = None
    else:
        acct["fee_session_key"] = _encrypt_session_key(generated)
        acct["fee_session_updated_at"] = now_iso
        acct["fee_session_expires_at"] = (datetime.utcnow() + timedelta(days=30)).replace(microsecond=0).isoformat() + "Z"

    acct["key_version"] = int(acct.get("key_version", 1) or 1) + 1
    _save_users(db)
    return {"ok": True, "account": _sanitize_trading_account(acct)}


@app.post("/api/trading/accounts/reorder")
def reorder_trading_accounts(payload: TradingReorderRequest, user: User = Depends(require_admin)):
    db = _load_users()
    accounts = _trading_accounts_ref(db)
    names = [str(a.get("name")) for a in accounts if isinstance(a, dict)]
    name_set = set(names)

    ordered = payload.ordered_names or []
    if len(set(ordered)) != len(ordered):
        raise HTTPException(status_code=400, detail="ordered_names must be unique")
    missing = [n for n in ordered if n not in name_set]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown accounts in ordered_names: {', '.join(missing)}")

    ranked = list(ordered)
    tail = [n for n in names if n not in set(ordered)]
    ranked.extend(tail)

    base = len(ranked) * 10
    by_name = {str(a.get("name")): a for a in accounts}
    for idx, n in enumerate(ranked):
        by_name[n]["priority"] = base - (idx * 10)

    _save_users(db)
    return {"ok": True, "accounts": [_sanitize_trading_account(by_name[n]) for n in ranked]}


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
                
                # Check if this timestamp belongs to the target market date
                if _market_date_for_ts(dt) != target_date.date():
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


def _market_date_for_ts(dt: datetime):
    """Return the market date a timestamp belongs to, using the noon ET roll."""
    et = dt.astimezone(ET)
    if et.hour < 12:
        return et.date()
    return (et + timedelta(days=1)).date()


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
                        dt = dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                if _market_date_for_ts(dt) != target_date.date():
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
                # Skip synthetic/missing rows recorded as 0/0.
                if upf == 0.0 and dnf == 0.0:
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

    # Dedup by ts (prefer later entries by sorting)
    by_ts = {}
    for p in sorted(points, key=lambda x: x["ts"]) :  # type: ignore
        by_ts[p["ts"]] = p
    out = list(by_ts.values())
    out.sort(key=lambda x: x["ts"])  # type: ignore
    return out


def _normalize_probability(v: Any) -> Optional[float]:
    try:
        n = float(v)
    except Exception:
        return None
    if n > 1:
        n = n / 100.0
    if not (0.0 <= n <= 1.0):
        return None
    return n


def _safe_mid_price(token_id: Optional[str]) -> Optional[float]:
    if not token_id:
        return None
    try:
        bid = clob_price(token_id, "sell")
    except Exception:
        bid = None
    try:
        ask = clob_price(token_id, "buy")
    except Exception:
        ask = None
    try:
        bid = float(bid) if bid is not None else None
    except Exception:
        bid = None
    try:
        ask = float(ask) if ask is not None else None
    except Exception:
        ask = None
    if (bid is not None) and (ask is not None):
        return (bid + ask) / 2.0
    if ask is not None:
        return ask
    if bid is not None:
        return bid
    return None


def _market_window_utc_for_date(target_date: datetime) -> tuple[datetime, datetime]:
    """Return UTC bounds for one market day using noon ET roll."""
    d = target_date.date()
    noon_target = datetime(d.year, d.month, d.day, 12, 0, 0)
    noon_prev = noon_target - timedelta(days=1)
    if hasattr(ET, "localize"):
        start_et = ET.localize(noon_prev)
        end_et = ET.localize(noon_target)
    else:
        start_et = noon_prev.replace(tzinfo=ET)
        end_et = noon_target.replace(tzinfo=ET)
    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)
    if d == _active_market_date():
        now_utc = datetime.now(timezone.utc)
        if now_utc < end_utc:
            end_utc = now_utc
    return start_utc, end_utc


def _fetch_clob_up_history_for_day(target_date: datetime) -> List[Dict[str, Any]]:
    """Fetch UP token historical prices from CLOB and map to local point format."""
    up_id, _dn_id, _market = _resolve_market_for_date(target_date.date())
    if not up_id:
        return []
    start_utc, end_utc = _market_window_utc_for_date(target_date)
    start_ts = int(start_utc.timestamp())
    end_ts = int(end_utc.timestamp())
    if end_ts <= start_ts:
        return []

    headers = {"Accept": "application/json", "User-Agent": _yahoo_user_agent()}
    history: List[Any] = []
    try:
        # Dense intraday series (close to the old minute-by-minute feel).
        resp = requests.get(
            f"{_clob_api_base().rstrip('/')}/prices-history",
            params={
                "market": up_id,
                "interval": "1d",
                "fidelity": 1,
            },
            timeout=7.5,
            headers=headers,
        )
        if resp.status_code == 200:
            payload = resp.json() if resp.content else {}
            history = payload.get("history", []) if isinstance(payload, dict) else []
            if not isinstance(history, list):
                history = []
    except Exception:
        history = []

    # Fallback for endpoint filter quirks: request full market history and clip locally.
    if len(history) < 5:
        try:
            resp2 = requests.get(
                f"{_clob_api_base().rstrip('/')}/prices-history",
                params={"market": up_id, "startTs": start_ts, "endTs": end_ts, "interval": "1m", "fidelity": 10},
                timeout=7.5,
                headers=headers,
            )
            if resp2.status_code == 200:
                payload2 = resp2.json() if resp2.content else {}
                h2 = payload2.get("history", []) if isinstance(payload2, dict) else []
                if isinstance(h2, list) and len(h2) > len(history):
                    history = h2
        except Exception:
            pass

    if len(history) < 5:
        try:
            resp3 = requests.get(
                f"{_clob_api_base().rstrip('/')}/prices-history",
                params={"market": up_id, "interval": "max", "fidelity": 1},
                timeout=7.5,
                headers=headers,
            )
            if resp3.status_code == 200:
                payload3 = resp3.json() if resp3.content else {}
                h3 = payload3.get("history", []) if isinstance(payload3, dict) else []
                if isinstance(h3, list) and len(h3) > len(history):
                    history = h3
        except Exception:
            pass

    out: List[Dict[str, Any]] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        t_raw = row.get("t")
        p_raw = row.get("p")
        up = _normalize_probability(p_raw)
        if up is None:
            continue
        try:
            t_num = float(t_raw)
        except Exception:
            continue
        # API may return seconds or milliseconds.
        if t_num > 1e12:
            t_num = t_num / 1000.0
        try:
            dt = datetime.fromtimestamp(t_num, tz=timezone.utc)
        except Exception:
            continue
        if dt < start_utc or dt > (end_utc + timedelta(minutes=1)):
            continue
        out.append({
            "ts": dt.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "up": up,
            "down": 1.0 - up,
        })
    out.sort(key=lambda x: x["ts"])
    return out


@app.get("/api/prices/day")
def prices_for_day(date: str, user: User = Depends(get_current_user)):
    try:
        d = datetime.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    clob_points = _fetch_clob_up_history_for_day(d)
    local_points = _merge_day_points(d)
    # Prefer authoritative CLOB history; only blend local tail points that are valid.
    points: List[Dict[str, Any]] = []
    if clob_points:
        points.extend(clob_points)
        latest_clob_ts = clob_points[-1]["ts"]
        for p in local_points:
            ts = str(p.get("ts") or "").strip()
            if not ts or ts <= latest_clob_ts:
                continue
            up = _normalize_probability(p.get("up"))
            down = _normalize_probability(p.get("down"))
            if up is None and down is not None:
                up = 1.0 - down
            if down is None and up is not None:
                down = 1.0 - up
            if up is None or down is None:
                continue
            # Discard clearly synthetic missing-data rows.
            if up == 0.0 and down == 0.0:
                continue
            points.append({"ts": ts, "up": up, "down": down})
    else:
        points = local_points

    by_ts: Dict[str, Dict[str, Any]] = {}
    for p in points:
        ts = str(p.get("ts") or "").strip()
        if not ts:
            continue
        up = _normalize_probability(p.get("up"))
        down = _normalize_probability(p.get("down"))
        if up is None and down is not None:
            up = 1.0 - down
        if down is None and up is not None:
            down = 1.0 - up
        if up is None or down is None:
            continue
        by_ts[ts] = {"ts": ts, "up": up, "down": down}
    points = sorted(by_ts.values(), key=lambda x: x["ts"])
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
    """Return only canonical confirmed fills (plus redemptions)."""
    return is_confirmed_trade_record(record)


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
    # Market rolls at 12:00 PM ET (noon): before noon uses today's market,
    # at/after noon uses next day's market.
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
            _up, _dn, market = _resolve_market_for_date(d)
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
def active_market():
    d = _active_market_date()
    up_id, dn_id, market = _resolve_market_for_date(d)
    return {"date": d.isoformat(), "up_id": up_id, "down_id": dn_id, "market": market}


@app.get("/api/yahoo/prediction-market")
def yahoo_prediction_market(slug: Optional[str] = None):
    try:
        d = None
        if not slug:
            d = _active_market_date()
            _, _, market = _resolve_market_for_date(d)
            slug = str((market or {}).get("slug") or "").strip()
        if not slug:
            raise HTTPException(status_code=404, detail="Active market slug unavailable")

        # Yahoo Finance appends the year to the slug (e.g. "bitcoin-up-or-down-on-march-30-2026")
        if d is None:
            d = _active_market_date()
        year = str(d.year)
        yahoo_slug = slug if slug.endswith(f"-{year}") else f"{slug}-{year}"

        session = requests.Session()
        crumb = _yahoo_get_crumb(session)
        if not crumb:
            raise HTTPException(status_code=502, detail="Yahoo crumb fetch failed")

        hosts = [
            "https://query1.finance.yahoo.com",
            "https://query2.finance.yahoo.com",
        ]
        # Yahoo moved this API path from /prediction/event to /prediction-market/event.
        # Keep legacy fallback for resiliency in case one host lags.
        endpoint_paths = [
            "/v1/finance/prediction-market/event/{slug}",
            "/v1/finance/prediction/event/{slug}",
        ]
        last_status = None
        for base in hosts:
            for endpoint in endpoint_paths:
                url = f"{base}{endpoint.format(slug=yahoo_slug)}"
                params = {
                    "slug": yahoo_slug,
                    "lang": "en-US",
                    "region": "US",
                    "crumb": crumb,
                }
                resp = session.get(
                    url,
                    params=params,
                    timeout=4.5,
                    headers={"Accept": "application/json", "User-Agent": _yahoo_user_agent()},
                )
                last_status = resp.status_code
                if resp.status_code == 200:
                    payload = resp.json() if resp.content else {}
                    return {"ok": True, "slug": slug, "yahoo_slug": yahoo_slug, "payload": payload}
                # If crumb expired/invalid, refresh once and retry this endpoint.
                if resp.status_code == 401:
                    YAHOO_CRUMB_CACHE["crumb"] = None
                    refreshed = _yahoo_get_crumb(session)
                    if refreshed:
                        params["crumb"] = refreshed
                        resp2 = session.get(
                            url,
                            params=params,
                            timeout=4.5,
                            headers={"Accept": "application/json", "User-Agent": _yahoo_user_agent()},
                        )
                        last_status = resp2.status_code
                        if resp2.status_code == 200:
                            payload = resp2.json() if resp2.content else {}
                            return {"ok": True, "slug": slug, "yahoo_slug": yahoo_slug, "payload": payload}
        raise HTTPException(status_code=502, detail=f"Yahoo event unavailable ({last_status})")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Yahoo event fetch failed: {e}")


def _parse_json_array_maybe(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return None
    return None


def _gamma_current_from_market(market: dict) -> Optional[Dict[str, Any]]:
    outcomes = _parse_json_array_maybe(market.get("outcomes")) or []
    prices = _parse_json_array_maybe(market.get("outcomePrices")) or []
    if len(prices) >= 2:
        labels = [str(x or "").strip().lower() for x in outcomes]
        up_idx = next((i for i, s in enumerate(labels) if "up" in s), 0)
        down_idx = 1 if up_idx == 0 else 0
        try:
            up = float(prices[up_idx])
            down = float(prices[down_idx])
            if up > 1:
                up /= 100.0
            if down > 1:
                down /= 100.0
            return {"up": {"mid": up}, "down": {"mid": down}}
        except Exception:
            pass

    # Fallback: approximate from bestBid/bestAsk/lastTradePrice for UP side.
    bid = market.get("bestBid")
    ask = market.get("bestAsk")
    last = market.get("lastTradePrice")
    try:
        bid = float(bid) if bid is not None else None
    except Exception:
        bid = None
    try:
        ask = float(ask) if ask is not None else None
    except Exception:
        ask = None
    try:
        last = float(last) if last is not None else None
    except Exception:
        last = None

    mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else (last if last is not None else (ask if ask is not None else bid))
    if mid is None:
        return None
    if mid > 1:
        mid /= 100.0
    down = 1.0 - mid
    return {"up": {"mid": mid}, "down": {"mid": down}}


@app.get("/api/polymarket/current-by-slug")
def polymarket_current_by_slug(slug: str, user: User = Depends(get_current_user)):
    slug = str(slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="Missing slug")
    try:
        base = "https://gamma-api.polymarket.com"
        headers = {"Accept": "application/json", "User-Agent": _yahoo_user_agent()}
        # Most reliable for pricing snapshot.
        mr = requests.get(f"{base}/markets", params={"slug": slug}, timeout=6.5, headers=headers)
        if mr.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Gamma markets fetch failed ({mr.status_code})")
        payload = mr.json() if mr.content else []
        markets = payload if isinstance(payload, list) else payload.get("data", [])
        if not isinstance(markets, list) or not markets:
            raise HTTPException(status_code=404, detail="Gamma market not found for slug")
        market = markets[0] if isinstance(markets[0], dict) else {}
        cur = _gamma_current_from_market(market)
        if not cur:
            raise HTTPException(status_code=502, detail="Gamma market missing current price fields")
        return {"ok": True, "slug": slug, "current": cur, "market": market}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gamma current fetch failed: {e}")


@app.get("/api/prices/current")
def current_prices(user: User = Depends(get_current_user)):
    now = time.time()
    if _CURRENT_PRICES_CACHE["data"] is not None and now - _CURRENT_PRICES_CACHE["ts"] < _CURRENT_PRICES_TTL:
        return _CURRENT_PRICES_CACHE["data"]
    d = _active_market_date()
    up_id, dn_id, market = _resolve_market_for_date(d)
    def _px(tid):
        try:
            bid = clob_price(tid, "sell")
        except Exception:
            bid = None
        try:
            ask = clob_price(tid, "buy")
        except Exception:
            ask = None
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
    result = {"ts": _utc_now_iso(), "date": d.isoformat(), "up": up, "down": dn}
    _CURRENT_PRICES_CACHE["data"] = result
    _CURRENT_PRICES_CACHE["ts"] = now
    return result


def _record_current_prices_once():
    """Fetch current mid prices and append to PRICE_LOG."""
    try:
        d = _active_market_date()
        up_id, dn_id, _ = _resolve_market_for_date(d)
        up_mid = _safe_mid_price(up_id)
        dn_mid = _safe_mid_price(dn_id)
        # Do not write synthetic 0/0 rows when no book data is available.
        if up_mid is None and dn_mid is None:
            return
        # Derive complement when only one side is available.
        if up_mid is None and dn_mid is not None:
            up_mid = 1.0 - dn_mid
        if dn_mid is None and up_mid is not None:
            dn_mid = 1.0 - up_mid
        if up_mid is None or dn_mid is None:
            return
        ts = _utc_now_iso()
        # Keep values bounded (0..1 typical), but store raw float
        _append_price_row(ts, float(up_mid), float(dn_mid))
    except Exception:
        pass


_recorder_thread: Optional[threading.Thread] = None
_recorder_stop = threading.Event()


def _price_recorder_loop(period_sec: float = 60.0):
    _ensure_price_log_header()
    # Seed: copy last 7 days from HIST_CSV into our log if missing
    try:
        today = datetime.now(timezone.utc).date()
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
    global TOKENS
    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_loop_exception_handler)
    except Exception:
        pass
    # Pre-load persisted tokens so no request ever hits an empty TOKENS dict
    try:
        disk = _load_tokens_file()
        if disk:
            TOKENS.update(disk)
    except Exception:
        pass
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
                if not is_confirmed_trade_record(rec):
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
    up_id, dn_id, _ = _resolve_market_for_date(d)
    def _mid(t):
        try: bid = clob_price(t, "sell")
        except Exception: bid = None
        try: ask = clob_price(t, "buy")
        except Exception: ask = None
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
    usdc_balance = None
    usdc_breakdown: Dict[str, float] = {}
    address = None
    safe_funder = None
    funder_address = None
    safe_address = None
    try:
        from balance_checker import get_data_api_positions_pnl
        db = _load_users()
        acct_cfg = _trading_accounts_ref(db)
        target_priv = None
        acct = _find_trading_account(acct_cfg, name)
        if not acct and user.is_admin:
            acct = _find_trading_account(acct_cfg, "Founders")
        if not acct:
            enabled = [a for a in acct_cfg if _is_account_enabled(a)]
            enabled.sort(key=lambda a: int(a.get("priority", 0) or 0), reverse=True)
            acct = enabled[0] if enabled else (acct_cfg[0] if acct_cfg else None)
        if acct:
            target_priv = (
                _decrypt_session_key(acct.get("trading_session_key"))
                or _decrypt_session_key(acct.get("session_key"))
                or _decrypt_session_key(acct.get("private_key"))
            )
            saved_owner = acct.get("wallet_owner")
            candidate_safe = acct.get("safe_address")
            if not candidate_safe:
                # Backward compatibility: accept funder/proxy as safe only if it behaves like a Safe.
                fallback_candidate = acct.get("funder") or acct.get("proxy_wallet")
                if isinstance(fallback_candidate, str) and ETH_ADDR_RE.match(fallback_candidate):
                    owners = _safe_owners_from_address(fallback_candidate, _chain_id_default())
                    if owners:
                        candidate_safe = fallback_candidate
                        if not saved_owner:
                            saved_owner = owners[0]
            safe_address = candidate_safe
            safe_funder = safe_address
            address = safe_address
            funder_address = saved_owner
            if funder_address and safe_address and str(funder_address).lower() == str(safe_address).lower():
                owners = _safe_owners_from_address(safe_address, _chain_id_default())
                if owners:
                    funder_address = owners[0]
                if str(funder_address).lower() == str(safe_address).lower():
                    funder_address = None
            if (not funder_address) and safe_address:
                owners = _safe_owners_from_address(safe_address, _chain_id_default())
                if owners:
                    funder_address = owners[0]
            if not address and target_priv and target_priv.startswith('0x') and 'REPLACE_WITH_TEST' not in target_priv:
                try:
                    address = Account.from_key(target_priv).address
                except Exception:
                    address = None

            usdc_total, usdc_parts = _polygon_usdc_balance(safe_address)
            usdc_balance = usdc_total
            usdc_breakdown = usdc_parts

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
    display_safe_balance = usdc_balance if usdc_balance is not None else holdings_value
    return {
        "date": d.isoformat(),
        "positions": {"UP": cost["UP"], "DOWN": cost["DOWN"]},
        "mark": {"UP": up_val, "DOWN": dn_val},
        "address": address,
        "funder_address": funder_address,
        "safe_address": safe_address or safe_funder or address,
        "safe_funder": safe_funder or address,
        "safe_balance": display_safe_balance,
        "safe_balance_source": "polygon_usdc" if usdc_balance is not None else "polymarket_holdings",
        "safe_usdc_tokens": usdc_breakdown,
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


async def _tail_log_file(path: Path, request: Request):
    try:
        with open(path, "r", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            while True:
                # Break quickly when the client disconnects (or server is shutting down),
                # so Ctrl+C can terminate cleanly.
                if await request.is_disconnected():
                    break
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.25)
                    continue
                yield line
    except FileNotFoundError:
        yield "\n"


@app.get("/api/logs/stream")
def stream_logs(request: Request, user: User = Depends(require_admin)):
    # Stream the latest log file
    files = list(LOGS_DIR.glob("*.log"))
    if not files:
        raise HTTPException(status_code=404, detail="No logs")
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return StreamingResponse(_tail_log_file(latest, request), media_type="text/plain")


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
            uvicorn.run(
                app,
                host=os.environ.get("HOST", "0.0.0.0"),
                port=port,
                reload=False,
                ssl_keyfile=ssl_key,
                ssl_certfile=ssl_cert,
                timeout_graceful_shutdown=2,
            )
        else:
            print(f"Starting HTTP server on http://10.0.0.147:{port}")
            print("To enable HTTPS, set SSL_CERT_PATH and SSL_KEY_PATH environment variables")
            uvicorn.run(
                app,
                host=os.environ.get("HOST", "0.0.0.0"),
                port=port,
                reload=False,
                timeout_graceful_shutdown=2,
            )
    finally:
        _recorder_stop.set()
        try:
            if _recorder_thread and _recorder_thread.is_alive():
                _recorder_thread.join(timeout=2.0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
