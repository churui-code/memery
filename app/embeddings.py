import argparse
from threading import Lock

from app.config import Settings


class LocalEmbeddings:
    dimensions = 512

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._lock = Lock()

    def _load(self, download: bool = False):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(
                model_name=self.settings.embedding_model,
                cache_dir=self.settings.model_cache,
                threads=2,
                providers=["CPUExecutionProvider"],
                local_files_only=not download,
            )
        return self._model

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        with self._lock:
            model = self._load()
            generator = model.query_embed(texts) if query else model.passage_embed(texts)
            vectors = [v.tolist() for v in generator]
            if any(len(v) != self.dimensions for v in vectors):
                raise ValueError("embedding_dimensions_mismatch")
            return vectors

    def download(self):
        with self._lock:
            try:
                model = self._load(download=False)
            except Exception:
                self._model = None
                model = self._load(download=True)
            vector = next(model.embed(["本地记忆初始化"]))
            if len(vector) != self.dimensions:
                raise ValueError("embedding_dimensions_mismatch")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.parse_args()
    LocalEmbeddings(Settings()).download()
    print("中文 embedding 模型已保存到本地缓存。")
