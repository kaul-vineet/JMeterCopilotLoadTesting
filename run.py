"""
run.py — Copilot Studio Load Test

First run / reconfigure:
    python run.py           → setup wizard, then Locust web UI
    python run.py --setup   → force wizard even if .env is already configured

Headless (pre-authenticate all profiles first):
    locust -f run.py --headless -u 10 -r 1
"""

# gevent monkey-patch must happen before any I/O library imports
import gevent.monkey
gevent.monkey.patch_all()

import base64
import csv
import hashlib
import itertools
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import colorama; colorama.init()
import msal
import requests
import websocket
from cryptography.fernet import Fernet
from flask import jsonify, request
from locust import User, events, task
from locust.exception import StopUser
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table

log     = logging.getLogger(__name__)
console = Console()

# ── Paths ─────────────────────────────────────────────────────────────────────

_HERE          = Path(__file__).parent
PROFILES_JSON  = _HERE / "profiles" / "profiles.json"
TOKENS_DIR     = _HERE / "profiles" / ".tokens"
UTTERANCES_DIR = _HERE / "utterances"
TOKENS_DIR.mkdir(parents=True, exist_ok=True)

# ── Windows Credential Manager helpers ───────────────────────────────────────

_KR_SERVICE = "copilot-load-test"


def _load_credential(key: str) -> str:
    try:
        import keyring
        val = keyring.get_password(_KR_SERVICE, key)
        if val:
            return val
    except Exception:
        pass
    return os.getenv(key, "")


def _save_credential(key: str, value: str):
    import keyring
    keyring.set_password(_KR_SERVICE, key, value)
    os.environ[key] = value


# ── Shared config dict (read by Locust User classes) ─────────────────────────

test_config: dict = {
    "frame_timeout": 10.0,
    "think_min":     30,
    "think_max":     60,
    "p95_target_ms": 2000,
    "max_error_rate": 0.5,
}

# ── Credentials (read from Windows Credential Manager, fallback to env vars) ──

TENANT_ID           = _load_credential("CS_TENANT_ID")
CLIENT_ID           = _load_credential("CS_CLIENT_ID")
AGENT_APP_ID        = _load_credential("CS_AGENT_APP_ID")
ENC_PASSWORD        = _load_credential("TOKEN_ENCRYPTION_PASSWORD")
DL_SECRET           = _load_credential("CS_DIRECTLINE_SECRET")
TOKEN_ENDPOINT      = _load_credential("CS_TOKEN_ENDPOINT")
ENDPOINT_NEEDS_AUTH = _load_credential("CS_TOKEN_ENDPOINT_REQUIRES_AUTH").lower() == "true"
DIRECTLINE_BASE = "https://directline.botframework.com"

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _scopes() -> list[str]:
    if AGENT_APP_ID:
        return [f"api://{AGENT_APP_ID}/access_as_user"]
    return ["https://api.powerplatform.com/.default"]


# ── Encryption + token store ──────────────────────────────────────────────────

def _get_encryption_key() -> bytes:
    try:
        import keyring
        key = keyring.get_password("copilot-load-test", "fernet-key")
        if not key:
            raw = Fernet.generate_key()
            keyring.set_password("copilot-load-test", "fernet-key", raw.decode())
            return raw
        return key.encode()
    except Exception:
        if not ENC_PASSWORD:
            print(
                "\n[ERROR] keyring unavailable and TOKEN_ENCRYPTION_PASSWORD is not set.\n"
                "Run: python run.py --setup\n"
            )
            sys.exit(1)
        dk = hashlib.pbkdf2_hmac(
            "sha256", ENC_PASSWORD.encode(), b"copilot-load-test-salt",
            iterations=260000, dklen=32,
        )
        return base64.urlsafe_b64encode(dk)


def _fernet() -> Fernet:
    return Fernet(_get_encryption_key())


def _token_path(username: str) -> Path:
    safe = username.replace("@", "_").replace(".", "_")
    return TOKENS_DIR / f"{safe}.enc"


def save_token(username: str, token_data: dict):
    payload = json.dumps(token_data).encode()
    _token_path(username).write_bytes(_fernet().encrypt(payload))


def load_token(username: str) -> dict | None:
    path = _token_path(username)
    if not path.exists():
        return None
    try:
        return json.loads(_fernet().decrypt(path.read_bytes()))
    except Exception:
        return None


def is_token_valid(token_data: dict, min_ttl_seconds: int = 600) -> bool:
    exp = token_data.get("expires_on")
    if not exp:
        return False
    return (datetime.fromtimestamp(exp, tz=timezone.utc) - datetime.now(tz=timezone.utc)).total_seconds() > min_ttl_seconds


# ── MSAL auth ─────────────────────────────────────────────────────────────────

def get_valid_token(username: str) -> str:
    token_data = load_token(username)
    if token_data and is_token_valid(token_data):
        return token_data["access_token"]

    if token_data and token_data.get("refresh_token"):
        app = msal.PublicClientApplication(
            CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
        )
        result = app.acquire_token_by_refresh_token(token_data["refresh_token"], scopes=_scopes())
        if "access_token" in result:
            token_data.update({
                "access_token":  result["access_token"],
                "expires_on":    int(result.get("expires_on") or (time.time() + result.get("expires_in", 3600))),
                "refresh_token": result.get("refresh_token", token_data.get("refresh_token")),
            })
            save_token(username, token_data)
            return result["access_token"]

    raise RuntimeError(f"No valid token for {username}. Run: python run.py --setup")


def authenticate_profile(username: str) -> bool:
    print(f"\n{'='*60}\n  Authenticating: {username}\n{'='*60}")
    app = msal.PublicClientApplication(
        CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
    )
    flow = app.initiate_device_flow(scopes=_scopes())
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")

    print(f"\n  1. Open:  {flow['verification_uri']}")
    print(f"  2. Enter: {flow['user_code']}")
    print("\n  Waiting for sign-in", end="", flush=True)

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print(f"\n  [FAILED] {result.get('error_description', result.get('error', 'Unknown'))}")
        return False

    save_token(username, {
        "access_token":  result["access_token"],
        "refresh_token": result.get("refresh_token", ""),
        "expires_on":    int(result.get("expires_on") or (time.time() + result.get("expires_in", 3600))),
        "username":      username,
    })
    print(f"\n  [OK] Token saved for {username}")
    return True


def load_profiles() -> list[dict]:
    if not PROFILES_JSON.exists():
        return []
    with open(PROFILES_JSON, encoding="utf-8") as f:
        return json.load(f)


# ── DirectLine ────────────────────────────────────────────────────────────────

@dataclass
class Conversation:
    id: str
    token: str
    stream_url: str


@dataclass
class Response:
    activities: list[dict]
    latency_ms: float
    timed_out: bool


