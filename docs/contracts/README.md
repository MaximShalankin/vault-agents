# Contracts — контракты артефактов координации

Владелец контрактов — vault-agents. Потребители: внешняя канбан-доска,
индекс vault-agents (`include_dirs: [tickets, inbox]`), тонкие внешние хуки
записи (перехват записи → вызов `vault-agents check`).

Спецификации лежат в пакете и являются единственными источниками правил:

| Спецификация | Контракт | Версия |
|---|---|---|
| `src/vault_agents/contracts/inbox-proto-1.json` | межпроектные сообщения `inbox/` | 1.1.0 |
| `src/vault_agents/contracts/tickets-v1.json` | тикеты `tickets/ticket-*.md` | 1.1.0 |

Валидатор — `src/vault_agents/contracts/validator.py`: читает спеки, не
содержит собственных словарей. Использование: `vault-agents check <файлы>`
(CLI) или `vault_agents.contracts.validate_file(path, new_file=...)`.
Режимы: `--new` — строгие гейты (создание тикета), без флага — мягкий
legacy (правка существующего).

## Статусы тикетов

Enum: `OPEN`, `IN_PROGRESS`, `DONE`, `DEFERRED`, `CANCELLED`.

Legacy-синонимы — часть контракта; `vault-agents spec tickets` отдаёт их в
`legacy.status_synonyms`:

| Канон | Синонимы |
|---|---|
| `OPEN` | open, открыт, todo, planned, draft, concept, запланировано, готов к реализации, спроектирован |
| `IN_PROGRESS` | in_progress, in progress, in-progress, wip, active, review, в работе |
| `DONE` | done, closed, resolved, completed, answered, завершён, выполнен, выполнено, подтверждено, решено, опубликован, опубликовано, готов |
| `DEFERRED` | deferred, отложен, parked |
| `CANCELLED` | cancelled, отменён, superseded |

Парсинг: берётся первый токен после `**Status:**`; `**Status:** open — ждёт
ревью` нормализуется в `OPEN`, остаток — допустимый комментарий. При
создании (`--new`) допустим только enum-токен; при правке неизвестное
значение — warning, не deny.

## Решения

1. Статус тикета — жирная строка `**Status:**` в шапке (не фронтматтер).
2. Малый enum + мягкий legacy: гейт строг только к новым файлам, синонимы
   маппятся словарём на чтении, отсутствие статуса валидно.
3. Имена `ticket-NNN.md` и `inbox/` стабильны; отдельная vault-native
   раскладка — вне текущего контракта.
4. Вся логика проверок — в vault-agents; внешний хук записи — тонкий вызов
   `vault-agents check`; при недоступности CLI хук может пропустить запись с
   предупреждением, не блокируя проекты.

## Правила изменений

- Любая правка семантики полей/словарей — bump `spec_version` (semver:
  мажор — ломающая, минор — новое необязательное поле/синоним) и запись в
  CHANGELOG.md. Спеки меняются вместе с тестами валидатора.
