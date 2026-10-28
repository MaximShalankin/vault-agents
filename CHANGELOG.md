# Changelog

## 0.3.2 — 2026-10-28

Hot reload конфига для массовой миграции (ticket-007).

### Added
- `VaultWatcher` перечитывает `vault.config.yaml` по mtime без рестарта MCP:
  пересоздаёт observer (новые `include_dirs`) и запускает incremental reindex.
- `Index.update_config` подхватывает include_dirs/retrieval/ask на лету;
  MCP-тулсы читают `index.cfg`, а не снимок при старте.

### Note
- Уже запущенные процессы MCP подхватят hot reload только после перезапуска
  сессии; новые сессии — сразу.

## 0.3.1 — 2026-10-28

Безопасность внешнего reindex при живом MCP (ticket-006).

### Fixed
- SIGSEGV при `vault-agents reindex` параллельно с живым MCP-процессом:
  Chroma не безопасна между процессами. MCP держит lifetime-lock
  `vault/.index/mcp.lock`; CLI-reindex при активном MCP завершается с кодом 1
  и сообщением с pid, не открывая Chroma.
- Отдельный `vault-agents watch` тоже берёт lifetime-lock; второй watcher не
  запускается.
- Инцидент на большом корпусе: индекс пересобран после остановки MCP
  (670 заметок, 6 566 чанков, 118.6 MiB).

## 0.3.0 — 2026-10-28

Качество выдачи (ticket-005): статусы в метаданных, opt-in веса, BM25-гейт
для `ask`.

### Added
- `status`/`status_norm` в метаданных чанков (open | in_progress | closed |
  unknown); нормализация свободных legacy-статусов, включая русскую метку
  `**Статус:**`.
- Metadata refresh: изменение статуса/типа/заголовка обновляет метаданные
  корпуса и Chroma без пересчёта эмбеддингов (reindex не требует `--all`).
- `retrieval.source_weights` и `retrieval.status_weights` — opt-in множители
  документов в RRF.
- `ask_require_bm25` (по умолчанию true): `ask` не отвечает, если ни один
  hit не имеет лексического (BM25) совпадения; `ask_min_similarity` > 0
  разрешает semantic-only выше порога.
- `SearchHit`: `status`, `status_norm`, `matched_bm25`, `similarity`.

### Changed
- `FORMAT = 3`: первый reindex после обновления — полный (добавлены статусы
  в метаданные).
- Дефолтные веса источников/статусов — нейтральные: калибровка на пилотах
  показала, что глобальное понижение tickets/inbox/closed ухудшает recall
  (hit@1 тикетов падает 1.00 → 0.30). Веса включаются на проект после
  калибровки.
- CLI `ask` не печатает источники, если ответ «ничего не нашлось».

## 0.2.0 — 2026-10-28

Внешние коллекции `tickets/` и `inbox/` с полным hybrid-поиском
(ticket-001 §16, P0–P4).

### Added
- `include_dirs` — внешние коллекции без переноса файлов; rel-путь от корня
  проекта, `source`/`type` в метаданных, заголовок/статус legacy-тикетов.
- `exclude_tags` — заметки с тегом (например, `trash`) не индексируются, но
  остаются доступны через `get_note`.
- `source`-фильтр в `search` (CLI/MCP), счётчики источников в `status`.
- Watcher следит и за внешними каталогами (`tickets`, `inbox`).
- Автодогон векторов, отложенных при недоступной Ollama.
- Батчевый upsert/delete в Chroma (лимит коллекции, большие корпуса).
- `include_skip_names`/`include_skip_dirs`: `readme.md` и `archive/` по
  умолчанию вне индекса.
- Новые проекты (`vault-agents init` / внешний scaffold) получают
  `include_dirs: [tickets, inbox]`; существующие — opt-in.
- Стартовый catch-up watcher'а: правки, сделанные при закрытой сессии,
  индексируются при следующем старте MCP.
- `status`: счётчики источников и размер индекса (`--json` → `index_bytes`).

### Changed
- Chunk id: `<note_id>#<i>` → `<rel>#<i>` — уникален по всему проекту.
  Manifest `format: 2`; первый reindex после обновления — полный.
- MCP и watcher делят один `Index`; мьютекс — на каталог индекса.
- `reindex` перечитывает состояние с диска перед записью (bug-022).
- `reindex` сообщает время выполнения и количество догнанных векторов.

### Fixed
- bug-021: `trash` больше не попадает в поиск/`ask`.
- bug-022: правка заметки не затирает индекс устаревшим снимком watcher'а;
  предупреждение при просадке корпуса более чем вдвое.

## 0.1.0 — 2026-10-03

Первая рабочая версия (ticket-051, фаза C): RAG (BM25 + BGE-M3 + RRF),
MCP-сервер, temporal KG, watcher, интеграция с внешним гейтом записи.
