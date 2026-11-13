"""`flatwatch init` and `flatwatch agent ...`: first-time setup and the macOS LaunchAgent.

The LaunchAgent runs `flatwatch run` in your GUI session (needed for the Chrome profile
and the login keychain the Claude CLI uses), wrapped in `caffeinate -is` so the Mac
doesn't idle-sleep while on power; closing the lid still sleeps it. The plist is
generated from wherever flatwatch is installed, so nothing machine-specific lives in git.
No ANTHROPIC_API_KEY is passed on purpose: Agent SDK calls should bill your Claude
subscription, not the API.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR, EXAMPLE_CONFIG, EXAMPLE_ENV, ConfigError, config_path, env_path

LABEL = "local.flatwatch"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# --------------------------------------------------------------------- init --

def init(target: Path | None = None) -> int:
    target = (target or DATA_DIR).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    for src, name, mode in ((EXAMPLE_CONFIG, "config.yaml", 0o644), (EXAMPLE_ENV, ".env", 0o600)):
        dst = target / name
        if dst.exists():
            print(f"exists, left alone: {dst}")
            continue
        shutil.copyfile(src, dst)
        dst.chmod(mode)
        print(f"created: {dst}")
    print(f"""
Next:
  1. Fill in {target / '.env'}: Telegram bot token + chat id, and your groups / subreddits.
  2. Review the criteria in {target / 'config.yaml'}.
  3. claude auth login            # LLM calls use your Claude subscription
  4. flatwatch ping               # checks config and sends a Telegram test message
  5. flatwatch login facebook reddit
  6. flatwatch agent install      # run in the background, start at login""")
    return 0


# -------------------------------------------------------------------- agent --

def _executable() -> str:
    exe = Path(sys.executable).with_name("flatwatch")      # venv / uv tool install
    if exe.exists():
        return str(exe)
    found = shutil.which("flatwatch")
    if not found:
        raise ConfigError("can't find the flatwatch executable; install it first (see README)")
    return found


def build_plist() -> dict:
    cfg, env = config_path().resolve(), env_path().resolve()
    if not cfg.exists():
        raise ConfigError(f"no config at {cfg}; run `flatwatch init` first")
    logs = DATA_DIR / "logs"
    path = os.pathsep.join(dict.fromkeys([
        str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    env_vars = {"PATH": path, "PYTHONUNBUFFERED": "1", "FLATWATCH_CONFIG": str(cfg), "FLATWATCH_ENV": str(env)}
    if os.environ.get("FLATWATCH_HOME"):
        env_vars["FLATWATCH_HOME"] = str(DATA_DIR)
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-is", _executable(), "run"],
        "WorkingDirectory": str(cfg.parent),
        "EnvironmentVariables": env_vars,
        "RunAtLoad": True,
        "KeepAlive": True,
        # don't respawn faster than once a minute; flatwatch itself also waits the minimum
        # polling interval after its last successful fetch before touching any site
        "ThrottleInterval": 60,
        "LimitLoadToSessionType": "Aqua",
        # warnings/errors only; the full log is ~/.flatwatch/logs/flatwatch.log (rotated)
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def _loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def agent(action: str) -> int:
    if sys.platform != "darwin":
        raise ConfigError("`flatwatch agent` manages a macOS LaunchAgent; on other systems run `flatwatch run` "
                          "under your own service manager")
    if action == "install":
        plist = build_plist()
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        if _loaded():
            _launchctl("bootout", f"{_domain()}/{LABEL}")
        PLIST.write_bytes(plistlib.dumps(plist))
        _launchctl("bootstrap", _domain(), str(PLIST), check=True)
        print(f"installed and started: {PLIST}\n  runs: {' '.join(plist['ProgramArguments'])}"
              f"\n  config: {plist['EnvironmentVariables']['FLATWATCH_CONFIG']}"
              f"\n  logs: {Path(plist['StandardErrorPath']).parent}")
    elif action == "uninstall":
        if _loaded():
            _launchctl("bootout", f"{_domain()}/{LABEL}")
        PLIST.unlink(missing_ok=True)
        print("stopped and removed the LaunchAgent")
    elif action == "start":
        if not PLIST.exists():
            raise ConfigError("not installed; run `flatwatch agent install`")
        if not _loaded():
            _launchctl("bootstrap", _domain(), str(PLIST), check=True)
        print("started")
    elif action == "stop":
        if _loaded():
            _launchctl("bootout", f"{_domain()}/{LABEL}")
        print("stopped (still installed; it starts again at next login, or `flatwatch agent start`)")
    elif action == "restart":
        if not _loaded():
            raise ConfigError("not running; use `flatwatch agent start`")
        _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}", check=True)
        print("restarted")
    elif action == "status":
        if not PLIST.exists():
            print("not installed")
            return 1
        out = _launchctl("print", f"{_domain()}/{LABEL}")
        if out.returncode:
            print(f"installed ({PLIST}) but not running")
            return 1
        keep = ("state =", "pid =", "last exit code", "runs =")
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip().startswith(keep)]
        print("\n".join(dict.fromkeys(lines)))
    return 0