def fetch_directline_token(aad_token: Optional[str] = None) -> str:
    if TOKEN_ENDPOINT:
        headers = {}
        if ENDPOINT_NEEDS_AUTH and aad_token:
            headers["Authorization"] = f"Bearer {aad_token}"
        resp = requests.get(TOKEN_ENDPOINT, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()["token"]

    if DL_SECRET:
        resp = requests.post(
            f"{DIRECTLINE_BASE}/v3/directline/tokens/generate",
            headers={"Authorization": f"Bearer {DL_SECRET}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["token"]

    raise RuntimeError("Neither CS_DIRECTLINE_SECRET nor CS_TOKEN_ENDPOINT is configured. Run: python run.py --setup")


def start_conversation(dl_token: str) -> Conversation:
    resp = requests.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations",
        headers={"Authorization": f"Bearer {dl_token}", "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return Conversation(id=data["conversationId"], token=data["token"], stream_url=data["streamUrl"])


def open_websocket(stream_url: str) -> websocket.WebSocket:
    ws = websocket.WebSocket(sslopt={"check_hostname": True})
    ws.connect(stream_url, timeout=20)
    return ws


def send_utterance(conversation: Conversation, utterance: str) -> tuple[str, float]:
    send_time = time.time()
    resp = requests.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}/activities",
        headers={"Authorization": f"Bearer {conversation.token}", "Content-Type": "application/json"},
        json={"locale": "en-US", "type": "message", "from": {"id": "load-test-user"}, "text": utterance},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"], send_time


def send_token_exchange(conversation: Conversation, invoke_id: str, connection_name: str, aad_token: str):
    """
    Responds to a signin/tokenExchange invoke from the bot.
    Copilot Studio sends this when 'Authenticate manually' is enabled —
    the client must reply with the user's AAD token to complete SSO.
    """
    resp = requests.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}/activities",
        headers={"Authorization": f"Bearer {conversation.token}", "Content-Type": "application/json"},
        json={
            "type": "invoke",
            "name": "signin/tokenExchange",
            "value": {
                "id": invoke_id,
                "connectionName": connection_name,
                "token": aad_token,
            },
            "from": {"id": "load-test-user"},
        },
        timeout=10,
    )
    if not resp.ok:
        log.debug("Token exchange HTTP %s body: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()
    log.debug("Token exchange sent: invoke=%s connection=%s", invoke_id, connection_name)


def read_response(
    ws: websocket.WebSocket,
    activity_id: str,
    frame_timeout: float = 10.0,
    conversation: Optional[Conversation] = None,
    aad_token: Optional[str] = None,
) -> Response:
    """
    Reads WebSocket frames until the bot replies to activity_id.
    When conversation + aad_token are provided, handles signin/tokenExchange
    invokes automatically so SSO-authenticated bots work without manual sign-in.
    """
    matched, last_match_time = [], None
    start_time = time.time()
    ws.settimeout(frame_timeout)

    while True:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        except websocket.WebSocketConnectionClosedException:
            log.warning("WebSocket connection closed by DirectLine")
            break

        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for activity in data.get("activities", []):
            # Teams SSO: bot sends signin/tokenExchange invoke directly
            if (activity.get("type") == "invoke"
                    and activity.get("name") == "signin/tokenExchange"
                    and conversation and aad_token):
                val = activity.get("value", {})
                try:
                    send_token_exchange(conversation, val.get("id", ""), val.get("connectionName", ""), aad_token)
                except Exception as e:
                    log.warning("Token exchange (invoke) failed: %s", e)
                continue

            # "Authenticate manually" SSO: bot sends message with OAuthCard attachment.
            # Client must respond with signin/tokenExchange invoke carrying the AAD token.
            if activity.get("type") == "message" and conversation and aad_token:
                for attach in activity.get("attachments", []):
                    if attach.get("contentType") == "application/vnd.microsoft.card.oauth":
                        content      = attach.get("content", {})
                        token_res    = content.get("tokenExchangeResource", {})
                        if token_res:
                            try:
                                send_token_exchange(
                                    conversation,
                                    token_res.get("id", ""),
                                    content.get("connectionName", ""),
                                    aad_token,
                                )
                                log.debug("OAuthCard token exchange sent: connection=%s", content.get("connectionName", ""))
                            except Exception as e:
                                log.warning("Token exchange (OAuthCard) failed: %s", e)
                        break  # only one OAuthCard per activity
                else:
                    # No OAuthCard — check if this is a real bot reply
                    if (activity.get("from", {}).get("role") == "bot"
                            and activity.get("replyToId") == activity_id):
                        matched.append(activity)
                        last_match_time = time.time()
                continue

            if (activity.get("type") == "message"
                    and activity.get("from", {}).get("role") == "bot"
                    and activity.get("replyToId") == activity_id):
                matched.append(activity)
                last_match_time = time.time()

    end_time   = last_match_time or time.time()
    latency_ms = (end_time - start_time) * 1000
    return Response(activities=matched, latency_ms=latency_ms, timed_out=len(matched) == 0)


def close_websocket(ws: websocket.WebSocket):
    try:
        ws.close()
    except Exception:
        pass


# ── Startup sequence (fires when Locust initialises in interactive mode) ──────

_TITLE = [
    " ██████╗ ██████╗ ██████╗ ██╗██╗      ██████╗ ████████╗",
    "██╔════╝██╔═══██╗██╔══██╗██║██║     ██╔═══██╗╚══██╔══╝",
    "██║     ██║   ██║██████╔╝██║██║     ██║   ██║   ██║   ",
    "██║     ██║   ██║██╔═══╝ ██║██║     ██║   ██║   ██║   ",
    "╚██████╗╚██████╔╝██║     ██║███████╗╚██████╔╝   ██║   ",
    " ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝ ╚═════╝    ╚═╝   ",
    "",
    "██╗      ██████╗  █████╗ ██████╗     ████████╗███████╗███████╗████████╗",
    "██║     ██╔═══██╗██╔══██╗██╔══██╗    ╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝",
    "██║     ██║   ██║███████║██║  ██║       ██║   █████╗  ███████╗   ██║   ",
    "██║     ██║   ██║██╔══██║██║  ██║       ██║   ██╔══╝  ╚════██║   ██║   ",
    "███████╗╚██████╔╝██║  ██║██████╔╝       ██║   ███████╗███████║   ██║   ",
    "╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝        ╚═╝   ╚══════╝╚══════╝   ╚═╝   ",
]
_ROCKET_BODY = [
    "         *    .  *       .         *    .",
    "    .  *    .       *  .    *   .     *  ",
    "       .        *    .        .   *      ",
    "  *  .    *   .    *    .   *    .    *  ",
    "                   /\\                   ",
    "                  /  \\                  ",
    "                 / 🔥 \\                 ",
    "                /______\\                ",
    "               /        \\               ",
    "              /  COPILOT \\              ",
    "             /____________\\             ",
    "                  |  |                  ",
    "                  |  |                  ",
    "                 /|  |\\                 ",
]
_ROCKET_EXHAUST = [
    "             ~ ~ ~ ~ ~               ",
    "            ~~ ~ ~ ~ ~~              ",
    "           ~ ~ ~ ~ ~ ~ ~             ",
    "          ~~ ~ ~ ~ ~ ~ ~~            ",
]
_EXPLOSION = """
         . * . * . * . * . * .
       *   \\  |  💥  |  /   *
         * - -BOOM!- - *
       *   /  |  💥  |  \\   *
         * . * . * . * . * . *
"""
_SPINNER = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]


def _show_startup_title():
    console.clear()
    console.print()
    for i, line in enumerate(_TITLE):
        colour = "bold yellow" if i < 6 else "bold green"
        console.print(f"[{colour}]{line}[/{colour}]")
    console.print()
    console.print(Rule("[dim]Copilot Studio · DirectLine · Entra ID Auth[/dim]", style="dim yellow"))
    console.print()


def _user_auth_required() -> bool:
    """AAD user auth is only needed when the token endpoint requires it, or SSO is configured."""
    return (bool(TOKEN_ENDPOINT) and ENDPOINT_NEEDS_AUTH) or bool(AGENT_APP_ID)


def _show_profile_status(profiles: list[dict]) -> list[str]:
    console.print(Panel("[bold cyan]  👾  PROFILE STATUS  👾[/bold cyan]", border_style="cyan"))
    console.print()
    needs_auth = []
    for profile in profiles:
        username = profile["username"]
        display  = profile.get("display_name", username)
        for i in range(11):
            bar = "█" * i + "░" * (10 - i)
            console.print(f"  [dim]{display:<25}[/dim]  [[cyan]{bar}[/cyan]]", end="\r")
            time.sleep(0.04)
        if not _user_auth_required():
            console.print(f"  [green]{display:<25}[/green]  [[bold green]██████████[/bold green]]  [bold green]READY ✓[/bold green]")
            continue
        try:
            get_valid_token(username)
            console.print(f"  [green]{display:<25}[/green]  [[bold green]██████████[/bold green]]  [bold green]READY ✓[/bold green]")
        except RuntimeError:
            console.print(f"  [red]{display:<25}[/red]  [[bold red]░░░░░░░░░░[/bold red]]  [bold red]NEEDS AUTH ✗[/bold red]")
            needs_auth.append(username)
    console.print()
    return needs_auth


def _rocket_auth(username: str) -> bool:
    console.print()
    console.print(Panel(
        f"[bold yellow]  🚀  INITIATING AUTH SEQUENCE FOR  🚀\n  [white]{username}[/white][/bold yellow]",
        border_style="yellow",
    ))
    console.print()
    for line in _ROCKET_BODY:
        console.print(f"[bold cyan]{line}[/bold cyan]")

    result = {"success": False, "done": False}

    def _run():
        result["success"] = authenticate_profile(username)
        result["done"] = True

    threading.Thread(target=_run, daemon=True).start()
    exhaust_cycle = itertools.cycle(_ROCKET_EXHAUST)
    stars = ["✦", "✧", "·", "•", "⋆", "*"]
    while not result["done"]:
        console.print(
            f"[bold orange3]{next(exhaust_cycle)}[/bold orange3]  [dim yellow]{random.choice(stars)}[/dim yellow]",
            end="\r",
        )
        time.sleep(0.15)
    console.print(" " * 60)
    if result["success"]:
        console.print(f"\n  [bold green]✓ AUTH COMPLETE — {username}[/bold green]\n")
    else:
        console.print(f"\n  [bold red]✗ AUTH FAILED — {username}[/bold red]\n")
    time.sleep(0.5)
    return result["success"]


def _bomb_countdown():
    console.print()
    console.print(Rule("[bold red]ALL SYSTEMS GO[/bold red]", style="bold red"))
    console.print()
    fuse_length = 20
    for i in range(fuse_length, -1, -1):
        console.print(
            f"  [bold red]{'~' * (fuse_length - i)}[/bold red][dim]{'─' * i}[/dim] 💣",
            end="\r",
        )
        time.sleep(0.07)
    console.print(" " * 60)
    for n in range(3, 0, -1):
        console.print(f"  [bold red]T-{n}[/bold red]", end="\r")
        time.sleep(0.7)
    console.print(f"  [bold green]GO! 🚀   [/bold green]")
    console.print()


def _check_credentials() -> str:
    """
    Scans Windows Credential Manager for required values.
    Returns 'ok', 'update' (user chose to reconfigure), or 'missing'.
    """
    spinner = itertools.cycle(_SPINNER)
    console.print(Panel("[bold cyan]  🔍  CREDENTIAL CHECK  [/bold cyan]", border_style="cyan"))
    console.print()

    def _spin_check(label: str, key: str) -> str:
        for _ in range(10):
            console.print(f"  [cyan]{next(spinner)}[/cyan]  [dim]{label}[/dim]", end="\r")
            time.sleep(0.05)
        return _load_credential(key)

    tenant_val = _spin_check("Entra Tenant ID",            "CS_TENANT_ID")
    client_val = _spin_check("App Registration Client ID", "CS_CLIENT_ID")
    secret_val = _spin_check("DirectLine Secret",          "CS_DIRECTLINE_SECRET")
    endpt_val  = _spin_check("Token Endpoint URL",         "CS_TOKEN_ENDPOINT")

    tenant_ok = bool(tenant_val and _GUID_RE.match(tenant_val))
    client_ok = bool(client_val and _GUID_RE.match(client_val))
    dl_ok     = bool(secret_val or endpt_val)

    def _row(ok: bool, label: str, note: str = ""):
        icon   = "[bold green]✓[/bold green]" if ok else "[bold red]✗[/bold red]"
        status = "[bold green]FOUND[/bold green]" if ok else "[bold red]MISSING[/bold red]"
        extra  = f"  [dim]{note}[/dim]" if note else ""
        console.print(f"  {icon}  {label:<35} {status}{extra}")

    _row(tenant_ok, "Entra Tenant ID")
    _row(client_ok, "App Registration Client ID")

    if secret_val and endpt_val:
        _row(True, "DirectLine Secret")
        _row(True, "Token Endpoint URL", "(both set — Token Endpoint takes priority)")
    elif secret_val:
        _row(True,  "DirectLine Secret")
        console.print(f"  [dim]─[/dim]  {'Token Endpoint URL':<35} [dim](not needed — DirectLine Secret is used)[/dim]")
    elif endpt_val:
        console.print(f"  [dim]─[/dim]  {'DirectLine Secret':<35} [dim](not needed — Token Endpoint is used)[/dim]")
        _row(True,  "Token Endpoint URL")
    else:
        _row(False, "DirectLine Secret",  "(need Secret or Token Endpoint)")
        _row(False, "Token Endpoint URL", "(need Secret or Token Endpoint)")

    console.print()

    if not (tenant_ok and client_ok and dl_ok):
        console.print(Panel(
            "[bold red]  ✗  Missing credentials — run:  python run.py --setup[/bold red]",
            border_style="red",
        ))
        console.print()
        return "missing"

    console.print(
        "  [bold green]✓  All credentials found.[/bold green]  "
        "[dim]Press [bold]U[/bold] to update, Enter to continue.[/dim]"
    )
    choice = input("  > ").strip().lower()
    console.print()
    return "update" if choice == "u" else "ok"


_ERROR_HINTS = {
    "IntegratedAuthenticationNotSupportedInChannel": (
        "Your bot has 'Authenticate with Microsoft' enabled — switch to Token Endpoint mode.\n\n"
        "  Fix (recommended) — use the Token Endpoint instead of a DirectLine Secret:\n"
        "    python run.py --setup  →  leave 'DirectLine Secret' blank,\n"
        "    paste 'Token Endpoint URL' from  Copilot Studio → Settings → Channels → Direct Line\n\n"
        "  Fix (alternative) — enable SSO token exchange via Bot Client ID:\n"
        "    python run.py --setup  →  fill in 'Bot Client ID'\n"
        "    (Copilot Studio → Settings → Security → Authentication → Client ID)"
    ),
    "Forbidden": (
        "The DirectLine secret may be wrong or the Direct Line channel is not enabled.\n"
        "  • Re-run setup:  python run.py --setup\n"
        "  • Verify:  Copilot Studio → Settings → Channels → Direct Line"
    ),
}

_ERROR_HINTS_DEFAULT = (
    "Check that the bot is published and the Direct Line channel is configured.\n"
    "  Re-run setup if credentials may have changed:  python run.py --setup"
)


def _print_error_hint(error_code: str):
    hint = _ERROR_HINTS.get(error_code, _ERROR_HINTS_DEFAULT)
    console.print(Panel(f"[yellow]{hint}[/yellow]", border_style="yellow", title="[dim]how to fix[/dim]"))
    console.print()


def _preflight_bot_check(profiles: list[dict]) -> bool:
    """
    Per-profile token validation, then a single end-to-end bot ping with 'hi'.
    Returns False and prints a clear error if anything fails.
    """
    console.print(Panel("[bold cyan]  🤖  BOT PRE-FLIGHT CHECK  [/bold cyan]", border_style="cyan"))
    console.print()

    spinner = itertools.cycle(_SPINNER)

    def _spin(label: str):
        for _ in range(14):
            console.print(f"  [cyan]{next(spinner)}[/cyan]  [dim]{label}[/dim]", end="\r")
            time.sleep(0.05)

    # ── Per-profile token check (only when AAD auth is required) ─────────────
    console.print("  [bold]Profile tokens[/bold]")
    console.print()
    aad_token_for_bot = None
    if _user_auth_required():
        for profile in profiles:
            username = profile["username"]
            display  = profile.get("display_name", username)
            _spin(f"Checking token — {display}...")
            try:
                tok = get_valid_token(username)
                if aad_token_for_bot is None:
                    aad_token_for_bot = tok
                console.print(f"  [bold green]✓[/bold green]  {display:<30} [green]token valid[/green]")
            except Exception as e:
                console.print(f"  [bold red]✗[/bold red]  {display:<30} [bold red]FAILED — {e}[/bold red]")
                console.print()
                console.print("  [yellow]Re-run setup and re-authenticate profiles.[/yellow]")
                console.print()
                return False
    else:
        for profile in profiles:
            display = profile.get("display_name", profile["username"])
            console.print(f"  [dim]─[/dim]  {display:<30} [dim]no auth required[/dim]")

    console.print()

    # ── Bot connectivity ping ─────────────────────────────────────────────────
    console.print("  [bold]Bot connectivity[/bold]")
    console.print()

    _spin("Fetching DirectLine token...")
    try:
        dl_token = fetch_directline_token(aad_token_for_bot)
        console.print(f"  [bold green]✓[/bold green]  DirectLine token        [green]OK[/green]")
    except Exception as e:
        console.print(f"  [bold red]✗[/bold red]  DirectLine token        [bold red]FAILED — {e}[/bold red]")
        console.print()
        console.print(Panel(
            "[yellow]  Possible causes:\n\n"
            "  • Wrong DirectLine secret → press U at startup to update\n"
            "  • Direct Line channel not enabled → Copilot Studio → Settings → Channels → Direct Line\n"
            "  • Bot uses Enhanced Authentication → re-run setup and switch to Token Endpoint[/yellow]",
            border_style="yellow",
        ))
        console.print()
        return False

    _spin("Starting conversation...")
    try:
        conversation = start_conversation(dl_token)
        console.print(f"  [bold green]✓[/bold green]  Conversation started    [green]OK[/green]  [dim]({conversation.id[:16]}…)[/dim]")
    except Exception as e:
        console.print(f"  [bold red]✗[/bold red]  Start conversation      [bold red]FAILED — {e}[/bold red]")
        console.print()
        return False

    _spin("Opening WebSocket...")
    try:
        ws = open_websocket(conversation.stream_url)
        console.print(f"  [bold green]✓[/bold green]  WebSocket               [green]OK[/green]")
    except Exception as e:
        console.print(f"  [bold red]✗[/bold red]  WebSocket               [bold red]FAILED — {e}[/bold red]")
        console.print()
        return False

    _spin("Sending 'hi'...")
    try:
        activity_id, _ = send_utterance(conversation, "hi")
        console.print(f"  [bold green]✓[/bold green]  Sent 'hi'               [green]OK[/green]")
    except Exception as e:
        console.print(f"  [bold red]✗[/bold red]  Send utterance          [bold red]FAILED — {e}[/bold red]")
        close_websocket(ws)
        return False

    _spin("Waiting for bot reply...")
    try:
        response = read_response(ws, activity_id, frame_timeout=15.0,
                                 conversation=conversation, aad_token=aad_token_for_bot)
    except Exception as e:
        console.print(f"  [bold red]✗[/bold red]  Bot response            [bold red]FAILED — {e}[/bold red]")
        return False
    finally:
        close_websocket(ws)

    if response.timed_out:
        console.print(f"  [bold red]✗[/bold red]  Bot response            [bold red]NO REPLY (15s timeout)[/bold red]")
        console.print()
        console.print("  [yellow]The bot did not respond. Check the bot is published and the channel is configured.[/yellow]")
        console.print()
        return False

    first_reply = response.activities[0].get("text", "").strip()

    # Treat bot-reported errors as pre-flight failure
    error_code = None
    m = re.search(r"Error code:\s*(\S+)", first_reply)
    if m:
        error_code = m.group(1).rstrip(".")

    if error_code:
        console.print(f"  [bold red]✗[/bold red]  Bot responded with error  [bold red]{error_code}[/bold red]")
        console.print()
        console.print(Panel(
            f"[red]Bot said:[/red]\n\n  [white]{first_reply[:400]}[/white]",
            border_style="red",
            title="[dim]pre-flight response[/dim]",
        ))
        console.print()
        _print_error_hint(error_code)
        return False

    console.print(f"  [bold green]✓[/bold green]  Bot responded           [green]{response.latency_ms:.0f}ms[/green]")
    console.print()
    console.print(Panel(
        f"[bold green]Bot said:[/bold green]\n\n  [white]{first_reply[:300]}[/white]",
        border_style="green",
        title="[dim]pre-flight response[/dim]",
    ))
    console.print()
    return True


def run_startup_sequence(environment, profiles: list[dict]):
    if "--headless" in sys.argv or "-headless" in sys.argv:
        return
    _show_startup_title()

    cred_status = _check_credentials()
    if cred_status == "missing":
        environment.runner.quit()
        return
    if cred_status == "update":
        run_wizard()
        profiles = load_profiles()  # reload in case profiles changed

    needs_auth = _show_profile_status(profiles)
    if needs_auth:
        console.print(Panel(
            f"[yellow]  {len(needs_auth)} profile(s) need sign-in.\n"
            "  Watch the prompts below — open the URL and enter the code.[/yellow]",
            border_style="yellow",
        ))
        console.print()
        for username in needs_auth:
            if not _rocket_auth(username):
                console.print(f"[bold red]Auth failed for {username}. Stopping.[/bold red]")
                environment.runner.quit()
                return

    if not _preflight_bot_check(profiles):
        environment.runner.quit()
        return

    _bomb_countdown()
    console.print(Panel(
        "[bold green]  🌐  TEST LAUNCHED — OPEN YOUR BROWSER  🌐[/bold green]\n\n"
        "  [bold white]http://localhost:8089[/bold white]\n\n"
        "  [dim]Fill in parameters and click [bold]Start[/bold] to begin the assault[/dim]",
        border_style="bold green",
        title="[bold green]READY[/bold green]",
    ))
    console.print()


# ── Setup wizard ──────────────────────────────────────────────────────────────

def _wizard_header():
    console.clear()
    console.print()
    console.print(Panel(
        "[bold cyan]  COPILOT STUDIO LOAD TEST — SETUP  [/bold cyan]\n\n"
        "  [dim]Saves credentials to Windows Credential Manager.[/dim]",
        border_style="cyan",
        title="[bold]SETUP[/bold]",
    ))
    console.print()


def _is_configured() -> bool:
    tenant = _load_credential("CS_TENANT_ID")
    client = _load_credential("CS_CLIENT_ID")
    has_dl = _load_credential("CS_DIRECTLINE_SECRET") or _load_credential("CS_TOKEN_ENDPOINT")
    if not tenant or not client or not has_dl:
        return False
    if not _GUID_RE.match(tenant):
        return False
    return len(load_profiles()) > 0


def _ask(label: str, hint: str = "", optional: bool = False, default: str = "") -> str:
    tag = "[dim](optional)[/dim] " if optional else ""
    prompt_label = f"  {tag}[bold cyan]{label}[/bold cyan]"
    if hint:
        prompt_label += f"\n  [dim]{hint}[/dim]"
    while True:
        value = Prompt.ask(prompt_label, **{"default": default} if default else {})
        if value or optional:
            return value
        console.print("  [bold red]This field is required.[/bold red]")


def _ask_guid(label: str, hint: str = "", default: str = "") -> str:
    while True:
        value = _ask(label, hint=hint, default=default)
        if _GUID_RE.match(value.strip()):
            return value.strip()
        console.print("  [bold red]Must be a GUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).[/bold red]")


def _gather_profiles() -> list[dict]:
    available_csvs = sorted(p.stem for p in UTTERANCES_DIR.glob("*.csv"))
    csv_hint = (
        "Name of the utterance CSV (without .csv) this profile will exclusively handle.\n"
        "  Leave blank to assign automatically by position.\n"
        + (f"  Available: {', '.join(available_csvs)}" if available_csvs else "  (no CSV files in utterances/ yet)")
    )
    profiles = []
    while True:
        username = _ask(
            "Username (UPN)",
            hint="e.g. loadtest.user1@yourcompany.com — will sign in via device code",
        )
        display = _ask(
            "Display name",
            hint="Short label shown in terminal output. Leave blank to use the part before @.",
            optional=True,
            default=username.split("@")[0],
        ) or username.split("@")[0]
        scenario = _ask("Scenario (CSV name)", hint=csv_hint, optional=True)
        scenario = scenario.strip().removesuffix(".csv") if scenario.strip() else ""
        profile: dict = {"username": username, "display_name": display}
        if scenario:
            profile["scenario"] = scenario
        profiles.append(profile)
        label = f"{username} ({display})" + (f" → {scenario}.csv" if scenario else "")
        console.print(f"  [bold green]✓ {label}[/bold green]")
        console.print()
        if not Confirm.ask("  Add another profile?", default=False):
            break
    return profiles


def _save_credentials(config: dict):
    global TENANT_ID, CLIENT_ID, DL_SECRET, AGENT_APP_ID, TOKEN_ENDPOINT, ENDPOINT_NEEDS_AUTH
    import keyring
    for key, value in config.items():
        keyring.set_password(_KR_SERVICE, key, value)
        os.environ[key] = value
    TENANT_ID          = config.get("CS_TENANT_ID",                    TENANT_ID)
    CLIENT_ID          = config.get("CS_CLIENT_ID",                    CLIENT_ID)
    DL_SECRET          = config.get("CS_DIRECTLINE_SECRET",            DL_SECRET)
    AGENT_APP_ID       = config.get("CS_AGENT_APP_ID",                 AGENT_APP_ID)
    TOKEN_ENDPOINT     = config.get("CS_TOKEN_ENDPOINT",               TOKEN_ENDPOINT)
    ENDPOINT_NEEDS_AUTH = config.get("CS_TOKEN_ENDPOINT_REQUIRES_AUTH", "false").lower() == "true"
    console.print("  [bold green]✓ Credentials saved to Windows Credential Manager[/bold green]")


def _write_profiles(profiles: list[dict]):
    PROFILES_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILES_JSON, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)
    console.print(f"  [bold green]✓ profiles.json written ({len(profiles)} profile(s))[/bold green]")


