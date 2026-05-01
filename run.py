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
import shutil
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


def _start_device_flow() -> tuple:
    """Start MSAL device code flow. Returns (app, flow) for use by the caller."""
    app = msal.PublicClientApplication(
        CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
    )
    flow = app.initiate_device_flow(scopes=_scopes())
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'unknown')}")
    return app, flow


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


# ── Gum TUI helpers ───────────────────────────────────────────────────────────

# 256-colour constants
_G_CYAN   = "14"
_G_PURPLE = "99"
_G_GREEN  = "82"
_G_YELLOW = "214"
_G_RED    = "196"
_G_DIM    = "240"
_G_WHITE  = "255"
_G_BLACK  = "0"


def _gum_ok() -> bool:
    """Return True if the gum binary is on PATH."""
    return bool(shutil.which("gum"))


def _gum_require():
    """Exit with install instructions if gum is not found."""
    if _gum_ok():
        return
    print()
    print("  Gum is required for the interactive UI.")
    print()
    print("  Install it with one of:")
    print("    Windows : winget install charmbracelet.gum")
    print("    macOS   : brew install gum")
    print("    Scoop   : scoop install charm-gum")
    print("    Manual  : https://github.com/charmbracelet/gum/releases")
    print()
    sys.exit(1)


def _gstyle(text: str, *, border: str = "", fg: str = _G_WHITE,
            border_fg: str = "", bold: bool = False,
            padding: str = "0 1", margin: str = "0 0",
            align: str = "left") -> str:
    """Return Lipgloss-styled text via `gum style`."""
    cmd = ["gum", "style",
           "--foreground", fg,
           "--align",      align,
           "--padding",    padding,
           "--margin",     margin]
    if border:
        cmd += ["--border", border,
                "--border-foreground", border_fg or fg]
    if bold:
        cmd += ["--bold"]
    cmd.append(text)
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout


def _gprint(text: str, **kwargs):
    """Print a gum-styled line (no extra trailing newline)."""
    sys.stdout.write(_gstyle(text, **kwargs))
    sys.stdout.flush()


def _ginput(placeholder: str = "", *, header: str = "", default: str = "",
            password: bool = False, width: int = 72) -> str:
    """Single-line styled input via `gum input`."""
    cmd = ["gum", "input",
           "--placeholder",          placeholder,
           "--prompt",               "  ❯ ",
           "--prompt.foreground",    _G_CYAN,
           "--cursor.foreground",    _G_CYAN,
           "--header.foreground",    _G_DIM,
           "--width",                str(width)]
    if header:
        cmd += ["--header", f"\n  {header}\n"]
    if default:
        cmd += ["--value", default]
    if password:
        cmd += ["--password"]
    r = subprocess.run(cmd, text=True, capture_output=True)
    return r.stdout.strip()


def _gconfirm(prompt: str, *, default: bool = False) -> bool:
    """Yes/No prompt via `gum confirm`. Returns True for Yes."""
    r = subprocess.run([
        "gum", "confirm", prompt,
        "--affirmative",         "Yes",
        "--negative",            "No",
        "--selected.background", _G_CYAN,
        "--selected.foreground", _G_BLACK,
        "--default",             "yes" if default else "no",
    ], text=True)
    return r.returncode == 0


def _gchoose(*items: str, header: str = "", height: int = 12) -> str:
    """Arrow-key selection via `gum choose`. Returns chosen item or ''."""
    cmd = ["gum", "choose",
           "--cursor.foreground",   _G_CYAN,
           "--selected.foreground", _G_GREEN,
           "--header.foreground",   _G_DIM,
           "--cursor",              "▸ ",
           "--height",              str(height)]
    if header:
        cmd += ["--header", header]
    cmd += list(items)
    r = subprocess.run(cmd, text=True, capture_output=True)
    return r.stdout.strip()


