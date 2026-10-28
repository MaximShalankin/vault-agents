# vault-agents

Переиспользуемое ядро памяти для агентских проектов: **RAG** (BM25 + эмбеддинги
+ RRF), **MCP-сервер** для Claude Code и **temporal Knowledge Graph**. Один
пакет на машину — вместо копипасты RAG-стека по каждому проекту

Проект живёт вокруг vault: заметки в markdown с фронтматтером по
`vault/00_System/vault_schema.json`. vault-agents ничего не пишет в заметки —
он их индексирует, ищет и отвечает. Контракт заметок и гейт записи —
отдельный инструмент; здесь находится его Python-зеркало для валидации и
lint'а (`schema.py`).

Рабочие протоколы проекта — `tickets/` и `inbox/` — индексируются как
**внешние коллекции** (`include_dirs`) полным гибридом (BM25 + вектора +
RRF), без переноса файлов. Архив и `readme.md` по умолчанию исключены.

## Требования

- Python 3.12 и [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/): `bge-m3` — вектора, любая chat-модель —
  ответы `ask` (по умолчанию `gemma4:e4b`):
  `ollama pull bge-m3 && ollama pull gemma4:e4b`
- Без Ollama ядро работает в режиме `bm25-only` — деградация заложена в дизайн

## Установка

```bash
git clone https://github.com/MaximShalankin/vault-agents
cd vault-agents
uv sync               # .venv с Python 3.12
./install-shim.sh     # шим ~/bin/vault-agents (опционально)
```

Шим — однострочник на `.venv/bin/vault-agents`; нужен `~/bin` в `$PATH`
(иначе вызывай `.venv/bin/vault-agents` или `uv run vault-agents`). MCP
подключается per-project через `.mcp.json`, который создаёт `vault-agents init`
в целевом проекте.

## Быстрый старт

```bash
vault-agents init --project ~/myproject    # скелет vault/ + конфиг + .mcp.json
vault-agents doctor --project ~/myproject  # проверить Ollama и модели
vault-agents reindex --project ~/myproject
vault-agents search "тема" --project ~/myproject
vault-agents ask "что известно про X?" --project ~/myproject
```

После `init` и перезапуска сессии Claude Code в проекте появляются MCP-тулсы
`search` / `ask` / `get_note` / `status` / `reindex` и `kg_*`.

## Команды

```bash
vault-agents init [--project DIR]        # досоздать скелет/конфиг/.mcp.json (идемпотентно)
vault-agents status [--project DIR]      # свежесть индекса, режим, число заметок/чанков
vault-agents doctor [--project DIR]      # проверка окружения: Ollama, модели, индексы
vault-agents reindex [--project DIR] [--all] [--changed FILE]
vault-agents search "запрос" [--top-k N] [--mode hybrid|bm25|vector] [--source vault|tickets|inbox]
vault-agents ask "вопрос"                # ответ с inline-цитатами [N]
vault-agents lint [--project DIR]        # расширенная проверка заметок (сверх контракта схемы)
vault-agents mcp                         # MCP-сервер (stdio) для Claude Code
vault-agents watch                       # авто-реиндекс (если MCP не запущен)
```

`--project` не обязателен: корень ищется по `vault/00_System/vault_schema.json`
вверх от cwd (или `$VAULT_PROJECT`).

## Как это работает

- **Индексация.** Заметки (кроме `skip_dirs` схемы) и внешние коллекции
  режутся на чанки (`chunk_size`/`chunk_overlap`); sha256 на заметку —
  повторный `reindex` переиндексирует только изменённое (no-op — секунды).
  Chunk id — `<rel>#<i>`, уникален по всему проекту (одинаковые stem в разных
  папках не конфликтуют). Изменение метаданных (статус/тип/заголовок)
  обновляется без пересчёта эмбеддингов.
- **Внешние коллекции.** `tickets/` и `inbox/` подключаются конфигом
  `include_dirs`; rel-путь — от корня проекта, `source` (`vault` | `tickets` |
  `inbox`), тип — из расположения, заголовок — из первого `# …`, статус — из
  `**Status:**` (legacy-тикеты без фронтматтера). Архив и `readme.md` не
  индексируются (`include_skip_names` / `include_skip_dirs`).
- **Исключение по тегам.** `exclude_tags: [trash]` — заметка не попадает в
  индекс, но остаётся доступной по прямому id (`get_note`). По умолчанию
  список пуст — поведение подключаемых проектов не меняется.
