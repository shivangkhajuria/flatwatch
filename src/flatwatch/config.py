"""Config loading and validation.

Secrets come from `.env` / the process environment and are only read here.
Never print or log `Secrets.telegram_bot_token`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("FLATWATCH_HOME", Path.home() / ".flatwatch")).expanduser()
# dev files (fixtures/, evals/) go in the source checkout when running from one, else ~/.flatwatch
WORK_DIR = REPO_ROOT if (REPO_ROOT / "pyproject.toml").exists() else DATA_DIR
TEMPLATES = Path(__file__).resolve().parent / "templates"
EXAMPLE_CONFIG = TEMPLATES / "config.example.yaml"
EXAMPLE_ENV = TEMPLATES / "env.example"

# Personal sources can live in .env instead of config.yaml (comma- or newline-separated).
SOURCE_ENV = {"facebook": "FLATWATCH_FACEBOOK_GROUPS", "reddit": "FLATWATCH_SUBREDDITS"}


def _find(env_var: str, name: str) -> Path:
    """$ENV_VAR, else ./name, else ~/.flatwatch/name (also the default when nothing exists yet)."""
    if v := os.environ.get(env_var):
        return Path(v).expanduser()
    for d in (Path.cwd(), DATA_DIR):
        if (d / name).exists():
            return d / name
    return DATA_DIR / name


def config_path() -> Path:
    return _find("FLATWATCH_CONFIG", "config.yaml")


def env_path() -> Path:
    return _find("FLATWATCH_ENV", ".env")


def _env_values(env_file: Path | None) -> dict[str, str]:
    """.env values overlaid with the real environment (which wins; useful for launchd / CI)."""
    values = {k: v for k, v in dotenv_values(env_file).items() if v} if env_file and env_file.exists() else {}
    values.update({k: v for k, v in os.environ.items() if v})
    return values

Unit = Literal["1rk", "studio", "1bhk", "2bhk", "3bhk", "4bhk_plus", "unknown"]
OfferingType = Literal["full_flat", "flatmate", "replacement", "pg_coliving", "other"]
Gender = Literal["male", "female", "any", "unknown"]


class ConfigError(RuntimeError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _as_list(v):
    return [v] if isinstance(v, str) else v


class P1Tier(_Strict):
    offering_type: list[OfferingType]
    unit: list[Unit]
    max_rent_inr: int = Field(gt=0)

    _norm = field_validator("offering_type", "unit", mode="before")(_as_list)


class P2Tier(_Strict):
    offering_type: list[OfferingType]
    unit: list[Unit]
    gender_pref: list[Gender]
    private_bathroom: bool
    max_rent_inr: int = Field(gt=0)

    _norm = field_validator("offering_type", "unit", "gender_pref", mode="before")(_as_list)


class Tiers(_Strict):
    P1: P1Tier
    P2: P2Tier


class Exclude(_Strict):
    intent: list[Literal["offering", "seeking", "other"]] = []
    offering_type: list[OfferingType] = []
    unit: list[Unit] = []
    gender_pref: list[Gender] = []


class Criteria(_Strict):
    localities: dict[str, list[str]]
    nearby_ok: list[str] = []
    tiers: Tiers
    budget_note: str = ""
    over_budget_digest_pct: int = Field(15, ge=0, le=100)
    rent_unknown: Literal["digest", "drop"] = "digest"
    bonus: dict[str, int] = {}
    exclude: Exclude = Exclude()
    flag_brokers: bool = True

    @field_validator("localities")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("at least one locality is required")
        return v


def normalize_fb_url(url: str) -> str:
    """Canonical group URL: https, www.facebook.com, no query/fragment, trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host in {"facebook.com", "m.facebook.com", "web.facebook.com"}:
        host = "www.facebook.com"
    path = parts.path.rstrip("/") + "/"
    return urlunsplit(("https", host, path, "", ""))


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


class Sources(_Strict):
    facebook: list[str] = []
    reddit: list[str] = []

    @field_validator("facebook")
    @classmethod
    def _fb(cls, v):
        return _dedupe([normalize_fb_url(u) for u in v])

    @field_validator("reddit")
    @classmethod
    def _reddit(cls, v):
        cleaned = []
        for s in v:
            s = s.strip().strip("/")
            for prefix in ("https://www.reddit.com/r/", "r/"):
                if s.lower().startswith(prefix.lower()):
                    s = s[len(prefix):]
            cleaned.append(s.split("/")[0])
        return _dedupe(cleaned)


class ExtractConfig(_Strict):
    backend: Literal["agent_sdk", "anthropic_api"] = "agent_sdk"
    model: str = "claude-haiku-5-5"
    batch_size: int = Field(15, ge=1, le=50)


class AlertsConfig(_Strict):
    max_age_hours: int = Field(24, ge=1)          # older posts never trigger an instant alert
    digest_times: list[str] = ["09:00", "21:00"]  # IST, HH:MM
    digest_max_age_days: int = Field(3, ge=1)

    @field_validator("digest_times")
    @classmethod
    def _hhmm(cls, v):
        for t in v:
            h, m = t.split(":")
            if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
                raise ValueError(f"bad time {t!r}")
        return v


class AppConfig(_Strict):
    criteria: Criteria
    sources: Sources
    extract: ExtractConfig = ExtractConfig()
    alerts: AlertsConfig = AlertsConfig()


class Secrets(BaseModel):
    telegram_bot_token: SecretStr
    telegram_chat_id: int
    anthropic_api_key: SecretStr | None = None


REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def load_secrets(env_file: Path | None = None) -> Secrets:
    env_file = env_file or env_path()
    values = _env_values(env_file)
    missing = [k for k in REQUIRED_ENV if not values.get(k)]
    if missing:
        raise ConfigError(f"missing required env var(s): {', '.join(missing)} (set them in {env_file})")
    try:
        chat_id = int(values["TELEGRAM_CHAT_ID"])  # type: ignore[arg-type]
    except ValueError:
        raise ConfigError("TELEGRAM_CHAT_ID must be an integer") from None
    return Secrets(
        telegram_bot_token=values["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=chat_id,
        anthropic_api_key=values.get("ANTHROPIC_API_KEY") or None,
    )


def load_config(path: Path | None = None, env_file: Path | None = None) -> AppConfig:
    """Load config.yaml. Sources set in the environment (FLATWATCH_FACEBOOK_GROUPS /
    FLATWATCH_SUBREDDITS, in .env or the real env) replace the ones in the file. The
    discovered .env is only read when no explicit path is given, so tests stay hermetic."""
    if path is None:
        path, env_file = config_path(), env_file or env_path()
    if not path.exists():
        raise ConfigError(f"config file not found: {path} (run `flatwatch init`)")
    raw = yaml.safe_load(path.read_text()) or {}
    env = _env_values(env_file)
    for source, var in SOURCE_ENV.items():
        if env.get(var):
            items = [x.strip() for x in env[var].replace("\n", ",").split(",") if x.strip()]
            raw.setdefault("sources", {})
            raw["sources"] = {**(raw["sources"] or {}), source: items}
    try:
        return AppConfig.model_validate(raw)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(f"invalid config {path}:\n{e}") from e


def auth_source(cfg: AppConfig) -> str:
    """Describe which billing path LLM calls will use. Safe to log."""
    if cfg.extract.backend == "agent_sdk":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "WARNING: ANTHROPIC_API_KEY is set; Agent SDK calls would bill the API, not the subscription"
        return "agent_sdk via Claude Code subscription login"
    return "anthropic_api via ANTHROPIC_API_KEY"
