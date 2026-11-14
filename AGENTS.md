# sports-app-core package governance

- This package is a待应用增量 for `/Users/ycy/ycy/sports-app`; it must not read or write `/Users/ycy/ycy/foot`.
- Runtime provider is Codex only; imports do not start network, scheduler, worker, or publication work.
- `tests/independent/` belongs to the quality auditor and is never modified by architecture.
- `model_probability` and `EV` remain null until separately validated.
- The current Flask factory returns 501 for task/publication operations not yet implemented; callers must not treat that as success.
- No credentials, real ledgers, logs, caches, or databases are packaged.
