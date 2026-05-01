"""
setup.py — First-run setup wizard for Copilot Studio Load Test.

Collects all configuration interactively, writes .env and profiles/profiles.csv,
then optionally authenticates every test profile.

Usage:
    python setup.py
"""

import csv
import getpass
import importlib
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table

console = Console()

ENV_PATH      = Path(__file__).parent / ".env"
PROFILES_PATH = Path(__file__).parent / "profiles" / "profiles.csv"

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _ask(
    label: str,
    hint: str = "",
    secret: bool = False,
    optional: bool = False,
    default: str = "",
) -> str:
    tag = "[dim](optional)[/dim] " if optional else ""
    prompt_label = f"  {tag}[bold cyan]{label}[/bold cyan]"
    if hint:
        prompt_label += f"\n  [dim]{hint}[/dim]"

    while True:
        if secret:
            console.print(prompt_label)
            if default:
                console.print("  [dim](press Enter to keep existing value)[/dim]")
            value = getpass.getpass("  > ")
            if not value:
                value = default
        else:
            kwargs = {"default": default} if default else {}
            value = Prompt.ask(prompt_label, **kwargs)

        if value or optional:
            return value
        console.print("  [bold red]This field is required.[/bold red]")


def _ask_guid(label: str, hint: str = "", default: str = "") -> str:
    while True:
        value = _ask(label, hint=hint, default=default)
        if _GUID_RE.match(value.strip()):
            return value.strip()
        console.print("  [bold red]Must be a GUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).[/bold red]")


# ── Header ────────────────────────────────────────────────────────────────────

def _show_header():
    console.clear()
    console.print()
    console.print(Panel(
        "[bold cyan]  COPILOT STUDIO LOAD TEST — SETUP WIZARD  [/bold cyan]\n\n"
        "  [dim]Configures .env and profiles.csv, then authenticates test users.[/dim]",
        border_style="cyan",
        title="[bold]SETUP[/bold]",
    ))
    console.print()


# ── Step 1 — Configuration ────────────────────────────────────────────────────

def _collect_config() -> dict:
    existing = {}
    if ENV_PATH.exists():
        existing = {
            k: v for k, v in dotenv_values(ENV_PATH).items()
            if v and not v.startswith("your-") and not v.startswith("https://your")
        }
        if existing:
            console.print(Panel(
                "[yellow]Existing .env found — press Enter on any field to keep the current value.[/yellow]",
                border_style="yellow",
            ))
            console.print()

    config = {}

    # Entra ID
    console.print(Rule("[bold]Entra ID[/bold]", style="cyan"))
    console.print()
    config["CS_TENANT_ID"] = _ask_guid(
        "Tenant ID",
        hint="Azure Portal → Entra ID → Overview → Tenant ID",
        default=existing.get("CS_TENANT_ID", ""),
    )
    config["CS_CLIENT_ID"] = _ask_guid(
        "App Registration Client ID",
        hint="Azure Portal → App registrations → your app → Application (client) ID",
        default=existing.get("CS_CLIENT_ID", ""),
    )
    console.print()

    # DirectLine
    console.print(Rule("[bold]DirectLine Connection[/bold]", style="cyan"))
    console.print()
    console.print("  Choose one:\n"
                  "    [bold]1[/bold]  DirectLine secret      [dim](Copilot Studio → Channels → Direct Line)[/dim]\n"
                  "    [bold]2[/bold]  Custom token endpoint  [dim](if your bot uses a token service)[/dim]")
    console.print()

    has_endpoint = (
        existing.get("CS_TOKEN_ENDPOINT", "")
        and not existing.get("CS_TOKEN_ENDPOINT", "").startswith("https://your")
    )
    dl_default = "2" if has_endpoint else "1"

    while True:
        choice = input(f"  > [{dl_default}] ").strip() or dl_default
        if choice in ("1", "2"):
            break
        console.print("  [bold red]Enter 1 or 2.[/bold red]")

    console.print()
    if choice == "1":
        secret = _ask(
            "DirectLine Secret",
            hint="Copilot Studio → Settings → Channels → Direct Line → copy the secret key",
            secret=True,
            default=existing.get("CS_DIRECTLINE_SECRET", ""),
        )
        config["CS_DIRECTLINE_SECRET"]       = secret
        config["CS_TOKEN_ENDPOINT"]           = ""
        config["CS_TOKEN_ENDPOINT_REQUIRES_AUTH"] = "false"
    else:
        config["CS_DIRECTLINE_SECRET"] = ""
        config["CS_TOKEN_ENDPOINT"] = _ask(
            "Token Endpoint URL",
            hint="https://...",
            default=existing.get("CS_TOKEN_ENDPOINT", ""),
        )
        needs_auth = Confirm.ask(
            "  Does this endpoint require an AAD Bearer token?",
            default=existing.get("CS_TOKEN_ENDPOINT_REQUIRES_AUTH", "false") == "true",
        )
        config["CS_TOKEN_ENDPOINT_REQUIRES_AUTH"] = "true" if needs_auth else "false"
    console.print()

    # Agent App ID
    console.print(Rule("[bold]Agent Identity[/bold]", style="cyan"))
    console.print()
    console.print(
        "  [dim]Required when your bot's OAuth scope is api://<app-id>/.default.\n"
        "  Leave blank to use the default Power Platform API scope.[/dim]"
    )
    console.print()
    config["CS_AGENT_APP_ID"] = _ask(
        "Agent App ID",
        hint="Copilot Studio → Settings → Advanced → Application ID",
        optional=True,
        default=existing.get("CS_AGENT_APP_ID", ""),
    )
    console.print()

    # Token encryption password
    console.print(Rule("[bold]Token Encryption[/bold]", style="cyan"))
    console.print()
    console.print(
        "  [dim]Windows uses Credential Manager automatically — leave blank.\n"
        "  Required only on Linux or Azure DevOps where keyring is unavailable.[/dim]"
    )
    console.print()
    config["TOKEN_ENCRYPTION_PASSWORD"] = _ask(
        "Token Encryption Password",
        hint="min 16 characters — leave blank on Windows",
        secret=True,
        optional=True,
        default=existing.get("TOKEN_ENCRYPTION_PASSWORD", ""),
    )

    return config