def _with_spinner(title: str, fn, *, spinner: str = "dot"):
    """Run fn() while showing a gum spinner. Returns fn's result, re-raises exceptions."""
    done  = threading.Event()
    box   = [None, None]   # [result, exception]

    def _worker():
        try:
            box[0] = fn()
        except Exception as exc:
            box[1] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    spin = subprocess.Popen(
        ["gum", "spin",
         "--spinner",          spinner,
         "--title",            f"  {title}",
         "--spinner.foreground", _G_CYAN,
         "--title.foreground",  _G_WHITE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    done.wait()
    spin.terminate()
    spin.wait()
    print()
    if box[1]:
        raise box[1]
    return box[0]


def _show_startup_title():
    os.system("cls" if os.name == "nt" else "clear")
    if _gum_ok():
        _gprint("\n".join(_TITLE[:6]),  fg=_G_YELLOW, bold=True, padding="0 0", margin="1 0")
        _gprint("\n".join(_TITLE[7:]),  fg=_G_GREEN,  bold=True, padding="0 0", margin="0 0")
        _gprint(
            "  Copilot Studio  ·  DirectLine 3.0  ·  Entra ID Auth",
            fg=_G_DIM, padding="0 0", margin="0 1",
        )
    else:
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
    if _gum_ok():
        _gprint("  PROFILE STATUS", border="rounded", fg=_G_CYAN,
                bold=True, padding="0 3", margin="1 0")
        print()
    else:
        console.print(Panel("[bold cyan]  👾  PROFILE STATUS  👾[/bold cyan]", border_style="cyan"))
        console.print()

    needs_auth = []
    for profile in profiles:
        username = profile["username"]
        display  = profile.get("display_name", username)

        if _gum_ok():
            if not _user_auth_required():
                _gprint(f"  {display:<24}  READY  (no auth required)",
                        fg=_G_GREEN, padding="0 1", margin="0 0")
                continue
            try:
                _with_spinner(f"Checking {display}…",
                              lambda u=username: get_valid_token(u))
                _gprint(f"  {display:<24}  READY ✓",
                        fg=_G_GREEN, bold=True, padding="0 1", margin="0 0")
            except RuntimeError:
                _gprint(f"  {display:<24}  NEEDS AUTH ✗",
                        fg=_G_RED, bold=True, padding="0 1", margin="0 0")
                needs_auth.append(username)
        else:
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

    print()
    return needs_auth


def _rocket_auth(username: str) -> bool:
    if _gum_ok():
        _gprint(
            f"  SIGN IN REQUIRED\n  {username}",
            border="rounded", fg=_G_YELLOW, bold=True,
            padding="1 3", margin="1 0", border_fg=_G_YELLOW,
        )
        try:
            app, flow = _start_device_flow()
        except RuntimeError as exc:
            _gprint(f"  ✗  {exc}", fg=_G_RED, bold=True, padding="0 2", margin="0 1")
            return False
        _gprint(
            f"  1.  Open   →  {flow['verification_uri']}\n"
            f"  2.  Enter  →  {flow['user_code']}",
            border="double", fg=_G_CYAN, bold=True,
            padding="1 4", margin="0 1", border_fg=_G_PURPLE,
        )
        print()
        result = _with_spinner(
            f"Waiting for sign-in…  (browser tab open, enter code above)",
            lambda: app.acquire_token_by_device_flow(flow),
            spinner="moon",
        )
        if "access_token" not in result:
            _gprint(
                f"  ✗  Auth failed — {result.get('error_description', result.get('error', 'unknown'))}",
                fg=_G_RED, bold=True, padding="0 2", margin="0 1",
            )
            return False
        save_token(username, {
            "access_token":  result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "expires_on":    int(result.get("expires_on") or (time.time() + result.get("expires_in", 3600))),
            "username":      username,
        })
        _gprint(f"  ✓  Signed in  —  {username}",
                fg=_G_GREEN, bold=True, padding="0 2", margin="0 1")
        time.sleep(0.5)
        return True

    # ── Fallback: original Rich/rocket animation ───────────────────────────
    console.print()
    console.print(Panel(
        f"[bold yellow]  🚀  INITIATING AUTH SEQUENCE FOR  🚀\n  [white]{username}[/white][/bold yellow]",
        border_style="yellow",
    ))
    console.print()
    for line in _ROCKET_BODY:
        console.print(f"[bold cyan]{line}[/bold cyan]")
    result_box = {"success": False, "done": False}
    def _run():
        result_box["success"] = authenticate_profile(username)
        result_box["done"]    = True
    threading.Thread(target=_run, daemon=True).start()
    exhaust_cycle = itertools.cycle(_ROCKET_EXHAUST)
    stars = ["✦", "✧", "·", "•", "⋆", "*"]
    while not result_box["done"]:
        console.print(
            f"[bold orange3]{next(exhaust_cycle)}[/bold orange3]  [dim yellow]{random.choice(stars)}[/dim yellow]",
            end="\r",
        )
        time.sleep(0.15)
    console.print(" " * 60)
    if result_box["success"]:
        console.print(f"\n  [bold green]✓ AUTH COMPLETE — {username}[/bold green]\n")
    else:
        console.print(f"\n  [bold red]✗ AUTH FAILED — {username}[/bold red]\n")
    time.sleep(0.5)
    return result_box["success"]


def _bomb_countdown():
    if _gum_ok():
        _gprint(
            "  ALL SYSTEMS GO",
            border="double", fg=_G_RED, bold=True,
            padding="0 6", margin="1 0", border_fg=_G_RED,
        )
        fuse_length = 20
        for i in range(fuse_length, -1, -1):
            sys.stdout.write(
                f"  \033[31m{'~' * (fuse_length - i)}\033[2m{'─' * i}\033[0m 💣\r"
            )
            sys.stdout.flush()
            time.sleep(0.07)
        sys.stdout.write(" " * 60 + "\r")
        for n in range(3, 0, -1):
            sys.stdout.write(f"  \033[1;31mT-{n}\033[0m\r")
            sys.stdout.flush()
            time.sleep(0.7)
        _gprint("  GO! 🚀", fg=_G_GREEN, bold=True, padding="0 4", margin="0 1")
        print()
        return
    # Fallback
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
    Scans Credential Manager for required values.
    Returns 'ok', 'update', or 'missing'.
    """
    if _gum_ok():
        _gprint("  CREDENTIAL CHECK", border="rounded", fg=_G_CYAN,
                bold=True, padding="0 3", margin="1 0")
        print()
        tenant_val = _with_spinner("Entra Tenant ID…",            lambda: _load_credential("CS_TENANT_ID"))
        client_val = _with_spinner("App Registration Client ID…",  lambda: _load_credential("CS_CLIENT_ID"))
        secret_val = _with_spinner("DirectLine Secret…",           lambda: _load_credential("CS_DIRECTLINE_SECRET"))
        endpt_val  = _with_spinner("Token Endpoint URL…",          lambda: _load_credential("CS_TOKEN_ENDPOINT"))

        tenant_ok = bool(tenant_val and _GUID_RE.match(tenant_val))
        client_ok = bool(client_val and _GUID_RE.match(client_val))
        dl_ok     = bool(secret_val or endpt_val)

        def _row(ok: bool, label: str, note: str = ""):
            icon   = "✓" if ok else "✗"
            status = "FOUND" if ok else "MISSING"
            note_s = f"  {note}" if note else ""
            _gprint(f"  {icon}  {label:<35} {status}{note_s}",
                    fg=_G_GREEN if ok else _G_RED, padding="0 0")

        def _dim_row(label: str, note: str = ""):
            _gprint(f"  ─  {label:<35} {note}", fg=_G_DIM, padding="0 0")

        _row(tenant_ok, "Entra Tenant ID")
        _row(client_ok, "App Registration Client ID")

        if secret_val and endpt_val:
            _row(True, "DirectLine Secret")
            _row(True, "Token Endpoint URL", "(both set — Token Endpoint takes priority)")
        elif secret_val:
            _row(True, "DirectLine Secret")
            _dim_row("Token Endpoint URL", "(not needed — DirectLine Secret is used)")
        elif endpt_val:
            _dim_row("DirectLine Secret",   "(not needed — Token Endpoint is used)")
            _row(True, "Token Endpoint URL")
        else:
            _row(False, "DirectLine Secret",  "(need Secret or Token Endpoint)")
            _row(False, "Token Endpoint URL", "(need Secret or Token Endpoint)")

        print()

        if not (tenant_ok and client_ok and dl_ok):
            _gprint("  ✗  Missing credentials — run:  python run.py --setup",
                    border="rounded", fg=_G_RED,
                    padding="1 2", margin="0 1", border_fg=_G_RED)
            print()
            return "missing"

        if _gconfirm("  All credentials found. Update before continuing?", default=False):
            return "update"
        return "ok"

    # ── Rich fallback ─────────────────────────────────────────────────────
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
    if _gum_ok():
        _gprint("  BOT PRE-FLIGHT CHECK", border="rounded", fg=_G_CYAN,
                bold=True, padding="0 3", margin="1 0")
    else:
        console.print(Panel("[bold cyan]  🤖  BOT PRE-FLIGHT CHECK  [/bold cyan]", border_style="cyan"))
    print()

    def _ok(label: str, note: str = ""):
        if _gum_ok():
            _gprint(f"  ✓  {label:<30} {note}", fg=_G_GREEN, padding="0 0")
        else:
            console.print(f"  [bold green]✓[/bold green]  {label:<30} [green]{note}[/green]")

    def _fail(label: str, detail: str = ""):
        if _gum_ok():
            _gprint(f"  ✗  {label:<30} {detail}", fg=_G_RED, bold=True, padding="0 0")
        else:
            console.print(f"  [bold red]✗[/bold red]  {label:<30} [bold red]{detail}[/bold red]")

    def _spin(title: str, fn):
        if _gum_ok():
            return _with_spinner(title, fn, spinner="globe")
        # Rich fallback: just run the function (spinner was cosmetic)
        return fn()

    # ── Per-profile token check ───────────────────────────────────────────
    if _gum_ok():
        _gprint("  Profile tokens", bold=True, fg=_G_WHITE, padding="0 0", margin="0 0")
    else:
        console.print("  [bold]Profile tokens[/bold]")
    print()

    aad_token_for_bot = None
    if _user_auth_required():
        for profile in profiles:
            username = profile["username"]
            display  = profile.get("display_name", username)
            try:
                tok = _spin(f"Checking token — {display}…", lambda u=username: get_valid_token(u))
                if aad_token_for_bot is None:
                    aad_token_for_bot = tok
                _ok(display, "token valid")
            except Exception as e:
                _fail(display, f"FAILED — {e}")
                print()
                if _gum_ok():
                    _gprint("  Re-run setup and re-authenticate profiles.",
                            fg=_G_YELLOW, padding="0 2", margin="0 1")
                else:
                    console.print("  [yellow]Re-run setup and re-authenticate profiles.[/yellow]")
                print()
                return False
    else:
        for profile in profiles:
            display = profile.get("display_name", profile["username"])
            if _gum_ok():
                _gprint(f"  ─  {display:<30} no auth required", fg=_G_DIM, padding="0 0")
            else:
                console.print(f"  [dim]─[/dim]  {display:<30} [dim]no auth required[/dim]")

    print()

    # ── Bot connectivity ping ─────────────────────────────────────────────
    if _gum_ok():
        _gprint("  Bot connectivity", bold=True, fg=_G_WHITE, padding="0 0", margin="0 0")
    else:
        console.print("  [bold]Bot connectivity[/bold]")
    print()

    try:
        dl_token = _spin("Fetching DirectLine token…",
                         lambda: fetch_directline_token(aad_token_for_bot))
        _ok("DirectLine token", "OK")
    except Exception as e:
        _fail("DirectLine token", f"FAILED — {e}")
        print()
        if _gum_ok():
            _gprint(
                "  Possible causes:\n"
                "  • Wrong DirectLine secret → press U at startup to update\n"
                "  • Direct Line channel not enabled in Copilot Studio\n"
                "  • Bot uses Enhanced Authentication → switch to Token Endpoint",
                border="rounded", fg=_G_YELLOW,
                padding="1 2", margin="0 1", border_fg=_G_YELLOW,
            )
        else:
            console.print(Panel(
                "[yellow]  Possible causes:\n\n"
                "  • Wrong DirectLine secret → press U at startup to update\n"
                "  • Direct Line channel not enabled → Copilot Studio → Settings → Channels → Direct Line\n"
                "  • Bot uses Enhanced Authentication → re-run setup and switch to Token Endpoint[/yellow]",
                border_style="yellow",
            ))
        print()
        return False

    try:
        conversation = _spin("Starting conversation…",
                             lambda: start_conversation(dl_token))
        _ok("Conversation started", f"OK  ({conversation.id[:16]}…)")
    except Exception as e:
        _fail("Start conversation", f"FAILED — {e}")
        print()
        return False

    try:
        ws = _spin("Opening WebSocket…",
                   lambda: open_websocket(conversation.stream_url))
        _ok("WebSocket", "OK")
    except Exception as e:
        _fail("WebSocket", f"FAILED — {e}")
        print()
        return False

    try:
        activity_id, _ = _spin("Sending 'hi'…",
                               lambda: send_utterance(conversation, "hi"))
        _ok("Sent 'hi'", "OK")
    except Exception as e:
        _fail("Send utterance", f"FAILED — {e}")
        close_websocket(ws)
        return False

    try:
        response = _spin("Waiting for bot reply…",
                         lambda: read_response(ws, activity_id, frame_timeout=15.0,
                                               conversation=conversation,
                                               aad_token=aad_token_for_bot))
    except Exception as e:
        _fail("Bot response", f"FAILED — {e}")
        return False
    finally:
        close_websocket(ws)

    if response.timed_out:
        _fail("Bot response", "NO REPLY (15s timeout)")
        print()
        if _gum_ok():
            _gprint("  The bot did not respond. Check it is published and the channel is configured.",
                    fg=_G_YELLOW, padding="0 2", margin="0 1")
        else:
            console.print("  [yellow]The bot did not respond. Check the bot is published and the channel is configured.[/yellow]")
        print()
        return False

    first_reply = response.activities[0].get("text", "").strip()

    error_code = None
    m = re.search(r"Error code:\s*(\S+)", first_reply)
    if m:
        error_code = m.group(1).rstrip(".")

    if error_code:
        _fail("Bot responded with error", error_code)
        print()
        if _gum_ok():
            _gprint(f"  Bot said:\n\n  {first_reply[:400]}",
                    border="rounded", fg=_G_RED,
                    padding="1 2", margin="0 1", border_fg=_G_RED)
        else:
            console.print(Panel(
                f"[red]Bot said:[/red]\n\n  [white]{first_reply[:400]}[/white]",
                border_style="red", title="[dim]pre-flight response[/dim]",
            ))
        print()
        _print_error_hint(error_code)
        return False

    _ok("Bot responded", f"{response.latency_ms:.0f}ms")
    print()
    if _gum_ok():
        _gprint(f"  Bot said:\n\n  {first_reply[:300]}",
                border="rounded", fg=_G_GREEN,
                padding="1 2", margin="0 1", border_fg=_G_GREEN)
    else:
        console.print(Panel(
            f"[bold green]Bot said:[/bold green]\n\n  [white]{first_reply[:300]}[/white]",
            border_style="green", title="[dim]pre-flight response[/dim]",
        ))
    print()
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

def _is_configured() -> bool:
    tenant = _load_credential("CS_TENANT_ID")
    client = _load_credential("CS_CLIENT_ID")
    has_dl = _load_credential("CS_DIRECTLINE_SECRET") or _load_credential("CS_TOKEN_ENDPOINT")
    if not tenant or not client or not has_dl:
        return False
    if not _GUID_RE.match(tenant):
        return False
    return len(load_profiles()) > 0


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


def _write_profiles(profiles: list[dict]):
    PROFILES_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILES_JSON, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


def run_wizard():
    _gum_require()

    state = {
        "tenant":              _load_credential("CS_TENANT_ID"),
        "client":              _load_credential("CS_CLIENT_ID"),
        "agent_app":           _load_credential("CS_AGENT_APP_ID"),
        "secret":              _load_credential("CS_DIRECTLINE_SECRET"),
        "endpoint":            _load_credential("CS_TOKEN_ENDPOINT"),
        "endpoint_needs_auth": _load_credential("CS_TOKEN_ENDPOINT_REQUIRES_AUTH").lower() == "true",
        "profiles":            load_profiles(),
    }

    _SAVE = "  ✓  Save & continue"
    _ADD  = "  +  Add profile"

    def _val(v: str, *, masked: bool = False, opt_note: str = "") -> str:
        if not v:
            return opt_note or "(not set)"
        if masked:
            return "●●●●●●●● (saved)"
        return (v[:44] + "…") if len(v) > 45 else v

    def _mrow(label: str, value: str, ok: bool | None = None) -> str:
        mark = "  ✓" if ok is True else ("  ✗" if ok is False else "")
        return f"  {label:<26}  {value:<46}{mark}"

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        _gprint(
            "  COPILOT STUDIO  ·  LOAD TEST  ·  SETUP WIZARD\n\n"
            "  Saves credentials to Windows Credential Manager.",
            border="double", fg=_G_CYAN, bold=True,
            border_fg=_G_PURPLE, padding="1 3", margin="1 0",
        )

        t_ok  = bool(state["tenant"]  and _GUID_RE.match(state["tenant"]))
        c_ok  = bool(state["client"]  and _GUID_RE.match(state["client"]))
        dl_ok = bool(state["secret"]  or  state["endpoint"])

        items = [
            _mrow("Tenant ID",           _val(state["tenant"]),
                  t_ok if state["tenant"] else False),
            _mrow("Client ID",           _val(state["client"]),
                  c_ok if state["client"] else False),
            _mrow("Bot Client ID (SSO)", _val(state["agent_app"],
                  opt_note="(optional — blank = SSO disabled)"), None),
            _mrow("DirectLine Secret",   _val(state["secret"], masked=True,
                  opt_note="(not set)"),
                  True if state["secret"] else (False if not state["endpoint"] else None)),
            _mrow("Token Endpoint",      _val(state["endpoint"],
                  opt_note="(not set — Secret is used)" if state["secret"] else "(not set)"),
                  True if state["endpoint"] else None),
        ]
        _N_CRED = len(items)   # must stay in sync with the rows above

        for p in state["profiles"]:
            display  = p.get("display_name", p["username"])
            tok      = load_token(p["username"])
            ready    = bool(tok and is_token_valid(tok))
            scenario = f"  [{p['scenario']}]" if p.get("scenario") else ""
            items.append(_mrow(f"Profile: {display}",
                               p["username"] + scenario, ready))

        items += [_ADD, _SAVE]

        choice = _gchoose(
            *items,
            header="\n  ↑ ↓  navigate     Enter  select\n",
            height=min(len(items) + 6, 24),
        )

        if not choice or choice.strip() == _SAVE.strip():
            errs = []
            if not t_ok:
                errs.append("Tenant ID is required and must be a valid GUID.")
            if not c_ok:
                errs.append("Client ID is required and must be a valid GUID.")
            if not dl_ok:
                errs.append("DirectLine Secret or Token Endpoint URL is required.")
            if not state["profiles"]:
                errs.append("At least one profile is required.")
            if errs:
                _gprint(
                    "\n".join(f"  ✗  {e}" for e in errs),
                    border="rounded", fg=_G_RED,
                    border_fg=_G_RED, padding="1 2", margin="0 1",
                )
                print()
                input("  Press Enter to go back...")
                continue
            break

        if choice.strip() == _ADD.strip():
            os.system("cls" if os.name == "nt" else "clear")
            _gprint("  ADD PROFILE", border="rounded", fg=_G_CYAN,
                    bold=True, padding="0 3", margin="1 0")
            print()
            uname = _ginput(
                "loadtest.user@yourcompany.com",
                header="Username (UPN) — the Microsoft 365 email for this test account",
            )
            if not uname:
                continue
            disp_def = uname.split("@")[0]
            disp = _ginput(
                disp_def,
                header="Display name  (label shown in terminal — press Enter to accept default)",
                default=disp_def,
            ) or disp_def
            available_csvs = sorted(p.stem for p in UTTERANCES_DIR.glob("*.csv"))
            scenario = ""
            if available_csvs:
                csv_pick = _gchoose(
                    "(none — auto assign)", *available_csvs,
                    header=f"\n  Which scenario CSV does this profile use?\n"
                           f"  Available: {', '.join(available_csvs)}\n",
                    height=min(len(available_csvs) + 5, 12),
                )
                scenario = "" if csv_pick.startswith("(none") else csv_pick.strip()
            new_p: dict = {"username": uname, "display_name": disp}
            if scenario:
                new_p["scenario"] = scenario
            state["profiles"].append(new_p)
            _gprint(f"  ✓  Added: {disp}  ({uname})",
                    fg=_G_GREEN, bold=True, padding="0 2", margin="0 1")
            time.sleep(0.8)
            continue

        # ── Match choice back to item index ──────────────────────────────
        idx = next((i for i, it in enumerate(items)
                    if it.strip() == choice.strip()), -1)

        if idx == 0:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                        header="Tenant ID  ·  Azure portal → Microsoft Entra ID → Overview → Tenant ID",
                        default=state["tenant"])
            if v:
                if _GUID_RE.match(v.strip()):
                    state["tenant"] = v.strip()
                else:
                    _gprint("  Not a valid GUID.  Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                            fg=_G_RED, padding="0 2", margin="0 1")
                    time.sleep(1.5)

        elif idx == 1:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                        header="Client ID  ·  Azure portal → App registrations → [your app] → Application (client) ID",
                        default=state["client"])
            if v:
                if _GUID_RE.match(v.strip()):
                    state["client"] = v.strip()
                else:
                    _gprint("  Not a valid GUID.  Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                            fg=_G_RED, padding="0 2", margin="0 1")
                    time.sleep(1.5)

        elif idx == 2:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput(
                "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  (or leave blank to disable SSO)",
                header="Bot Client ID  ·  Copilot Studio → Settings → Security → Authentication → Client ID\n"
                       "  Leave blank if the bot does not use authentication.",
                default=state["agent_app"],
            )
            if v == "" or (v and _GUID_RE.match(v.strip())):
                state["agent_app"] = v.strip()
            elif v:
                _gprint("  Not a valid GUID — and not blank.  Clear the field to disable SSO.",
                        fg=_G_RED, padding="0 2", margin="0 1")
                time.sleep(1.5)

        elif idx == 3:
            os.system("cls" if os.name == "nt" else "clear")
            _gprint(
                "  DirectLine Secret\n\n"
                "  Copilot Studio → Settings → Channels → Direct Line → Secret keys → Show\n"
                "  Leave blank to keep the existing saved value.",
                border="rounded", fg=_G_DIM,
                border_fg=_G_DIM, padding="1 2", margin="0 1",
            )
            print()
            v = _ginput("paste secret here (input is hidden)", password=True)
            if v:
                state["secret"] = v

        elif idx == 4:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput(
                "https://…",
                header="Token Endpoint URL  ·  Copilot Studio → Settings → Channels → Direct Line → Token Endpoint\n"
                       "  Leave blank if using DirectLine Secret instead.",
                default=state["endpoint"],
            )
            state["endpoint"] = v.strip() if v else ""
            if state["endpoint"]:
                state["endpoint_needs_auth"] = _gconfirm(
                    "Does this Token Endpoint require an AAD Bearer token?",
                    default=state.get("endpoint_needs_auth", False),
                )

        elif _N_CRED <= idx < _N_CRED + len(state["profiles"]):
            p_idx = idx - _N_CRED
            p     = state["profiles"][p_idx]
            os.system("cls" if os.name == "nt" else "clear")
            _gprint(
                f"  Profile: {p.get('display_name', p['username'])}\n  {p['username']}",
                border="rounded", fg=_G_CYAN,
                bold=True, padding="1 2", margin="1 0",
            )
            action = _gchoose(
                "  Edit username / display name / scenario",
                "  Re-authenticate now",
                "  Delete this profile",
                "  ← Cancel",
                header="\n  What would you like to do?\n",
                height=8,
            )
            if "Edit" in action:
                uname = _ginput("", header="Username (UPN)",
                                default=p["username"]) or p["username"]
                disp_def = uname.split("@")[0]
                disp = _ginput("", header="Display name",
                               default=p.get("display_name", disp_def)) or disp_def
                available_csvs = sorted(q.stem for q in UTTERANCES_DIR.glob("*.csv"))
                scenario = p.get("scenario", "")
                if available_csvs:
                    csv_pick = _gchoose(
                        "(none)", *available_csvs,
                        header="\n  Scenario CSV\n",
                        height=min(len(available_csvs) + 4, 10),
                    )
                    scenario = "" if csv_pick.strip() == "(none)" else csv_pick.strip()
                new_p: dict = {"username": uname, "display_name": disp}
                if scenario:
                    new_p["scenario"] = scenario
                state["profiles"][p_idx] = new_p

            elif "Re-authenticate" in action:
                print()
                _rocket_auth(p["username"])

            elif "Delete" in action:
                if _gconfirm(f"Delete profile {p.get('display_name', p['username'])}?"):
                    state["profiles"].pop(p_idx)
                    _gprint("  Profile deleted.",
                            fg=_G_YELLOW, padding="0 2", margin="0 1")
                    time.sleep(0.6)

    # ── Save ──────────────────────────────────────────────────────────────────
    os.system("cls" if os.name == "nt" else "clear")
    _gprint("  SAVING…", bold=True, fg=_G_DIM, padding="0 2", margin="1 0")
    _save_credentials({
        "CS_TENANT_ID":                    state["tenant"],
        "CS_CLIENT_ID":                    state["client"],
        "CS_AGENT_APP_ID":                 state["agent_app"],
        "CS_DIRECTLINE_SECRET":            state["secret"],
        "CS_TOKEN_ENDPOINT":               state["endpoint"],
        "CS_TOKEN_ENDPOINT_REQUIRES_AUTH": "true" if (
            state["endpoint"].strip() and state.get("endpoint_needs_auth")) else "false",
    })
    _write_profiles(state["profiles"])
    _gprint("  ✓  Credentials saved to Windows Credential Manager.",
            fg=_G_GREEN, bold=True, padding="0 2", margin="0 1")
    print()

    # ── Auth profiles that still need it ──────────────────────────────────
    needs_auth = [
        p["username"] for p in state["profiles"]
        if not is_token_valid(load_token(p["username"]) or {})
    ]
    if needs_auth:
        _gprint(
            f"  {len(needs_auth)} profile(s) need sign-in.\n"
            "  Open the URL shown below and enter the code in a browser.",
            border="rounded", fg=_G_YELLOW,
            border_fg=_G_YELLOW, padding="1 2", margin="0 1",
        )
        print()
        for username in needs_auth:
            _rocket_auth(username)

    _gprint("  ✓  Setup complete!",
            border="rounded", fg=_G_GREEN,
            border_fg=_G_GREEN, bold=True, padding="1 3", margin="1 0")
    print()


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
