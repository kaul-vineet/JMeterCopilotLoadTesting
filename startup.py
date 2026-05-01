"""
startup.py — Jazzy terminal startup sequence for Copilot Studio Load Test.
Handles credential checks, animated prompts, rocket auth, and bomb countdown.
"""

import os
import sys
import time
import getpass
import itertools
import threading
import random

import colorama
colorama.init()

from rich.console import Console
from rich.text import Text
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.align import Align
from rich.columns import Columns
from rich.rule import Rule
from rich import box

console = Console()

# ── ASCII Art ─────────────────────────────────────────────────────────────────

TITLE = [
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

ROCKET_BODY = [
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

ROCKET_EXHAUST = [
    "             ~ ~ ~ ~ ~               ",
    "            ~~ ~ ~ ~ ~~              ",
    "           ~ ~ ~ ~ ~ ~ ~             ",
    "          ~~ ~ ~ ~ ~ ~ ~~            ",
]

BOOM_FRAMES = [
    """
                    💣
              ~~~~
    """,
    """
                    💣
           ~~~~~~~~~
    """,
    """
                    💣
      ~~~~~~~~~~~~~~
    """,
    """
                    💣
  ~~~~~~~~~~~~~~~~
    """,
]

EXPLOSION = """
         . * . * . * . * . * .
       *   \\  |  💥  |  /   *
         * - -BOOM!- - *
       *   /  |  💥  |  \\   *
         * . * . * . * . * . *
"""

CREEPER = """
  ┌──────────────────────────┐
  │  ██████  ██████          │
  │  ██████  ██████          │
  │        ██                │
  │   ████████████           │
  │   ██  ████  ██           │
  │   ██  ████  ██           │
  │       ████               │
  └──────────────────────────┘
"""

# ── Step 1 — Minecraft Title Crawl ───────────────────────────────────────────

def show_title():
    console.clear()
    time.sleep(0.3)

    # Dirt background panel effect
    console.print()
    console.print(Panel(
        "[bold orange3]  L O A D I N G   W O R L D . . .[/bold orange3]",
        style="on dark_orange3",
        border_style="orange3",
        width=60,
    ))
    time.sleep(0.8)
    console.clear()
    console.print()

    # Minecraft-style character crawl — each line revealed char by char
    for i, line in enumerate(TITLE):
        colour = "bold yellow" if i < 6 else "bold green"
        for char in line:
            console.print(f"[{colour}]{char}[/{colour}]", end="")
            sys.stdout.flush()
            time.sleep(0.003)
        console.print()

    console.print()
    console.print(Rule("[dim]Copilot Studio · DirectLine · Entra ID Auth[/dim]", style="dim yellow"))
    console.print()
    time.sleep(0.6)


# ── Step 2 — Credential Scan ─────────────────────────────────────────────────

CREDENTIAL_KEYS = [
    ("CS_TENANT_ID",               "Entra ID Tenant"),
    ("CS_CLIENT_ID",               "App Registration Client"),
    ("CS_DIRECTLINE_SECRET",       "DirectLine Secret"),
    ("CS_TOKEN_ENDPOINT",          "Token Endpoint"),
]

SPINNER_FRAMES = ["⣾","⣽","⣻","⢿","⡿","⣟","⣯","⣷"]

def scan_credentials() -> list[tuple[str, str]]:
    """
    Scans Windows Credential Manager for each required key.
    Returns list of (key, label) tuples that are missing.
    """
    try:
        import keyring as kr
        use_keyring = True
    except Exception:
        use_keyring = False

    console.print(Panel(
        "[bold cyan]  🔍  SCANNING WINDOWS CREDENTIAL MANAGER  🔍[/bold cyan]",
        border_style="cyan",
    ))
    console.print()

    missing = []
    spinner = itertools.cycle(SPINNER_FRAMES)

    for key, label in CREDENTIAL_KEYS:
        for _ in range(12):
            console.print(
                f"  [cyan]{next(spinner)}[/cyan]  [dim]{label}[/dim]",
                end="\r"
            )
            time.sleep(0.06)

        value = None
        if use_keyring:
            value = kr.get_password("copilot-load-test", key)
        if not value:
            value = os.getenv(key, "")

        if value:
            console.print(
                f"  [bold green]✓[/bold green]  {label:<35} [green]FOUND[/green]     "
            )
        else:
            # Only flag as missing if it's actually needed
            # Either DIRECTLINE_SECRET or TOKEN_ENDPOINT is enough — not both required
            if key == "CS_TOKEN_ENDPOINT" and _is_found("CS_DIRECTLINE_SECRET", use_keyring):
                console.print(
                    f"  [dim]─[/dim]  {label:<35} [dim]SKIPPED (using secret)[/dim]"
                )
                continue
            if key == "CS_DIRECTLINE_SECRET" and _is_found("CS_TOKEN_ENDPOINT", use_keyring):
                console.print(
                    f"  [dim]─[/dim]  {label:<35} [dim]SKIPPED (using endpoint)[/dim]"
                )
                continue
            console.print(
                f"  [bold red]✗[/bold red]  {label:<35} [bold red]MISSING[/bold red]  "
            )
            missing.append((key, label))

    console.print()
    return missing


def _is_found(key: str, use_keyring: bool) -> bool:
    if use_keyring:
        try:
            import keyring as kr
            val = kr.get_password("copilot-load-test", key)
            if val:
                return True
        except Exception:
            pass
    return bool(os.getenv(key, ""))


# ── Step 3 — Credential Prompts ───────────────────────────────────────────────

def prompt_missing_credentials(missing: list[tuple[str, str]]):
    """Prompts user for missing credentials and saves to keyring."""
    if not missing:
        return

    # Flashing warning
    for _ in range(4):
        console.print(
            "[bold red on white]  ⚠  MISSING CREDENTIALS DETECTED  ⚠  [/bold red on white]",
            justify="center"
        )
        time.sleep(0.25)
        console.print(" " * 50, end="\r")
        time.sleep(0.15)

    console.print()
    console.print(Panel(
        "[yellow]Enter the missing values below.\n"
        "They will be saved to [bold]Windows Credential Manager[/bold] — never stored in plain text.[/yellow]",
        border_style="yellow",
    ))
    console.print()

    try:
        import keyring as kr
        use_keyring = True
    except Exception:
        use_keyring = False

    for key, label in missing:
        # Animated prompt cursor
        for _ in range(3):
            console.print(f"  [bold yellow]>[/bold yellow] {label}: ", end="\r")
            time.sleep(0.2)
            console.print(f"  [bold cyan]>[/bold cyan] {label}: ", end="\r")
            time.sleep(0.2)

        console.print(f"  [bold green]>[/bold green] {label}: ", end="")
        value = getpass.getpass("")

        if use_keyring:
            kr.set_password("copilot-load-test", key, value)
        os.environ[key] = value

        console.print(
            f"  [bold green]✓[/bold green]  {label} saved to Windows Credential Manager"
        )
        time.sleep(0.2)

    console.print()


# ── Step 4 — Profile Status ───────────────────────────────────────────────────

def show_profile_status(profiles: list[dict]) -> list[str]:
    """
    Shows health bar for each profile.
    Returns list of usernames needing auth.
    """
    from auth import load_token, is_token_valid

    console.print(Panel(
        "[bold cyan]  👾  PROFILE STATUS  👾[/bold cyan]",
        border_style="cyan",
    ))
    console.print()

    needs_auth = []

    for profile in profiles:
        username = profile["username"]
        display  = profile.get("display_name", username)
        token    = load_token(username)

        for i in range(11):
            bar   = "█" * i + "░" * (10 - i)
            console.print(
                f"  [dim]{display:<25}[/dim]  [[cyan]{bar}[/cyan]]",
                end="\r"
            )
            time.sleep(0.04)

        if token and is_token_valid(token):
            console.print(
                f"  [green]{display:<25}[/green]  [[bold green]██████████[/bold green]]  [bold green]READY ✓[/bold green]"
            )
        else:
            console.print(
                f"  [red]{display:<25}[/red]  [[bold red]░░░░░░░░░░[/bold red]]  [bold red]NEEDS AUTH ✗[/bold red]"
            )
            needs_auth.append(username)

    console.print()
    return needs_auth


# ── Step 5 — Rocket Launch (device code auth) ─────────────────────────────────

def rocket_launch(username: str, auth_fn) -> bool:
    """
    Shows rocket animation while waiting for device code sign-in.
    auth_fn is called and runs the device code flow.
    Returns True if auth succeeded.
    """
    console.print()
    console.print(Panel(
        f"[bold yellow]  🚀  INITIATING AUTH SEQUENCE FOR  🚀\n"
        f"  [white]{username}[/white][/bold yellow]",
        border_style="yellow",
    ))
    console.print()

    # Print static rocket body
    for line in ROCKET_BODY:
        console.print(f"[bold cyan]{line}[/bold cyan]")

    # Auth runs in background thread — rocket exhaust animates in foreground
    result = {"success": False, "done": False}

    def run_auth():
        result["success"] = auth_fn(username)
        result["done"] = True

    auth_thread = threading.Thread(target=run_auth, daemon=True)
    auth_thread.start()

    exhaust_cycle = itertools.cycle(ROCKET_EXHAUST)
    stars = ["✦", "✧", "·", "•", "⋆", "*"]

    while not result["done"]:
        exhaust = next(exhaust_cycle)
        star = random.choice(stars)
        console.print(
            f"[bold orange3]{exhaust}[/bold orange3]  [dim yellow]{star}[/dim yellow]",
            end="\r"
        )
        time.sleep(0.15)

    auth_thread.join()
    console.print(" " * 60)

    if result["success"]:
        console.print(
            f"\n  [bold green]✓ AUTH COMPLETE — {username}[/bold green]\n"
        )
    else:
        console.print(
            f"\n  [bold red]✗ AUTH FAILED — {username}[/bold red]\n"
        )
    time.sleep(0.5)
    return result["success"]


# ── Step 6 — Bomb Countdown ───────────────────────────────────────────────────

def bomb_countdown():
    console.print()
    console.print(Rule("[bold red]ALL SYSTEMS GO[/bold red]", style="bold red"))
    console.print()

    # Bomb with burning fuse
    fuse_length = 20
    for i in range(fuse_length, -1, -1):
        fuse_lit   = "[bold red]" + "~" * (fuse_length - i) + "[/bold red]"
        fuse_unlit = "[dim]" + "─" * i + "[/dim]"
        console.print(
            f"  {fuse_lit}{fuse_unlit} 💣",
            end="\r"
        )
        time.sleep(0.07)

    console.print(" " * 60)

    # Countdown
    for n in range(3, 0, -1):
        console.print(
            Panel(
                f"[bold red]  T - {n}  [/bold red]",
                border_style="bold red",
                width=20,
            ),
            justify="center"
        )
        time.sleep(0.7)
        console.print("\033[4A", end="")  # move cursor up 4 lines

    console.print(" " * 60)
    console.print(" " * 60)
    console.print(" " * 60)
    console.print(" " * 60)
    console.print()

    # BOOM
    console.print(Panel(
        EXPLOSION,
        style="bold yellow on red",
        border_style="bold yellow",
    ))
    time.sleep(0.4)
    console.print()


# ── Step 7 — Ready Message ────────────────────────────────────────────────────

def ready_message():
    console.print(Panel(
        "[bold green]  🌐  TEST LAUNCHED — OPEN YOUR BROWSER  🌐[/bold green]\n\n"
        "  [bold white]http://localhost:8089[/bold white]\n\n"
        "  [dim]Fill in parameters and click [bold]Start[/bold] to begin the assault[/dim]",
        border_style="bold green",
        title="[bold green]READY[/bold green]",
    ))
    console.print()


# ── Main sequence ─────────────────────────────────────────────────────────────

def run_startup_sequence(environment, profiles: list[dict]):
    """
    Full startup sequence. Called from locustfile.py init event.
    Skipped automatically in headless mode.
    """
    headless = "--headless" in sys.argv or "-headless" in sys.argv
    if headless:
        return

    from auth import authenticate_profile

    # Step 1 — Title
    show_title()

    # Step 2 — Credential scan
    missing_creds = scan_credentials()

    # Step 3 — Prompt for missing credentials
    if missing_creds:
        prompt_missing_credentials(missing_creds)

    # Step 4 — Profile status
    needs_auth = show_profile_status(profiles)

    # Step 5 — Rocket auth for profiles that need it
    if needs_auth:
        console.print(Panel(
            f"[yellow]  {len(needs_auth)} profile(s) need sign-in.\n"
            "  Watch the prompts below — open the URL and enter the code.[/yellow]",
            border_style="yellow",
        ))
        console.print()

        for username in needs_auth:
            success = rocket_launch(username, authenticate_profile)
            if not success:
                console.print(
                    f"[bold red]Auth failed for {username}. Stopping.[/bold red]"
                )
                environment.runner.quit()
                return

    # Step 6 — Bomb countdown
    bomb_countdown()

    # Step 7 — Ready
    ready_message()