# ── Step 2 — Profiles ─────────────────────────────────────────────────────────

def _collect_profiles() -> list[dict]:
    console.print()
    console.print(Rule("[bold]Test User Profiles[/bold]", style="cyan"))
    console.print()

    existing: list[dict] = []
    if PROFILES_PATH.exists():
        with open(PROFILES_PATH, newline="") as f:
            existing = list(csv.DictReader(f))

    if existing:
        table = Table(show_header=True, header_style="bold cyan", box=None)
        table.add_column("Username", style="white")
        table.add_column("Display Name", style="dim")
        for p in existing:
            table.add_row(p["username"], p.get("display_name", ""))
        console.print(table)
        console.print()

        if not Confirm.ask("  Replace existing profiles?", default=False):
            if Confirm.ask("  Add more profiles?", default=False):
                return existing + _gather_profiles()
            return existing
        console.print()

    console.print("  [dim]Add the user accounts that will run the load test.[/dim]")
    console.print()
    return _gather_profiles()


def _gather_profiles() -> list[dict]:
    profiles = []
    while True:
        username = _ask("Username (UPN)", hint="e.g. testuser@contoso.com")
        default_display = username.split("@")[0]
        display = _ask(
            "Display name",
            hint="shown in terminal output",
            optional=True,
            default=default_display,
        ) or default_display
        profiles.append({"username": username, "display_name": display})
        console.print(f"  [bold green]✓ Added {username}[/bold green]")
        console.print()
        if not Confirm.ask("  Add another profile?", default=False):
            break
    return profiles


# ── Step 3 — Write files ──────────────────────────────────────────────────────

def _write_env(config: dict):
    lines = [
        "# Entra ID app registration",
        f"CS_TENANT_ID={config['CS_TENANT_ID']}",
        f"CS_CLIENT_ID={config['CS_CLIENT_ID']}",
        "",
        "# DirectLine connection — set ONE of these two",
        f"CS_DIRECTLINE_SECRET={config.get('CS_DIRECTLINE_SECRET', '')}",
        f"CS_TOKEN_ENDPOINT={config.get('CS_TOKEN_ENDPOINT', '')}",
        "",
        "# Set this if your token endpoint requires user auth (AAD Bearer token)",
        f"CS_TOKEN_ENDPOINT_REQUIRES_AUTH={config.get('CS_TOKEN_ENDPOINT_REQUIRES_AUTH', 'false')}",
        "",
        "# Agent identity (from Copilot Studio environment settings)",
        f"CS_AGENT_APP_ID={config.get('CS_AGENT_APP_ID', '')}",
        "",
        "# Token encryption fallback — required when keyring is unavailable (Linux/Azure)",
        f"TOKEN_ENCRYPTION_PASSWORD={config.get('TOKEN_ENCRYPTION_PASSWORD', '')}",
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print("  [bold green]✓ .env written[/bold green]")


def _write_profiles(profiles: list[dict]):
    PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILES_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["username", "display_name"])
        writer.writeheader()
        writer.writerows(profiles)
    console.print(f"  [bold green]✓ profiles.csv written ({len(profiles)} profile(s))[/bold green]")


# ── Step 4 — Auth ─────────────────────────────────────────────────────────────

def _run_auth(profiles: list[dict]):
    load_dotenv(override=True)

    import auth as auth_module
    importlib.reload(auth_module)

    success = 0
    for profile in profiles:
        username = profile["username"]
        token = auth_module.load_token(username)
        if token and auth_module.is_token_valid(token):
            console.print(f"  [dim]SKIP {username} — token still valid[/dim]")
            success += 1
            continue
        if auth_module.authenticate_profile(username):
            success += 1

    console.print()
    status = "bold green" if success == len(profiles) else "bold yellow"
    console.print(Panel(
        f"[{status}]  {success} / {len(profiles)} profiles authenticated[/{status}]",
        border_style="green" if success == len(profiles) else "yellow",
    ))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _show_header()

    console.print(Panel("[bold]Step 1 of 4 — Configuration[/bold]", border_style="dim"))
    console.print()
    config = _collect_config()

    console.print()
    console.print(Panel("[bold]Step 2 of 4 — Test Profiles[/bold]", border_style="dim"))
    profiles = _collect_profiles()

    console.print()
    console.print(Panel("[bold]Step 3 of 4 — Writing Files[/bold]", border_style="dim"))
    console.print()
    _write_env(config)
    _write_profiles(profiles)

    console.print()
    if Confirm.ask("  Step 4 of 4 — Authenticate profiles now?", default=True):
        console.print()
        _run_auth(profiles)

    console.print()
    console.print(Panel(
        "[bold green]  Setup complete![/bold green]\n\n"
        "  Start the load test:\n"
        "  [bold white]    locust -f locustfile.py,ui.py[/bold white]\n\n"
        "  Then open [bold white]http://localhost:8089[/bold white]",
        border_style="bold green",
        title="[bold green]DONE[/bold green]",
    ))
    console.print()


if __name__ == "__main__":
    main()
