"""Авто-реиндекс: watchdog + debounce.

Два канала (ticket-051): watcher в процессе MCP и CLI `reindex --changed`
(его дёргает PostToolUse-хук). Оба идут через один Index с single-writer
lock — параллельные переиндексации не пересекаются.

С ticket-004 watcher следит и за внешними коллекциями (`include_dirs`:
tickets, inbox). Экземпляр Index передаётся из MCP-сервера (bug-022): в
процессе не должно быть двух независимых Index, пишущих в один каталог.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import notes as notes_mod
from . import schema as schema_mod
from .config import CONFIG_REL, Config, load_config
from .rag.index import Index, acquire_mcp_lock, release_mcp_lock

POLL = 0.5


class _Collector(FileSystemEventHandler):
    def __init__(
        self,
        project: Path,
        pending: dict[str, float],
        lock: threading.Lock,
        accept,
    ):
        self.project = project.resolve()
        self.pending = pending
        self.lock = lock
        self.accept = accept

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        for src in (event.src_path, getattr(event, "dest_path", "")):
            if not src:
                continue
            p = Path(src)
            if p.suffix != ".md":
                continue
            try:
                rel = notes_mod.rel_for_path(p.resolve(), self.project)
            except (ValueError, OSError):
                continue
            if not self.accept(rel):
                continue
            with self.lock:
                self.pending[rel] = time.time()


class VaultWatcher:
    """Дебаунс-воркер: копит изменения и переиндексирует пачкой."""

    def __init__(self, cfg: Config, index: Index | None = None):
        self.cfg = cfg
        self.vault = cfg.project / "vault"
        self.debounce = float(cfg.watcher.get("debounce_sec", 2))
        self.pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer: Observer | None = None
        self._worker: threading.Thread | None = None
        self._index = index
        self.schema = schema_mod.load(cfg.schema_path) if cfg.schema_path.is_file() else None
        self._cfg_path = cfg.project / CONFIG_REL
        self._cfg_mtime = self._mtime()

    def _mtime(self) -> int:
        try:
            return self._cfg_path.stat().st_mtime_ns
        except OSError:
            return 0

    def _reload_config(self) -> bool:
        """Подхватить изменения vault.config.yaml без рестарта MCP.

        Массовая миграция (ticket-007) включает include_dirs в конфигах
        работающих сессий; перезапускать каждую сессию не нужно. При
        изменении конфига пересоздаём observer (могли появиться/исчезнуть
        внешние каталоги) и просим Index перечитать cfg; последующий
        reindex в _loop индексирует новые коллекции.
        """
        mtime = self._mtime()
        if mtime == self._cfg_mtime:
            return False
        self._cfg_mtime = mtime
        try:
            new_cfg = load_config(self.cfg.project)
        except Exception as e:  # noqa: BLE001 — watcher не должен умирать
            print(f"[watch] config reload failed: {e}", file=sys.stderr, flush=True)
            return False
        self.cfg = new_cfg
        self.vault = new_cfg.project / "vault"
        self.debounce = float(new_cfg.watcher.get("debounce_sec", 2))
        self.schema = schema_mod.load(new_cfg.schema_path) if new_cfg.schema_path.is_file() else None
        if self._index is not None:
            self._index.update_config(new_cfg)
        self._restart_observer()
        print(
            f"[watch] config reloaded: include_dirs={new_cfg.include_dirs}",
            file=sys.stderr,
            flush=True,
        )
        return True

    # --- фильтр: только заметки, только не skip_dirs/readme/archive ---
    def _accept(self, rel: str) -> bool:
        parts = Path(rel).parts
        if self.schema and parts and parts[0] in self.schema.skip_dirs:
            return False
        if parts and parts[-1].lower() in self.cfg.include_skip_names:
            return False
        skip = {d.lower() for d in self.cfg.include_skip_dirs}
        if self.schema:
            skip |= {d.lower() for d in self.schema.skip_dirs}
        return not any(p.lower() in skip for p in parts[:-1])

    def _drain(self, now: float) -> list[str]:
        with self._lock:
            ready = [rel for rel, ts in self.pending.items() if now - ts >= self.debounce]
            for rel in ready:
                self.pending.pop(rel, None)
        return [rel for rel in ready if self._accept(rel)]

    def _roots(self) -> list[Path]:
        roots = []
        if self.vault.is_dir():
            roots.append(self.vault)
        project = self.cfg.project.resolve()
        for inc in self.cfg.include_dirs:
            base = (project / inc).resolve()
            if base.is_dir() and base.is_relative_to(project) and base not in roots:
                roots.append(base)
        return roots

    def _catch_up(self, idx: Index) -> None:
        """Догнать изменения, сделанные без запущенного watcher'а.

        MCP-сервер стартует с индексом с диска; если правки в tickets/inbox
        шли при закрытой сессии, без этого шага поиск до первого события
        оставался бы устаревшим. Инкрементально, в фоне watcher-потока.
        """
        try:
            res = idx.reindex()
        except Exception as e:  # noqa: BLE001 — старт не должен падать
            print(f"[watch] стартовый reindex failed: {e}", file=sys.stderr, flush=True)
            return
        if res.get("notes_changed") or res.get("notes_removed") or res.get("vectors_updated"):
            print(
                f"[watch] стартовый reindex: заметок {res.get('notes_changed', 0)}"
                f" (+{res.get('notes_removed', 0)} удалено),"
                f" векторов {res.get('vectors_updated', 0)},"
                f" чанков {res.get('chunks_total', 0)}",
                file=sys.stderr,
                flush=True,
            )

    def _loop(self) -> None:
        idx = self._index or Index(self.cfg)
        self._index = idx
        self._catch_up(idx)
        while not self._stop.is_set():
            time.sleep(POLL)
            if self._reload_config():
                try:
                    res = idx.reindex()
                    print(
                        f"[watch] config reload reindex: заметок {res.get('notes_changed', 0)},"
                        f" чанков {res.get('chunks_total', 0)}",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[watch] config reload reindex failed: {e}", file=sys.stderr, flush=True)
            if not self.pending:
                continue
            batch = self._drain(time.time())
            if not batch:
                continue
            try:
                res = idx.reindex(changed=batch)
                if res.get("notes_changed") or res.get("notes_removed"):
                    print(
                        f"[watch] reindex: {res['notes_changed']} заметок "
                        f"(+{res.get('notes_removed', 0)} удалено), чанков {res.get('chunks_total', 0)}",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception as e:  # noqa: BLE001 — watcher не должен умирать
                print(f"[watch] reindex failed: {e}", file=sys.stderr, flush=True)

    def _start_observer(self) -> Observer:
        handler = _Collector(self.cfg.project, self.pending, self._lock, self._accept)
        obs = Observer()
        for root in self._roots():
            obs.schedule(handler, str(root), recursive=True)
        obs.start()
        return obs

    def _restart_observer(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._observer = self._start_observer()

    def start(self) -> None:
        self._observer = self._start_observer()
        self._worker = threading.Thread(target=self._loop, name="vault-reindex", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._worker:
            self._worker.join(timeout=5)


def run_watcher(project: str | None = None) -> None:
    """Отдельный процесс (когда MCP не запущен)."""
    cfg = load_config(project)
    if not cfg.watcher.get("enabled", True):
        print("watcher выключен в конфиге (watcher.enabled)", file=sys.stderr)
        return
    lock = acquire_mcp_lock(cfg.index_dir)
    if lock is None:
        print("MCP уже активен — отдельный watcher не запускается", file=sys.stderr)
        return
    w = VaultWatcher(cfg)
    print(f"[watch] {cfg.project} (debounce {w.debounce}s) — Ctrl+C для выхода", file=sys.stderr, flush=True)
    w.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        w.stop()
        release_mcp_lock(lock)