def run_wizard():
    import getpass as _gp

    # ── Mutable wizard state ──────────────────────────────────────────────────
    state = {
        "tenant":              _load_credential("CS_TENANT_ID"),
        "client":              _load_credential("CS_CLIENT_ID"),
        "agent_app":           _load_credential("CS_AGENT_APP_ID"),
        "secret":              _load_credential("CS_DIRECTLINE_SECRET"),
        "endpoint":            _load_credential("CS_TOKEN_ENDPOINT"),
        "endpoint_needs_auth": _load_credential("CS_TOKEN_ENDPOINT_REQUIRES_AUTH").lower() == "true",
        "profiles":            load_profiles(),
    }

    def _auth_badge(username: str) -> str:
        tok = load_token(username)
        if tok and is_token_valid(tok):
            return "[bold green]READY ✓[/bold green]"
        return "[bold red]NEEDS AUTH ✗[/bold red]"

    def _show_menu() -> tuple[list, int]:
        """Renders the numbered parameter list. Returns (rows, add_profile_idx)."""
        console.clear()
        console.print()
        console.print(Panel(
            "[bold cyan]  COPILOT STUDIO LOAD TEST — SETUP  [/bold cyan]\n\n"
            "  [dim]Saves credentials to Windows Credential Manager.[/dim]",
            border_style="cyan",
            title="[bold]SETUP[/bold]",
        ))
        console.print()

        # rows: list of (field_key, label)
        # IMPORTANT: _N_CRED_ROWS must equal the number of non-profile rows appended here.
        _N_CRED_ROWS = 5
        rows: list[tuple[str, str]] = []
        rows.append(("tenant",    "Tenant ID"))
        rows.append(("client",    "App Registration Client ID"))
        rows.append(("agent_app", "Bot Client ID (SSO)"))
        rows.append(("secret",    "DirectLine Secret"))
        rows.append(("endpoint",  "Token Endpoint URL"))
        assert len(rows) == _N_CRED_ROWS, "Update _N_CRED_ROWS to match the number of credential rows"
        for p in state["profiles"]:
            display = p.get("display_name", p["username"])
            rows.append(("profile", f"Profile — {display}"))

        add_idx = len(rows) + 1

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("#",     style="bold cyan", width=4,  no_wrap=True)
        table.add_column("Field", min_width=32, no_wrap=True)
        table.add_column("Value")

        for i, (key, label) in enumerate(rows, 1):
            if key == "tenant":
                val = state["tenant"] or "[dim](not set)[/dim]"
                lbl_style = "bold red" if not state["tenant"] else "dim"
            elif key == "client":
                val = state["client"] or "[dim](not set)[/dim]"
                lbl_style = "bold red" if not state["client"] else "dim"
            elif key == "agent_app":
                val = state["agent_app"] or "[dim](not set — SSO disabled)[/dim]"
                lbl_style = "dim"
            elif key == "secret":
                val = "[green]●●●●●●●● (saved)[/green]" if state["secret"] else "[dim](not set)[/dim]"
                lbl_style = "bold red" if not state["secret"] and not state["endpoint"] else "dim"
            elif key == "endpoint":
                val = state["endpoint"] or "[dim](not set)[/dim]"
                lbl_style = "bold red" if not state["secret"] and not state["endpoint"] else "dim"
            else:  # profile
                profile_idx = i - (_N_CRED_ROWS + 1)
                p = state["profiles"][profile_idx]
                scenario = p.get("scenario", "")
                val = p["username"]
                if scenario:
                    val += f"  →  {scenario}.csv"
                val += "   " + _auth_badge(p["username"])
                lbl_style = "dim"

            table.add_row(f"[{i}]", f"[{lbl_style}]{label}[/{lbl_style}]", val)

        table.add_row(f"[{add_idx}]", "[dim]Add profile[/dim]", "")
        console.print(table)
        console.print()
        console.print("  [dim]Enter number to edit · press [bold]Enter[/bold] to save and continue[/dim]")
        console.print()
        return rows, add_idx

    # ── Main menu loop ────────────────────────────────────────────────────────
    while True:
        rows, add_idx = _show_menu()
        raw = input("  > ").strip()

        if not raw:
            errs = []
            if not state["tenant"] or not _GUID_RE.match(state["tenant"]):
                errs.append("Tenant ID is required and must be a valid GUID.")
            if not state["client"] or not _GUID_RE.match(state["client"]):
                errs.append("App Registration Client ID is required and must be a valid GUID.")
            if not state["secret"].strip() and not state["endpoint"].strip():
                errs.append("DirectLine Secret or Token Endpoint URL is required.")
            if not state["profiles"]:
                errs.append("At least one profile is required.")
            if errs:
                console.print()
                for e in errs:
                    console.print(f"  [bold red]✗  {e}[/bold red]")
                console.print()
                input("  Press Enter to go back...")
                continue
            break

        if not raw.isdigit():
            continue

        sel = int(raw)

        if sel == add_idx:
            console.clear()
            console.print()
            console.print(Rule("[bold]Add Profile[/bold]", style="cyan"))
            console.print()
            new_profiles = _gather_profiles()
            state["profiles"].extend(new_profiles)
            continue

        if sel < 1 or sel > len(rows):
            continue

        key, label = rows[sel - 1]
        console.clear()
        console.print()
        console.print(Rule(f"[bold]Edit: {label}[/bold]", style="cyan"))
        console.print()

        if key == "tenant":
            state["tenant"] = _ask_guid(
                "Tenant ID",
                hint="Azure Portal → Microsoft Entra ID → Overview → Tenant ID",
                default=state["tenant"],
            )

        elif key == "client":
            state["client"] = _ask_guid(
                "App Registration Client ID",
                hint="Azure Portal → App registrations → [your app] → Application (client) ID",
                default=state["client"],
            )

        elif key == "agent_app":
            state["agent_app"] = _ask(
                "Bot Client ID",
                hint=(
                    "Required when your bot uses 'Authenticate with Microsoft' (SSO).\n"
                    "  Copilot Studio → Settings → Security → Authentication → Client ID\n"
                    "  Leave blank if authentication is not configured."
                ),
                optional=True,
                default=state["agent_app"],
            )

        elif key == "secret":
            if state["secret"]:
                console.print("  [dim](press Enter to keep existing value)[/dim]")
                console.print()
            console.print("  [bold cyan]DirectLine Secret[/bold cyan]")
            console.print("  [dim]Copilot Studio → Settings → Channels → Direct Line → Secret keys[/dim]")
            console.print()
            new_val = _gp.getpass("  > ").strip()
            if new_val:
                state["secret"] = new_val

        elif key == "endpoint":
            state["endpoint"] = _ask(
                "Token Endpoint URL",
                hint=(
                    "Copilot Studio → Settings → Channels → Direct Line → Token Endpoint URL\n"
                    "  Must start with https://  Leave blank if using DirectLine Secret instead"
                ),
                optional=True,
                default=state["endpoint"],
            )
            if state["endpoint"].strip():
                state["endpoint_needs_auth"] = Confirm.ask(
                    "  Does this endpoint require an AAD Bearer token?",
                    default=state.get("endpoint_needs_auth", False),
                )

        elif key == "profile":
            _n_cred = 5  # must match the number of credential rows in _show_menu
            profile_idx = sel - (_n_cred + 1)
            p = state["profiles"][profile_idx]
            console.print(f"  [dim]Username:[/dim]  {p['username']}")
            console.print(f"  [dim]Status:  [/dim]  {_auth_badge(p['username'])}")
            console.print()
            console.print("  [1]  Edit username / display name / scenario")
            console.print("  [2]  Re-authenticate now")
            console.print("  [3]  Delete this profile")
            console.print("  [Enter]  Cancel")
            console.print()
            sub = input("  > ").strip()

            if sub == "1":
                console.print()
                username = _ask("Username (UPN)",
                               hint="e.g. loadtest.user1@yourcompany.com",
                               default=p["username"])
                disp_default = username.split("@")[0]
                disp = (_ask("Display name", optional=True,
                            default=p.get("display_name", disp_default))
                        or disp_default)
                available_csvs = sorted(q.stem for q in UTTERANCES_DIR.glob("*.csv"))
                csv_hint = (
                    "Name of the utterance CSV (without .csv). Leave blank for auto-assignment.\n"
                    + (f"  Available: {', '.join(available_csvs)}" if available_csvs else "")
                )
                scenario = _ask("Scenario (CSV name)", hint=csv_hint, optional=True,
                               default=p.get("scenario", ""))
                scenario = scenario.strip().removesuffix(".csv") if scenario.strip() else ""
                new_p: dict = {"username": username, "display_name": disp}
                if scenario:
                    new_p["scenario"] = scenario
                state["profiles"][profile_idx] = new_p

            elif sub == "2":
                console.print()
                _rocket_auth(p["username"])

            elif sub == "3":
                state["profiles"].pop(profile_idx)
                console.print("  [bold yellow]Profile removed.[/bold yellow]")
                time.sleep(0.6)

    # ── Save ──────────────────────────────────────────────────────────────────
    console.clear()
    console.print()
    console.print(Rule("[bold]Saving[/bold]", style="dim"))
    console.print()
    _save_credentials({
        "CS_TENANT_ID":                    state["tenant"],
        "CS_CLIENT_ID":                    state["client"],
        "CS_AGENT_APP_ID":                 state["agent_app"],
        "CS_DIRECTLINE_SECRET":            state["secret"],
        "CS_TOKEN_ENDPOINT":               state["endpoint"],
        "CS_TOKEN_ENDPOINT_REQUIRES_AUTH": "true" if (state["endpoint"].strip() and state.get("endpoint_needs_auth")) else "false",
    })
    _write_profiles(state["profiles"])
    console.print()

    # ── Auth any profiles that still need it ──────────────────────────────────
    needs_auth = [
        p["username"] for p in state["profiles"]
        if not is_token_valid(load_token(p["username"]) or {})
    ]
    if needs_auth:
        console.print(Panel(
            f"[yellow]  {len(needs_auth)} profile(s) need sign-in.\n"
            "  Watch below — open the URL shown and enter the code.[/yellow]",
            border_style="yellow",
        ))
        console.print()
        for username in needs_auth:
            _rocket_auth(username)

    console.print()
    console.print(Panel(
        "[bold green]  Setup complete! Starting Locust...[/bold green]",
        border_style="bold green",
    ))
    console.print()