- **Поиск.** Гибрид: BM25 (`rank-bm25` + русский стеммер nltk; бэкенд
  переключается на FTS5 конфигом) + вектора (BGE-M3 через Ollama) + слияние
  RRF (веса и `rrf_k` — в конфиге). Фильтр `source` — по коллекции. Режим по
  умолчанию — `snippets` (компактные фрагменты: критично для сессий с
  ограниченным контекстом).
- **`ask`.** Top-N сниппетов → Ollama → ответ с inline-цитатами `[N]` и
  списком источников. LLM недоступна → честный фолбэк в сниппеты. Если ни
  один фрагмент не имеет BM25-совпадения, `ask` честно отвечает «не
  нашлось», а не строит ответ по векторным соседям (`ask_require_bm25`;
  semantic-only — через `ask_min_similarity`).
- **Деградация обязательна.** Ollama — единая точка отказа: недоступна →
  `status` показывает `bm25-only`, поиск продолжает работать, `ask` отдаёт
  сниппеты, reindex не падает. Когда Ollama возвращается, `reindex` сам
  догоняет недостающие векторы (без `--all`).
- **Авто-реиндекс.** Watcher (watchdog, debounce 2s) в MCP-процессе следит и
  за vault, и за `include_dirs`; при старте делает инкрементальный catch-up
  (правки, сделанные без запущенного watcher'а, не теряются). CLI
  `reindex --changed <file>` — точечная переиндексация для PostToolUse-хука.
  Индекс — под single-writer lock; MCP и watcher делят один `Index`. Пока MCP
  жив, CLI `reindex` отказывается работать (exit 1): Chroma не безопасна
  между процессами; изменения подхватывает watcher MCP. Полный reindex —
  после остановки MCP.
- **KG** (`vault/.kg/graph.sqlite3`): temporal entities/triples
  (`valid_from`/`valid_to`) для структурированных фактов — «md для текстов,
  sqlite для фактов».

## Конфиг

`vault/00_System/vault.config.yaml` в проекте; дефолты — в
`src/vault_agents/config.py`. Ключевое: `include_dirs` (внешние коллекции),
`exclude_tags` (например, `[trash]`), `include_skip_names`/`include_skip_dirs`
(по умолчанию `readme.md` / `archive`), `embedding.model` (bge-m3:latest),
`bm25.backend`, `retrieval.mode/weights`, `ask.model`, `watcher.*`.

```yaml
include_dirs: [tickets, inbox]
exclude_tags: [trash]
```

Качество выдачи настраивается опционально:

```yaml
retrieval:
  source_weights: {vault: 1.0, tickets: 0.8}  # по умолчанию пусто = 1.0
  status_weights: {closed: 0.5}               # по умолчанию пусто = 1.0
  ask_require_bm25: true                      # ask не отвечает по векторным соседям
  ask_min_similarity: 0.0                     # >0 разрешает semantic-only
```

Глобальные веса по умолчанию нейтральны: калибровка на трёх пилотах показала,
что понижение tickets/inbox или закрытых тикетов ухудшает recall (hit@1
тикетов 1.00 → 0.30). Включать веса — только на проект, после калибровки.

Новый проект (`vault-agents init`) получает `include_dirs: [tickets, inbox]`
по умолчанию. Существующие проекты без ключа остаются opt-in
(`include_dirs: []`), пока не включат явно.

Индексы — `vault/.index/` (в `.gitignore` проекта), регенерируемы.

## Раскладка

```
src/vault_agents/
  config.py        # конфиг + поиск корня проекта
  frontmatter.py   # минимальный YAML-поднабор
  schema.py        # vault_schema.json + валидация заметки
  notes.py         # заметки vault + внешние коллекции, теги, чанки, sha256
  rag/             # embeddings, bm25, chroma, hybrid(RRF), index
  kg/              # temporal sqlite
  mcp/server.py    # FastMCP: search/ask/get_note/status/reindex + kg_*
  cli.py           # команды
```

## Разработка

```bash
uv sync
./scripts/check.sh    # pytest + compileall
```

## Статус

0.3.2: RAG (hybrid), внешние коллекции `tickets`/`inbox`, temporal KG,
watcher с hot reload конфигурации, MCP-сервер. История изменений —
[CHANGELOG.md](CHANGELOG.md).

## Лицензия

MIT — см. [LICENSE](LICENSE).
