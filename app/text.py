import logging
import re
from functools import lru_cache

import jieba

jieba.setLogLevel(logging.WARNING)
STOP = set("我 你 他 她 它 的 了 呢 吗 是 在 有 和 与 及 请 记住 记得 什么 怎么 哪个 哪些 多少 告诉 知道 一下 现在".split())


@lru_cache(maxsize=2048)
def tokens(text: str) -> tuple[str, ...]:
    words = re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", text.lower())
    # Search mode expands compounds such as “喝咖啡” to include “咖啡”.
    parts = [t for word in words for t in jieba.cut_for_search(word) if t.strip() and t not in STOP]
    return tuple(dict.fromkeys(parts))[:256]


def terms(text: str) -> str:
    return " ".join(tokens(text))


def ts_query(text: str) -> str:
    return " | ".join("'" + t.replace("'", "''") + "'" for t in tokens(text))


def chunks(text: str, size: int = 300, overlap: int = 50) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size - overlap)] or [""]