# ── Locust web UI extension ───────────────────────────────────────────────────

@events.init.add_listener
def _on_locust_init_ui(environment, **kwargs):
    if not hasattr(environment, "web_ui") or environment.web_ui is None:
        return

    app = environment.web_ui.app

    @app.route("/cs-config", methods=["POST"])
    def set_cs_config():
        data = request.get_json(force=True, silent=True) or {}
        test_config["frame_timeout"]  = float(data.get("frame_timeout", 10))
        test_config["think_min"]      = int(data.get("think_min", 30))
        test_config["think_max"]      = int(data.get("think_max", 60))
        test_config["p95_target_ms"]  = int(data.get("p95_target_ms", 2000))
        test_config["max_error_rate"] = float(data.get("max_error_rate", 0.5))
        if data.get("dl_secret"):
            os.environ["CS_DIRECTLINE_SECRET"] = data["dl_secret"]
        if data.get("token_endpoint"):
            os.environ["CS_TOKEN_ENDPOINT"] = data["token_endpoint"]
        return jsonify({"status": "ok", "config": test_config})

    @app.route("/cs-profiles", methods=["GET"])
    def get_profiles():
        return jsonify({"profiles": [r["username"] for r in load_profiles()]})

    profiles_list = load_profiles()
    profile_opts  = "".join(f'<option value="{p["username"]}">{p["username"]}</option>' for p in profiles_list)
    dl_secret_val = _load_credential("CS_DIRECTLINE_SECRET")
    token_ep_val  = _load_credential("CS_TOKEN_ENDPOINT")

    custom_html = f"""
<style>
  #cs-config-panel {{
    background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 6px;
    padding: 20px 24px; margin-bottom: 20px; font-family: inherit;
  }}
  #cs-config-panel h3 {{
    margin-top: 0; margin-bottom: 16px; font-size: 15px; color: #333;
    border-bottom: 1px solid #dee2e6; padding-bottom: 8px;
  }}
  .cs-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; }}
  .cs-field label {{ display: block; font-size: 13px; font-weight: 600; color: #555; margin-bottom: 4px; }}
  .cs-field input, .cs-field select {{
    width: 100%; padding: 6px 8px; border: 1px solid #ced4da;
    border-radius: 4px; font-size: 13px; box-sizing: border-box;
  }}
  .cs-hint {{ font-size: 11px; color: #888; margin-top: 2px; }}
  .cs-section-title {{
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    color: #888; margin: 14px 0 8px; letter-spacing: 0.5px;
  }}
</style>
<div id="cs-config-panel">
  <h3>Copilot Studio Test Configuration</h3>
  <div class="cs-section-title">DirectLine Connection</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>DirectLine Secret</label>
      <input type="password" id="cs-dl-secret" value="{dl_secret_val}" placeholder="From Credential Manager if blank">
      <div class="cs-hint">Leave blank to use stored credential</div>
    </div>
    <div class="cs-field">
      <label>Token Endpoint URL</label>
      <input type="text" id="cs-token-endpoint" value="{token_ep_val}" placeholder="Or use Token Endpoint">
      <div class="cs-hint">Alternative to DirectLine Secret</div>
    </div>
  </div>
  <div class="cs-section-title">Timing</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>Response Timeout (seconds)</label>
      <input type="number" id="cs-frame-timeout" value="10" min="5" max="60">
      <div class="cs-hint">Max wait for each bot frame</div>
    </div>
    <div class="cs-field">
      <label>Think Time Min (seconds)</label>
      <input type="number" id="cs-think-min" value="30" min="5" max="300">
      <div class="cs-hint">Official guidance: 30–60s between turns</div>
    </div>
    <div class="cs-field">
      <label>Think Time Max (seconds)</label>
      <input type="number" id="cs-think-max" value="60" min="5" max="300">
    </div>
  </div>
  <div class="cs-section-title">Success Criteria</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>95th Percentile Target (ms)</label>
      <input type="number" id="cs-p95" value="2000" min="100">
      <div class="cs-hint">Baseline: 2000ms per CS guidance</div>
    </div>
    <div class="cs-field">
      <label>Max Error Rate (%)</label>
      <input type="number" id="cs-error-rate" value="0.5" min="0" max="100" step="0.1">
      <div class="cs-hint">Baseline: 0.5% per CS guidance</div>
    </div>
  </div>
  <div class="cs-section-title">Profiles</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>Active Profile Set</label>
      <select id="cs-profiles">
        <option value="all">All profiles ({len(profiles_list)} loaded)</option>
        {profile_opts}
      </select>
      <div class="cs-hint">{len(profiles_list)} profile(s) found in profiles/profiles.json</div>
    </div>
  </div>
  <div style="margin-top:14px;padding:10px 12px;background:#fff3cd;border:1px solid #ffc107;border-radius:4px;font-size:12px;color:#856404;">
    <strong>Sign-in:</strong> If any profile needs authentication, a sign-in prompt will appear
    in the terminal. Open the URL shown there and enter the code.
  </div>
</div>
<script>
(function() {{
  document.addEventListener('DOMContentLoaded', function() {{
    var startForm = document.querySelector('form[action*="swarm"]') ||
                    document.getElementById('start-form') ||
                    document.querySelector('form');
    if (!startForm) return;
    startForm.addEventListener('submit', function(e) {{
      e.preventDefault(); e.stopImmediatePropagation();
      var config = {{
        dl_secret:      document.getElementById('cs-dl-secret').value,
        token_endpoint: document.getElementById('cs-token-endpoint').value,
        frame_timeout:  parseFloat(document.getElementById('cs-frame-timeout').value),
        think_min:      parseInt(document.getElementById('cs-think-min').value),
        think_max:      parseInt(document.getElementById('cs-think-max').value),
        p95_target_ms:  parseInt(document.getElementById('cs-p95').value),
        max_error_rate: parseFloat(document.getElementById('cs-error-rate').value),
      }};
      fetch('/cs-config', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(config)
      }}).then(function() {{ startForm.submit(); }})
        .catch(function() {{ startForm.submit(); }});
    }}, true);
  }});
}})();
</script>
"""

    @environment.web_ui.app.after_request
    def inject_cs_panel(response):
        if (
            response.content_type.startswith("text/html")
            and (b'id="start-form"' in response.data or b"new-test" in response.data)
        ):
            html = response.get_data(as_text=True)
            html = html.replace('<div class="container">', '<div class="container">' + custom_html, 1)
            response.set_data(html)
        return response


