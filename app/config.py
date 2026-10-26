from dataclasses import dataclass, field
import os
from pathlib import Path


def secret(name: str, default: str = "") -> str:
    filename = os.getenv(name + "_FILE")
    if filename:
        return Path(filename).read_text().strip()
    return os.getenv(name, default)


@dataclass
class Settings:
    db_host: str = field(default_factory=lambda: os.getenv("DB_HOST", "db"))
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "memory"))
    db_user: str = field(default_factory=lambda: os.getenv("DB_USER", "memory_app"))
    db_password: str = field(default_factory=lambda: secret("DB_PASSWORD"), repr=False)
    db_schema: str = "memory"
    api_token: str = field(default_factory=lambda: secret("API_TOKEN"), repr=False)
    model_key: str = field(default_factory=lambda: secret("MINIMAX_API_KEY"), repr=False)
    model_base_url: str = field(default_factory=lambda: os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic"))
    model_name: str = field(default_factory=lambda: os.getenv("MINIMAX_MODEL", "MiniMax-M3"))
    thinking: str = field(default_factory=lambda: os.getenv("MINIMAX_THINKING", "disabled"))
    model_timeout: float = field(default_factory=lambda: float(os.getenv("MODEL_TIMEOUT_SECONDS", "90")))
    embedding_model: str = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
    model_cache: str = field(default_factory=lambda: os.getenv("MODEL_CACHE", "/data/models"))
    deletion_journal: str = field(default_factory=lambda: os.getenv("DELETION_JOURNAL", "/data/journal/deletions.jsonl"))
    history_days: int = field(default_factory=lambda: int(os.getenv("HISTORY_DAYS", "30")))
    min_similarity: float = 0.55

    def __post_init__(self):
        if not 1 <= self.history_days <= 36500:
            raise ValueError('HISTORY_DAYS 必须为 1 到 36500 天。')
        if not 1 <= self.model_timeout <= 600:
            raise ValueError('MODEL_TIMEOUT_SECONDS 必须为 1 到 600 秒。')
        if self.model_name != 'MiniMax-M3':
            raise ValueError('v0.1 固定使用 MiniMax-M3。')
