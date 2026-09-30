"""从 .env 读取配置。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _parse_aliases(raw: str) -> dict[str, str]:
    aliases = {}
    for item in raw.split(","):
        if ":" in item:
            acct, name = item.split(":", 1)
            aliases[acct.strip()] = name.strip()
    return aliases


@dataclass
class Settings:
    token: str = os.getenv("IBKR_FLEX_TOKEN", "").strip()
    query_ids: list[str] = field(default_factory=lambda: [
        q.strip() for q in os.getenv("IBKR_FLEX_QUERY_ID", "").split(",") if q.strip()
    ])
    display_currency: str = os.getenv("DISPLAY_CURRENCY", "USD").strip().upper() or "USD"
    aliases: dict[str, str] = field(default_factory=lambda: _parse_aliases(os.getenv("ACCOUNT_ALIASES", "")))
    auto_fetch_time: str = os.getenv("AUTO_FETCH_TIME", "").strip()
    db_path: Path = ROOT / os.getenv("DB_PATH", "data/ibkr.db")
    raw_dir: Path = ROOT / "data" / "raw"
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    demo_mode: bool = os.getenv("DEMO_MODE") == "1"


settings = Settings()