# ── Locust event hooks ────────────────────────────────────────────────────────

@events.init.add_listener
def _on_locust_init(environment, **kwargs):
    if os.environ.get("CS_SETUP_DONE") == "1":
        return  # python run.py already ran the full startup sequence
    profiles = load_profiles()
    run_startup_sequence(environment, profiles)


@events.test_start.add_listener
def _on_test_start(environment, **kwargs):
    if "--headless" not in sys.argv and "-headless" not in sys.argv:
        return
    if not _user_auth_required():
        print("\n[Auth] No AAD auth required — proceeding.\n")
        return
    profiles = load_profiles()
    for profile in profiles:
        token_data = load_token(profile["username"])
        if not token_data or not is_token_valid(token_data):
            print(f"\n[Auth] No valid token for {profile['username']}."
                  " Pre-authenticate all profiles before running headless.\n")
            environment.runner.quit()
            return
    print("\n[Auth] All profiles have valid tokens — proceeding.\n")


# ── Locust User classes ───────────────────────────────────────────────────────

_profiles_list = load_profiles()
_csv_files     = sorted(UTTERANCES_DIR.glob("*.csv"))


def _load_utterances(path: Path) -> list[str]:
    with open(path, newline="") as f:
        rows = [r["utterance"] for r in csv.DictReader(f)]
    if not rows:
        raise RuntimeError(f"Utterances file is empty: {path}")
    return rows


