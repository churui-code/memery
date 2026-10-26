# Repository Guidelines

## Project Structure & Module Organization

This local household-memory service uses Python 3.12, FastAPI, and PostgreSQL 16 with pgvector.

- `app/main.py` defines API routes and validation; `service.py` coordinates chat and memory operations; `repository.py` owns persistence.
- `app/automatic.py`, `model.py`, and `embeddings.py` handle memory proposals, MiniMax requests, and local vectors. `worker.py` processes indexing and retention jobs.
- `app/schema.sql` and `app/migration_*.sql` define versioned database changes.
- `memory` is the executable CLI; `scripts/` contains lifecycle and verification tools.
- `tests/` contains pytest suites and shared fixtures; `docs/` contains design notes and verification reports. `README.md` describes current behavior.
- `data/` stores databases, model caches, deletion journals, and encrypted backups; exclude it from version control.

## Build, Test, and Development Commands

Run from the repository root with Docker Desktop and host Python 3; host scripts need no third-party packages.

- `python3 scripts/manage.py up`: prepare configuration, build containers, download embeddings, and start services at `127.0.0.1:8765`.
- `docker compose build api`: rebuild after code changes; source files are copied into the image.
- `python3 scripts/manage.py test`: run pytest in Docker against the initialized database.
- `python3 scripts/manage.py status`: inspect containers; use `python3 scripts/manage.py down` to stop services while retaining data.
- `docker compose logs --tail=100 api worker`: inspect service logs.
- `python3 scripts/verify.py`: run isolated storage, embedding, outage, and restore checks. Add `--real-model` for live MiniMax checks; `python3 -m scripts.verify_automatic` checks automatic memory. Run verification scripts sequentially.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Match surrounding style and keep formatting changes scoped. No formatter or linter is configured. Keep SQL parameterized, database changes versioned, and user-facing messages consistent with the existing Chinese interface.

## Testing Guidelines

Name files `test_*.py` and functions `test_<behavior>`. Reuse `tests/conftest.py` fixtures, which isolate database schemas and deletion journals and fake model/embedding calls. Add regression tests for changed behavior, particularly transactions, revision conflicts, deletion replay, and cloud exclusions. No coverage threshold is configured.

## Commit & Pull Request Guidelines

This checkout has no Git metadata, so historical conventions cannot be verified. Use concise imperative subjects, such as `Fix deletion journal replay`. PRs should describe behavior changes, link relevant issues, report validation commands/results, and explain migration or configuration changes. Update `README.md` when public behavior changes.

## Security & Configuration

Never commit `.env`, `.secrets/`, runtime data, or personal conversation content. Use synthetic verification data, preserve deletion journals, and keep `allow_cloud=false` records out of model requests.
