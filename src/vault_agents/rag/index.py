"""Менеджер индекса: инкрементальная пересборка, поиск, деградация.

Устройство (ticket-051):

- `manifest.json` — по заметке: sha256, список чанков, метаданные, когда
  проиндексирована. Инкремент считается по нему.
- `corpus.pkl` — все чанки (id, текст, метаданные) + BM25-индекс; BM25
  перестраивается из корпуса на каждый reindex (дёшево), а дорогие эмбеддинги
  считаются только для изменённых заметок.
- `chroma/` — векторное хранилище (ChromaDB, cosine).
- `lock` — single-writer: reindex (CLI, watcher, MCP) берёт эксклюзивный flock.

Деградация обязательна (аудит ticket-041): Ollama недоступна → векторная часть
откладывается (`vector_stale`), BM25-поиск продолжает работать, reindex не
падает.
"""

from __future__ import annotations

import fcntl
import functools
import json
import os
import pickle
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from .. import notes as notes_mod
from .. import schema as schema_mod
from ..config import Config
from .bm25 import BM25Index, Hit, build_from_config
from .chroma_store import VectorStore
from . import embeddings as embeddings_mod
from .hybrid import rrf_fuse

MANIFEST = "manifest.json"
CORPUS = "corpus.pkl"
LOCK = "lock"
MCP_LOCK = "mcp.lock"  # lifetime-lock MCP-процесса (ticket-006)

_mcp_lock_held = False


class MCPActive(RuntimeError):
    """Индекс занят живым MCP-процессом: внешний reindex запрещён (ticket-006).

    Chroma PersistentClient не безопасен между процессами: CLI-reindex при
    работающем MCP давал SIGSEGV на большом корпусе (6+ тыс. чанков).
    """


def acquire_mcp_lock(index_dir: Path):
    """Взять lifetime-lock индекса на время жизни MCP/watch-процесса.

    Возвращает file handle (держать до выхода) или None, если лок уже занят.
    """
    global _mcp_lock_held
    index_dir.mkdir(parents=True, exist_ok=True)
    fh = (index_dir / MCP_LOCK).open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _mcp_lock_held = True
    return fh


def release_mcp_lock(fh) -> None:
    global _mcp_lock_held
    if fh is not None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
    _mcp_lock_held = False


def check_mcp_idle(index_dir: Path) -> None:
    """MCPActive, если индекс занят другим живым MCP-процессом."""
    if _mcp_lock_held:
        return
    path = Path(index_dir) / MCP_LOCK
    if not path.is_file():
        return
    try:
        fh = path.open("r")
    except OSError:
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = fh.read().strip()
            raise MCPActive(
                f"MCP-сервер проекта активен (pid {pid or '?'}); "
                "reindex выполняет его watcher — CLI-reindex пропущен"
            ) from None
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()

# Версия раскладки индекса. 1 = "<note_id>#<i>" (коллизии на одинаковых
# stem), 2 = "<rel>#<i>" (уникален по проекту, ticket-004), 3 = + status/
# status_norm в метаданных (веса статусов, ticket-005).
FORMAT = 3


@dataclass
class NoteMeta:
    sha: str
    chunks: list[str] = field(default_factory=list)
    note_id: str = ""
    rel: str = ""
    title: str = ""
    type: str = ""
    source: str = "vault"
    status: str = ""
    status_norm: str = "unknown"
    indexed_at: float = 0.0


@dataclass
class SearchHit:
    chunk_id: str
    note_id: str
    rel: str
    title: str
    type: str
    score: float
    text: str
    source: str = "vault"
    status: str = ""
    status_norm: str = "unknown"
    matched_bm25: bool = False
    similarity: float = 0.0


@dataclass
class IndexStatus:
    notes_total: int
    notes_indexed: int
    pending_notes: int
    chunks: int
    vectors: int
    mode: str  # hybrid | bm25-only | empty
    vector_stale: bool
    last_reindex: float | None
    embedding_model: str
    index_dir: str
    sources: dict[str, int] = field(default_factory=dict)
    index_bytes: int = 0


def _load_pickle(path: Path, default):
    if not path.is_file():
        return default
    try:
        with path.open("rb") as f:
            return pickle.load(f)
    except (pickle.PickleError, EOFError, OSError):
        return default


def _dump_pickle(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)



