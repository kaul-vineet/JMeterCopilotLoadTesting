"""
auth.py — Run this BEFORE starting Locust.
Authenticates each profile in profiles/profiles.csv via device code flow
and stores encrypted tokens in profiles/.tokens/.

Usage:
    python auth.py                    # authenticate all profiles
    python auth.py --profile user1    # authenticate one profile by username
"""

import os
import sys
import csv
import json
import base64
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
import msal
from cryptography.fernet import Fernet

load_dotenv()

TENANT_ID     = os.getenv("CS_TENANT_ID")
CLIENT_ID     = os.getenv("CS_CLIENT_ID")
AGENT_APP_ID  = os.getenv("CS_AGENT_APP_ID")
ENC_PASSWORD  = os.getenv("TOKEN_ENCRYPTION_PASSWORD", "")

PROFILES_CSV  = Path(__file__).parent / "profiles" / "profiles.csv"
TOKENS_DIR    = Path(__file__).parent / "profiles" / ".tokens"
TOKENS_DIR.mkdir(parents=True, exist_ok=True)

# Scope: Power Platform API (matches Gradio repo auth)
# Falls back to agent app scope if AGENT_APP_ID is set
def _build_scope():
    if AGENT_APP_ID:
        return [f"api://{AGENT_APP_ID}/.default"]
    return ["https://api.powerplatform.com/.default"]

SCOPES = _build_scope()


# ── Encryption key ────────────────────────────────────────────────────────────

def _get_encryption_key() -> bytes:
    """
    Returns a 32-byte Fernet key.
    Primary:  OS keyring (DPAPI on Windows, Keychain on macOS)
    Fallback: PBKDF2 from TOKEN_ENCRYPTION_PASSWORD env var (Linux/Azure)
    """
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
                "Set TOKEN_ENCRYPTION_PASSWORD in your .env file.\n"
            )
            sys.exit(1)
        # PBKDF2 key derivation from password — deterministic, no storage needed
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            ENC_PASSWORD.encode(),
            b"copilot-load-test-salt",
            iterations=260000,
            dklen=32,
        )
        return base64.urlsafe_b64encode(dk)


def _fernet() -> Fernet:
    return Fernet(_get_encryption_key())


# ── Token store ───────────────────────────────────────────────────────────────

def _token_path(username: str) -> Path:
    safe = username.replace("@", "_").replace(".", "_")
    return TOKENS_DIR / f"{safe}.enc"


def save_token(username: str, token_data: dict):
    payload = json.dumps(token_data).encode()
    encrypted = _fernet().encrypt(payload)
    _token_path(username).write_bytes(encrypted)


def load_token(username: str) -> dict | None:
    path = _token_path(username)
    if not path.exists():
        return None
    try:
        decrypted = _fernet().decrypt(path.read_bytes())
        return json.loads(decrypted)
    except Exception:
        return None


def is_token_valid(token_data: dict, min_ttl_seconds: int = 600) -> bool:
    """Returns True if token has at least min_ttl_seconds remaining."""
    exp = token_data.get("expires_on")
    if not exp:
        return False
    expires = datetime.fromtimestamp(exp, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    return (expires - now).total_seconds() > min_ttl_seconds


# ── Token acquisition ─────────────────────────────────────────────────────────

def get_valid_token(username: str) -> str:
    """
    Returns a valid access token for the given username.
    1. Checks encrypted cache — returns immediately if valid (>10min remaining)
    2. Attempts silent MSAL refresh if refresh_token exists
    3. Raises RuntimeError if neither works (user must re-run auth.py)
    """
    token_data = load_token(username)

    if token_data and is_token_valid(token_data):
        return token_data["access_token"]

    # Attempt silent refresh
    if token_data and token_data.get("refresh_token"):
        app = msal.PublicClientApplication(
            CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        )
        result = app.acquire_token_by_refresh_token(
            token_data["refresh_token"], scopes=SCOPES
        )
        if "access_token" in result:
            _merge_and_save(username, token_data, result)
            return result["access_token"]

    raise RuntimeError(
        f"No valid token for {username}. Run: python auth.py --profile {username}"
    )


def _merge_and_save(username: str, existing: dict, new_result: dict):
    existing.update({
        "access_token":  new_result["access_token"],
        "expires_on":    new_result.get("expires_on", 0),
        "refresh_token": new_result.get("refresh_token", existing.get("refresh_token")),
    })
    save_token(username, existing)


# ── Device code flow ──────────────────────────────────────────────────────────

def authenticate_profile(username: str):
    """
    Runs interactive device code flow for one profile.
    Prints sign-in URL and code to terminal. Blocks until user completes sign-in.
    """
    print(f"\n{'='*60}")
    print(f"  Authenticating: {username}")
    print(f"{'='*60}")

    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    )

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")

    # Print sign-in instructions prominently
    print(f"\n  1. Open:  {flow['verification_uri']}")
    print(f"  2. Enter: {flow['user_code']}")
    print(f"\n  Waiting for sign-in", end="", flush=True)

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "Unknown error"))
        print(f"\n  [FAILED] {error}")
        return False

    save_token(username, {
        "access_token":  result["access_token"],
        "refresh_token": result.get("refresh_token", ""),
        "expires_on":    result.get("expires_on", 0),
        "username":      username,
    })
    print(f"\n  [OK] Token saved for {username}")
    return True


# ── Profile loader ────────────────────────────────────────────────────────────

def load_profiles() -> list[dict]:
    if not PROFILES_CSV.exists():
        print(f"[ERROR] profiles.csv not found at {PROFILES_CSV}")
        sys.exit(1)
    with open(PROFILES_CSV, newline="") as f:
        return list(csv.DictReader(f))


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Authenticate Copilot Studio load test profiles")
    parser.add_argument("--profile", help="Authenticate a single profile by username")
    parser.add_argument("--force", action="store_true", help="Re-authenticate even if token is valid")
    args = parser.parse_args()

    if not TENANT_ID or not CLIENT_ID:
        print("[ERROR] CS_TENANT_ID and CS_CLIENT_ID must be set in .env")
        sys.exit(1)

    profiles = load_profiles()

    if args.profile:
        profiles = [p for p in profiles if p["username"] == args.profile]
        if not profiles:
            print(f"[ERROR] Profile '{args.profile}' not found in profiles.csv")
            sys.exit(1)

    success = 0
    for profile in profiles:
        username = profile["username"]

        if not args.force:
            existing = load_token(username)
            if existing and is_token_valid(existing):
                print(f"[SKIP] {username} — token valid, skipping (use --force to re-authenticate)")
                success += 1
                continue

        if authenticate_profile(username):
            success += 1

    print(f"\n{'='*60}")
    print(f"  Done: {success}/{len(profiles)} profiles authenticated")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