def _fire_metric(environment, name: str, latency_ms: float, error: Exception = None):
    environment.events.request.fire(
        request_type="CopilotStudio", name=name,
        response_time=latency_ms, response_length=0, exception=error,
    )


class CopilotBaseUser(User):
    abstract       = True
    utterances     = []   # class-level list, set per subclass — read-only
    scenario_name  = ""
    fixed_profile  = {}   # pinned at class creation time

    def on_start(self):
        self.profile = self.__class__.fixed_profile
        self.ws           = None
        self.conversation = None
        self._idx         = 0
        self._open_conversation()

    def _open_conversation(self):
        """Opens a fresh DirectLine conversation. Replaces any existing ws/conversation."""
        if self.ws:
            close_websocket(self.ws)
            self.ws = None

        self.aad_token = None
        if _user_auth_required():
            try:
                self.aad_token = get_valid_token(self.profile["username"])
            except RuntimeError as e:
                log.error("Auth failed for %s: %s", self.profile["username"], e)
                raise StopUser()

        try:
            dl_token = fetch_directline_token(self.aad_token)
        except Exception as e:
            log.error("DirectLine token fetch failed: %s", e)
            _fire_metric(self.environment, "Fetch Token", 0, error=e)
            raise StopUser()

        try:
            self.conversation = start_conversation(dl_token)
        except Exception as e:
            log.error("Start conversation failed: %s", e)
            _fire_metric(self.environment, "Start Conversation", 0, error=e)
            raise StopUser()

        try:
            self.ws = open_websocket(self.conversation.stream_url)
        except Exception as e:
            log.error("WebSocket open failed: %s", e)
            _fire_metric(self.environment, "Open WebSocket", 0, error=e)
            raise StopUser()

        self._idx = 0

    def on_stop(self):
        if self.ws:
            close_websocket(self.ws)

    def _send_and_measure(self):
        utterance = self.utterances[self._idx]
        self._idx += 1

        try:
            activity_id, _ = send_utterance(self.conversation, utterance)
        except Exception as e:
            log.error("Send utterance failed: %s", e)
            _fire_metric(self.environment, "Send Utterance", 0, error=e)
            raise StopUser()

        frame_timeout = test_config.get("frame_timeout", 10.0)
        try:
            response = read_response(
                self.ws, activity_id,
                frame_timeout=frame_timeout,
                conversation=self.conversation,
                aad_token=self.aad_token,
            )
        except Exception as e:
            log.error("Read response failed: %s", e)
            _fire_metric(self.environment, "Copilot Response", 0, error=e)
            raise StopUser()

        if response.timed_out:
            log.warning("No bot reply received for activity %s", activity_id)
            _fire_metric(self.environment, "Copilot Response", response.latency_ms,
                         error=Exception("No bot reply received"))
            raise StopUser()

        _fire_metric(self.environment, f"Copilot Response — {self.scenario_name}", response.latency_ms)
        time.sleep(random.randint(test_config.get("think_min", 30), test_config.get("think_max", 60)))

        # All utterances sent — close this conversation and open a fresh one
        if self._idx >= len(self.utterances):
            self._open_conversation()