# Мьютекс — на каталог индекса, а не на экземпляр Index (bug-022): в одном
# процессе (MCP + watcher) может жить несколько Index, и поле экземпляра их
# не сериализует. Ключ — resolved путь, чтобы относительные и абсолютные пути
# к одному индексу давали один lock.
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def process_lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _synchronized(method):
    """MCP вызывает тулсы из worker-потоков, watcher реиндексирует в своём —
    общий Index обязан сериализоваться (flock защищает только межпроцессно)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return wrapper


def _warn_if_shrunk(before: int, after: int) -> bool:
    """Запись уменьшила корпус более чем вдвое — почти наверняка затирание
    устаревшим снимком (bug-022), а не удаление заметок человеком."""
    if before >= 2 and after * 2 < before:
        print(
            f"[index] ! корпус ужался: было {before} чанков, стало {after} — "
            "похоже на запись устаревшего снимка (bug-022)",
            file=sys.stderr,
            flush=True,
        )
        return True
    return False


class Index:
    def __init__(self, cfg: Config, *, use_lock: bool = True):
        self.cfg = cfg
        self.dir = cfg.index_dir
        self._mutex = process_lock(self.dir)
        self.use_lock = use_lock
        self._manifest: dict[str, NoteMeta] = {}
        self._corpus: dict = {"ids": [], "texts": [], "meta": {}}
        self._format = 1
        self._vector_stale = False
        self._bm25: BM25Index | None = None
        self._vectors: VectorStore | None = None
        self._load()

    # ---------- служебное ----------

    def _paths(self) -> tuple[Path, Path, Path]:
        return self.dir / MANIFEST, self.dir / CORPUS, self.dir / "chroma"

    def _load(self) -> None:
        mpath, cpath, _ = self._paths()
        raw = {}
        if mpath.is_file():
            try:
                raw = json.loads(mpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        self._manifest = {
            rel: NoteMeta(**{**m, "rel": m.get("rel", rel)}) for rel, m in raw.get("notes", {}).items()
        }
        self._corpus = _load_pickle(cpath, {"ids": [], "texts": [], "meta": {}})
        try:
            self._format = int(raw.get("format", 1) or 1)
        except (TypeError, ValueError):
            self._format = 1
        self._vector_stale = bool(raw.get("vector_stale"))
        self._bm25 = None
        self._stamp = self._file_stamp()

    def _file_stamp(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """mtime_ns + размер: секундного mtime мало — две записи в одну
        секунду выглядели бы «не изменилось» (bug-022)."""
        mpath, cpath, _ = self._paths()

        def get(p: Path) -> tuple[int, int]:
            try:
                st = p.stat()
            except OSError:
                return (0, 0)
            return (st.st_mtime_ns, st.st_size)

        return get(mpath), get(cpath)

    def _ensure_loaded(self) -> None:
        """Другой процесс (CLI/watch) мог переписать индекс — перечитываем."""
        stamp = self._file_stamp()
        if stamp != self._stamp:
            self._load()

    @_synchronized
    def update_config(self, cfg: Config) -> None:
        """Подхватить новый конфиг без пересоздания Index (hot reload).

        Меняются include_dirs/exclude_tags/retrieval/ask; index_dir и
        embedding-модель считаются неизменными на время жизни процесса.
        """
        if cfg.index_dir != self.dir:
            print(
                f"[index] ! index_dir изменился ({self.dir} → {cfg.index_dir}); "
                "перезапусти процесс, чтобы сменить каталог индекса",
                file=sys.stderr,
                flush=True,
            )
        self.cfg = cfg

    def _bm25_index(self) -> BM25Index:
        if self._bm25 is None:
            self._bm25 = build_from_config(
                self._corpus.get("ids", []), self._corpus.get("texts", []), self.cfg
            )
        return self._bm25

    def _vector_store(self) -> VectorStore:
        if self._vectors is None:
            _, _, chroma_dir = self._paths()
            self._vectors = VectorStore(chroma_dir)
        return self._vectors

    def _save_manifest(self, *, vector_stale: bool, updated_at: float) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        mpath, _, _ = self._paths()
        payload = {
            "format": FORMAT,
            "updated_at": updated_at,
            "embedding_model": str(self.cfg.embedding.get("model", "")),
            "vector_stale": vector_stale,
            "notes": {rel: asdict(m) for rel, m in self._manifest.items()},
        }
        tmp = mpath.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(mpath)
        self._format = FORMAT
        self._vector_stale = vector_stale

    def _catch_up_vectors(self, verbose: bool) -> tuple[int, bool]:
        """Догнать вектора, отложенные при недоступной Ollama.

        Возвращает (сколько добавлено, удалось ли). Без изменений заметок
        incremental reindex иначе оставался бы no-op, и bm25-only после
        деградации не восстанавливался бы сам (ticket-004 P2).
        """
        store = self._vector_store()
        existing = store.ids()
        ids = self._corpus.get("ids", [])
        texts = self._corpus.get("texts", [])
        meta = self._corpus.get("meta", {})
        missing = [(cid, text) for cid, text in zip(ids, texts) if cid not in existing]
        if not missing:
            return 0, True

        def progress(done: int, total: int) -> None:
            if verbose and total >= 64:
                print(f"\r  embeddings (догон) {done}/{total}", end="", file=sys.stderr, flush=True)
                if done >= total:
                    print(file=sys.stderr, flush=True)

        try:
            embs = embeddings_mod.embed_texts(
                [text for _, text in missing], self.cfg, progress=progress
            )
            store.upsert(
                ids=[cid for cid, _ in missing],
                embeddings=embs,
                documents=[text for _, text in missing],
                metadatas=[meta.get(cid, {}) for cid, _ in missing],
            )
        except embeddings_mod.EmbeddingsUnavailable:
            return 0, False
        return len(missing), True

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if not self.use_lock:
            yield
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / LOCK).open("w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # ---------- reindex ----------

    @_synchronized
    def reindex(
        self,
        *,
        all: bool = False,
        changed: list[str] | None = None,
        verbose: bool = False,
    ) -> dict:
        """Инкрементальная пересборка. changed — пути заметок (rel, с .md или
        без); по умолчанию — все изменившиеся по sha256."""
        check_mcp_idle(self.dir)
        with self._lock():
            t0 = time.perf_counter()
            res = self._reindex_locked(all=all, changed=changed, verbose=verbose)
            res["seconds"] = round(time.perf_counter() - t0, 3)
            return res

    def _rel_from_change(self, c: str) -> str | None:
        """Путь из --changed/хука → rel индекса.

        Принимает абсолютный путь, `vault/...` и rel от vault/проекта.
        Возвращает None, если файл вне проекта.
        """
        project = self.cfg.project.resolve()
        p = Path(c)
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(project).as_posix()
            except ValueError:
                return None
        else:
            rel = Path(c).as_posix()
        if rel.startswith("vault/"):
            rel = rel[len("vault/") :]
        if not rel.endswith(".md"):
            rel += ".md"
        return rel

    def _reindex_locked(self, *, all: bool, changed: list[str] | None, verbose: bool) -> dict:
        # bug-022: перед записью перечитываем состояние с диска под lock —
        # иначе устаревший снимок этого экземпляра затирает чужой reindex.
        self._ensure_loaded()
        was_vector_stale = self._vector_stale
        if self._format < FORMAT:
            # старая раскладка chunk id (note_id#i) — пересобираем целиком
            all, changed = True, None

        schema = schema_mod.load(self.cfg.schema_path)
        include_dirs = self.cfg.include_dirs
        exclude_tags = self.cfg.exclude_tags

        current: dict[str, notes_mod.Note] = {}
        wanted = None
        if changed is not None:
            wanted = set()
            for c in changed:
                rel = self._rel_from_change(c)
                if rel is not None:
                    wanted.add(rel)

        for path in notes_mod.iter_note_paths(
            self.cfg.project,
            schema,
            include_dirs,
            skip_names=self.cfg.include_skip_names,
            skip_dirs=self.cfg.include_skip_dirs,
        ):
            rel = notes_mod.rel_for_path(path, self.cfg.project)
            if wanted is not None and rel not in wanted:
                # при точечном обновлении нужна sha-сверка только для цели
                continue
            note = notes_mod.read_note(
                path, self.cfg.project, schema, exclude_tags=exclude_tags
            )
            if not note.excluded:
                current[rel] = note

        todo: list[notes_mod.Note] = []
        meta_changed: list[notes_mod.Note] = []
        for rel, note in current.items():
            old = self._manifest.get(rel)
            if all or old is None or old.sha != note.sha256:
                todo.append(note)
            elif (
                old.title, old.type, old.source, old.status, old.status_norm
            ) != (note.title, note.type, note.source, note.status, note.status_norm):
                # контент не изменился, но метаданные (статус/тип/заголовок)
                # другие — обновим без пересчёта эмбеддингов
                meta_changed.append(note)
        if all or changed is None:
            removed = [rel for rel in self._manifest if rel not in current]
        else:
            # точечный режим: цель могла быть удалена или исключена тегом
            removed = [
                rel for rel in (wanted or []) if rel not in current and rel in self._manifest
            ]

        if not todo and not removed and not meta_changed:
            total = len(self._corpus.get("ids", []))
            vectors_updated = 0
            vector_stale = self._vector_stale
            if vector_stale:
                added, ok = self._catch_up_vectors(verbose)
                vectors_updated = added
                vector_stale = not ok
                if ok:
                    self._save_manifest(vector_stale=False, updated_at=time.time())
                    self._stamp = self._file_stamp()
            return {
                "notes_changed": 0,
                "notes_removed": 0,
                "chunks_total": total,
                "chunks": total,
                "vectors_updated": vectors_updated,
                "metadata_updated": 0,
                "vector_stale": vector_stale,
                "changed": [],
            }

        ids: list[str] = list(self._corpus.get("ids", []))
        texts: list[str] = list(self._corpus.get("texts", []))
        meta: dict = dict(self._corpus.get("meta", {}))
        chunks_before = len(ids)

        def drop_chunks(chunk_ids: list[str]) -> None:
            drop = set(chunk_ids)
            keep = [(i, t) for i, t in zip(ids, texts) if i not in drop]
            ids[:] = [i for i, _ in keep]
            texts[:] = [t for _, t in keep]
            for cid in drop:
                meta.pop(cid, None)

        # удалённые/изменённые заметки: убираем старые чанки (id собираем до
        # правки манифеста — они нужны ещё и для удаления векторов)
        stale_chunk_ids: list[str] = []
        for rel in removed:
            old = self._manifest.pop(rel, None)
            if old:
                stale_chunk_ids.extend(old.chunks)
                drop_chunks(old.chunks)
        for note in todo:
            old = self._manifest.get(note.rel)
            if old:
                stale_chunk_ids.extend(old.chunks)
                drop_chunks(old.chunks)

        # добавляем новые чанки
        for note in todo:
            for ch in note.chunks:
                ids.append(ch.id)
                texts.append(ch.text)
                meta[ch.id] = {
                    "note_id": ch.note_id,
                    "rel": ch.rel,
                    "title": ch.title,
                    "type": ch.type,
                    "source": ch.source,
                    "status": ch.status,
                    "status_norm": ch.status_norm,
                }

        _warn_if_shrunk(chunks_before, len(ids))

        self._corpus = {"ids": ids, "texts": texts, "meta": meta}
        self._bm25 = None  # перестроится лениво из нового корпуса
        self._bm25_index()

        # --- metadata refresh: статус/тип/заголовок без пересчёта эмбеддингов ---
        metadata_updated = 0
        if meta_changed:
            upd_ids: list[str] = []
            upd_metas: list[dict] = []
            for note in meta_changed:
                old = self._manifest.get(note.rel)
                if old is None:
                    continue
                for cid in old.chunks:
                    m = meta.get(cid)
                    if m is None:
                        continue
                    m.update(
                        {
                            "note_id": note.note_id,
                            "rel": note.rel,
                            "title": note.title,
                            "type": note.type,
                            "source": note.source,
                            "status": note.status,
                            "status_norm": note.status_norm,
                        }
                    )
                    upd_ids.append(cid)
                    upd_metas.append(dict(m))
            if upd_ids:
                try:
                    self._vector_store().update_metadatas(upd_ids, upd_metas)
                    metadata_updated = len(upd_ids)
                except Exception as e:  # noqa: BLE001 — corpus уже обновлён
                    print(
                        f"[index] ! metadata refresh в Chroma не удался: {e}",
                        file=sys.stderr,
                        flush=True,
                    )

        # --- вектора: удаление — всегда (эмбеддинги не нужны), добавление — по
        # мере доступности Ollama; недоступна → откладываем (деградация) ---
        new_chunks = [ch for note in todo for ch in note.chunks]
        vector_stale = False
        vectors_updated = 0
        if stale_chunk_ids or new_chunks:
            store = self._vector_store()
            store.delete(stale_chunk_ids)
            if new_chunks:
                def embed_progress(done: int, total: int) -> None:
                    if verbose and total >= 64:
                        print(f"\r  embeddings {done}/{total}", end="", file=sys.stderr, flush=True)
                        if done >= total:
                            print(file=sys.stderr, flush=True)

                try:
                    embs = embeddings_mod.embed_texts(
                        [c.text for c in new_chunks], self.cfg, progress=embed_progress
                    )
                    store.upsert(
                        ids=[c.id for c in new_chunks],
                        embeddings=embs,
                        documents=[c.text for c in new_chunks],
                        metadatas=[meta[c.id] for c in new_chunks],
                    )
                    vectors_updated = len(new_chunks)
                except embeddings_mod.EmbeddingsUnavailable:
                    vector_stale = True
                    if verbose:
                        print("  ! ollama недоступна — вектора отложены (bm25-режим)")

        # Ollama вернулась, а прошлый reindex оставил дыры — догоняем
        if not vector_stale and was_vector_stale:
            added, ok = self._catch_up_vectors(verbose)
            vectors_updated += added
            vector_stale = not ok

        # --- манифест ---
        now = time.time()
        for note in todo:
            self._manifest[note.rel] = NoteMeta(
                sha=note.sha256,
                chunks=[c.id for c in note.chunks],
                note_id=note.note_id,
                rel=note.rel,
                title=note.title,
                type=note.type,
                source=note.source,
                status=note.status,
                status_norm=note.status_norm,
                indexed_at=now,
            )
        for note in meta_changed:
            old = self._manifest.get(note.rel)
            if old is not None:
                old.title = note.title
                old.type = note.type
                old.source = note.source
                old.status = note.status
                old.status_norm = note.status_norm
                old.indexed_at = now

        _, cpath, _ = self._paths()
        self._save_manifest(vector_stale=vector_stale, updated_at=now)
        _dump_pickle(cpath, self._corpus)
        self._stamp = self._file_stamp()

        return {
            "notes_changed": len(todo),
            "notes_removed": len(removed),
            "chunks_added": len(new_chunks),
            "chunks_total": len(ids),
            "vectors_updated": vectors_updated,
            "metadata_updated": metadata_updated,
            "vector_stale": vector_stale,
            "changed": [n.rel for n in todo] + [n.rel for n in meta_changed] + removed,
        }

    # ---------- поиск ----------

    @_synchronized
    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        mode: str | None = None,
        source: str | None = None,
    ) -> list[SearchHit]:
        """Поиск по всему корпусу (vault + внешние коллекции).

        source — фильтр по источнику (vault | tickets | inbox): ранжирование
        ограничивается коллекцией и на этапе отбора кандидатов.
        """
        self._ensure_loaded()
        retr = self.cfg.retrieval
        top_k = int(top_k or retr.get("top_k", 8))
        mode = str(mode or retr.get("mode", "hybrid"))
        source = source or None
        if source:
            # алиасы: в плане встречается source=ticket (тип), у нас — имя
            # коллекции/каталога (tickets)
            source = {"ticket": "tickets", "tickets": "tickets", "inbox": "inbox", "vault": "vault"}.get(
                source, source
            )

        # С фильтром источника нужно больше кандидатов: нужная коллекция может
        # быть малой частью корпуса (inbox — 363 из 30 769 чанков).
        candidates = max(top_k * 10, 200) if source else max(top_k * 3, 20)
        rankings: dict[str, list[str]] = {}
        weights: dict[str, float] = {}
        meta = self._corpus.get("meta", {})
        source_weights = retr.get("source_weights") or {}
        status_weights = retr.get("status_weights") or {}
        bm25_ids: set[str] = set()
        similarities: dict[str, float] = {}

        def of_source(cid: str) -> bool:
            return not source or (meta.get(cid) or {}).get("source", "vault") == source

        if mode in ("hybrid", "bm25"):
            hits: list[Hit] = self._bm25_index().search(
                query, candidates, allow=of_source if source else None
            )
            rankings["bm25"] = [h.chunk_id for h in hits]
            bm25_ids = set(rankings["bm25"])
            weights["bm25"] = float((retr.get("weights") or {}).get("bm25", 0.4))

        if mode in ("hybrid", "vector"):
            try:
                qv = embeddings_mod.embed_query(query, self.cfg)
                where = {"source": source} if source else None
                vhits = self._vector_store().query(qv, candidates, where=where)
                rankings["vector"] = [h.chunk_id for h in vhits]
                similarities = {h.chunk_id: h.score for h in vhits}
                weights["vector"] = float((retr.get("weights") or {}).get("vector", 0.6))
            except embeddings_mod.EmbeddingsUnavailable:
                if mode == "vector":
                    raise

        def doc_weight(src: str, cid: str) -> float:
            m = meta.get(cid) or {}
            sw = float(source_weights.get(m.get("source", "vault"), 1.0))
            stw = float(status_weights.get(m.get("status_norm", "unknown"), 1.0))
            return sw * stw

        fused = rrf_fuse(
            rankings, weights, k=int(retr.get("rrf_k", 60)), doc_weight=doc_weight
        )
        texts = dict(zip(self._corpus.get("ids", []), self._corpus.get("texts", [])))

        out: list[SearchHit] = []
        for cid, score in fused:
            if len(out) >= top_k:
                break
            m = meta.get(cid)
            if m is None:
                # «призрак»: вектор/BM25 знают чанк, корпус — уже нет
                # (рассинхрон после ручной правки индекса) — пропускаем
                continue
            if not of_source(cid):
                continue
            out.append(
                SearchHit(
                    chunk_id=cid,
                    note_id=m.get("note_id", cid.split("#")[0]),
                    rel=m.get("rel", ""),
                    title=m.get("title", ""),
                    type=m.get("type", ""),
                    score=round(score, 6),
                    text=texts.get(cid, ""),
                    source=m.get("source", "vault"),
                    status=m.get("status", ""),
                    status_norm=m.get("status_norm", "unknown"),
                    matched_bm25=cid in bm25_ids,
                    similarity=round(float(similarities.get(cid, 0.0)), 6),
                )
            )
        return out

    # ---------- статус ----------

    @_synchronized
    def status(self) -> IndexStatus:
        self._ensure_loaded()
        schema = schema_mod.load(self.cfg.schema_path)
        total = 0
        pending = 0
        current_rels: set[str] = set()
        sources: dict[str, int] = {}
        for path in notes_mod.iter_note_paths(
            self.cfg.project,
            schema,
            self.cfg.include_dirs,
            skip_names=self.cfg.include_skip_names,
            skip_dirs=self.cfg.include_skip_dirs,
        ):
            rel = notes_mod.rel_for_path(path, self.cfg.project)
            note = notes_mod.read_note(
                path,
                self.cfg.project,
                schema,
                exclude_tags=self.cfg.exclude_tags,
                build_chunks=False,
            )
            if note.excluded:
                continue
            total += 1
            current_rels.add(rel)
            sources[note.source] = sources.get(note.source, 0) + 1
            old = self._manifest.get(rel)
            if old is None or old.sha != note.sha256:
                pending += 1

        mpath, _, _ = self._paths()
        payload = {}
        if mpath.is_file():
            try:
                payload = json.loads(mpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                payload = {}

        chunks = len(self._corpus.get("ids", []))
        try:
            vectors = self._vector_store().count() if chunks else 0
        except Exception:
            vectors = 0

        vector_stale = bool(payload.get("vector_stale")) or vectors < chunks
        if chunks == 0:
            mode = "empty"
        elif vector_stale or not embeddings_mod.ping(self.cfg):
            mode = "bm25-only"
        else:
            mode = "hybrid"

        return IndexStatus(
            notes_total=total,
            notes_indexed=sum(1 for rel in self._manifest if rel in current_rels),
            pending_notes=pending,
            chunks=chunks,
            vectors=vectors,
            mode=mode,
            vector_stale=vector_stale,
            last_reindex=payload.get("updated_at"),
            embedding_model=str(self.cfg.embedding.get("model", "")),
            index_dir=str(self.dir),
            sources=sources,
            index_bytes=_dir_bytes(self.dir),
        )


def _dir_bytes(path: Path) -> int:
    try:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except OSError:
        return 0


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def snippet(text: str, query: str, limit: int = 400) -> str:
    """Компактный фрагмент вокруг первого вхождения слова запроса."""
    if len(text) <= limit:
        return text
    import re

    low = text.lower()
    pos = -1
    for w in re.findall(r"\w+", query.lower()):
        p = low.find(w)
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos == -1:
        return text[:limit] + "…"
    start = max(0, pos - limit // 3)
    end = min(len(text), start + limit)
    # подрезаем до границ слов, чтобы сниппет не начинался с обрубка
    if start > 0:
        sp = text.find(" ", start, min(len(text), start + 40))
        if sp != -1:
            start = sp + 1
    if end < len(text):
        sp = text.rfind(" ", max(0, end - 40), end)
        if sp != -1:
            end = sp
    out = text[start:end]
    return ("…" if start > 0 else "") + out + ("…" if end < len(text) else "")