# Dynamically create one User class per CSV file found in utterances/.
# Each class is pinned to one profile: csv[i] → profile[i % len(profiles)].
# Drop any CSV into utterances/ and it becomes a Locust scenario automatically.
def _make_user_class(class_name: str, utterances: list[str], scenario: str, profile: dict) -> type:
    def send(self):
        self._send_and_measure()
    send.__name__ = "send"
    return type(class_name, (CopilotBaseUser,), {
        "utterances":    utterances,
        "scenario_name": scenario,
        "fixed_profile": profile,
        "weight":        1,
        "send":          task(send),
    })


# Profiles with an explicit scenario field are pinned to that CSV.
# Profiles with no scenario field fill in the remaining CSVs by position.
_pinned     = {p["scenario"]: p for p in _profiles_list if p.get("scenario")}
_unassigned = [p for p in _profiles_list if not p.get("scenario")]

for _i, _csv in enumerate(_csv_files):
    _scenario   = _csv.stem.replace("_", " ").title()
    _class_name = "".join(w.capitalize() for w in _csv.stem.split("_")) + "User"
    if _csv.stem in _pinned:
        _profile = _pinned[_csv.stem]
    elif _unassigned:
        _profile = _unassigned[_i % len(_unassigned)]
    elif _profiles_list:
        _profile = _profiles_list[_i % len(_profiles_list)]
    else:
        _profile = {}
    globals()[_class_name] = _make_user_class(_class_name, _load_utterances(_csv), _scenario, _profile)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _is_configured():
        run_wizard()

    _profiles = load_profiles()

    _show_startup_title()

    _cred_status = _check_credentials()
    if _cred_status == "missing":
        sys.exit(1)
    if _cred_status == "update":
        run_wizard()
        _profiles = load_profiles()

    _needs_auth = _show_profile_status(_profiles)
    if _needs_auth:
        console.print(Panel(
            f"[yellow]  {len(_needs_auth)} profile(s) need sign-in.\n"
            "  Watch the prompts below — open the URL and enter the code.[/yellow]",
            border_style="yellow",
        ))
        console.print()
        for _username in _needs_auth:
            if not _rocket_auth(_username):
                console.print(f"[bold red]Auth failed for {_username}. Stopping.[/bold red]")
                sys.exit(1)

    if not _preflight_bot_check(_profiles):
        sys.exit(1)

    _bomb_countdown()
    console.print(Panel(
        "[bold green]  🌐  TEST LAUNCHED — OPEN YOUR BROWSER  🌐[/bold green]\n\n"
        "  [bold white]http://localhost:8089[/bold white]\n\n"
        "  [dim]Fill in parameters and click [bold]Start[/bold] to begin[/dim]",
        border_style="bold green",
        title="[bold green]READY[/bold green]",
    ))
    console.print()

    os.environ["CS_SETUP_DONE"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "locust", "-f", __file__],
        check=False,
    )
    sys.exit(result.returncode)
