"""bug-022: один Index на процесс, reload перед записью, защита от затирания."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from vault_agents import config as config_mod
from vault_agents.rag.chroma_store import VectorStore
from vault_agents.rag.index import (
    Index,
    MCPActive,
    _warn_if_shrunk,
    acquire_mcp_lock,
    process_lock,
    release_mcp_lock,
)
from vault_agents.watch import VaultWatcher

from .conftest import note_text


def _index(project: Path) -> Index:
    return Index(config_mod.load_config(project))


def test_stale_instance_reloads_before_write(project: Path, fake_embeddings):
    stale = _index(project)  # снимок пустого индекса — как watcher в bug-022
    fresh = _index(project)
    fresh.reindex()

    beta = project / "vault/10_Notes/20261003-note-beta.md"
    beta.write_text(
        note_text("20261003-note-beta", "Бета", "Бета теперь про квантование."),
        encoding="utf-8",
    )
    res = stale.reindex(changed=["10_Notes/20261003-note-beta.md"])

    assert res["notes_changed"] == 1
    st = stale.status()
    assert st.notes_total == 2
    assert st.notes_indexed == 2
    # главное: альфа не потеряна записью устаревшего снимка
    assert stale.search("альфа счётчики")
    assert stale.search("квантование")


def test_mcp_lifetime_lock_blocks_external_reindex(project: Path, fake_embeddings):
    cfg = config_mod.load_config(project)
    idx = Index(cfg)
    idx.reindex()

    own = acquire_mcp_lock(cfg.index_dir)
    assert own is not None
    # в MCP-процессе reindex разрешён (lock свой)
    assert idx.reindex()["notes_changed"] == 0
    release_mcp_lock(own)

    code = (
        "import fcntl, sys, time;"
        "f=open(sys.argv[1],'a+');"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX);"
        "print('locked', flush=True);"
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(cfg.index_dir / "mcp.lock")],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    try:
        with pytest.raises(MCPActive):
            idx.reindex()
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # лок снят — внешний reindex снова работает
    assert idx.reindex()["notes_changed"] == 0


def test_watcher_hot_reloads_include_dirs(project: Path, fake_embeddings):
    cfg_path = project / config_mod.CONFIG_REL
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump({"include_dirs": []}), encoding="utf-8")
    cfg = config_mod.load_config(project)
    idx = Index(cfg)
    idx.reindex()

    (project / "tickets").mkdir(parents=True, exist_ok=True)
    (project / "tickets/ticket-001.md").write_text(
        "# Ticket-001: квантование\n\n**Status:** OPEN\n\nтело про квантование\n",
        encoding="utf-8",
    )

    watcher = VaultWatcher(cfg, index=idx)
    watcher._restart_observer = lambda: None  # observer в тесте не нужен
    cfg_path.write_text(yaml.safe_dump({"include_dirs": ["tickets"]}), encoding="utf-8")

    assert watcher._reload_config() is True
    assert watcher.cfg.include_dirs == ["tickets"]
    assert idx.cfg.include_dirs == ["tickets"]

    idx.reindex()
    hits = idx.search("квантование", source="tickets")
    assert hits and hits[0].rel == "tickets/ticket-001.md"


def test_shared_process_lock_per_index_dir(project: Path):
    a = process_lock(project / "vault/.index")
    b = process_lock(project / "vault/./.index")
    assert a is b
    assert process_lock(project / "other/.index") is not a


def test_shrink_warning(capsys):
    assert _warn_if_shrunk(10, 4) is True
    out = capsys.readouterr()
    assert "корпус ужался" in out.err
    assert _warn_if_shrunk(10, 6) is False
    assert _warn_if_shrunk(0, 0) is False


def test_watcher_uses_shared_index_and_skips_service_files(project: Path):
    (project / "tickets/archive").mkdir(parents=True, exist_ok=True)
    (project / "inbox/archive").mkdir(parents=True, exist_ok=True)
    cfg_path = project / config_mod.CONFIG_REL
    cfg_path.write_text(
        yaml.safe_dump({"include_dirs": ["tickets", "inbox"]}, allow_unicode=True),
        encoding="utf-8",
    )
    cfg = config_mod.load_config(project)
    idx = Index(cfg)
    watcher = VaultWatcher(cfg, index=idx)

    assert watcher._index is idx
    assert watcher._accept("10_Notes/20261003-note-alpha.md")
    assert watcher._accept("tickets/ticket-001.md")
    assert not watcher._accept("00_System/STATE.md")
    assert not watcher._accept("tickets/readme.md")
    assert not watcher._accept("tickets/archive/old.md")
    assert not watcher._accept("inbox/archive/old.md")


def test_watcher_catch_up_on_start(project: Path, capsys):
    cfg_path = project / config_mod.CONFIG_REL
    cfg_path.write_text("watcher:\n  enabled: true\n", encoding="utf-8")
    cfg = config_mod.load_config(project)

    class _FakeIndex:
        def __init__(self):
            self.calls = []

        def reindex(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "notes_changed": 2,
                "notes_removed": 0,
                "vectors_updated": 3,
                "chunks_total": 5,
            }

    fake = _FakeIndex()
    watcher = VaultWatcher(cfg, index=fake)
    watcher._catch_up(fake)
    assert fake.calls == [{}]  # инкрементальный reindex без changed
    assert "стартовый reindex" in capsys.readouterr().err


def test_legacy_manifest_format_forces_full_reindex(project: Path, fake_embeddings):
    idx = _index(project)
    idx.reindex()
    mpath = project / "vault/.index/manifest.json"
    payload = json.loads(mpath.read_text(encoding="utf-8"))
    payload.pop("format")
    mpath.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    fresh = _index(project)
    res = fresh.reindex(changed=["10_Notes/20261003-note-alpha.md"])
    # changed проигнорирован: старая раскладка chunk id → полная пересборка
    assert res["notes_changed"] == 2


class _FakeCol:
    def __init__(self):
        self.upserts: list[list[str]] = []
        self.deletes: list[list[str]] = []

    def upsert(self, ids, embeddings, documents, metadatas):
        self.upserts.append(list(ids))

    def delete(self, ids):
        self.deletes.append(list(ids))


class _FakeClient:
    def __init__(self, limit: int):
        self.limit = limit

    def get_max_batch_size(self) -> int:
        return self.limit


def test_vector_store_batches_upsert_and_delete():
    store = VectorStore.__new__(VectorStore)
    store.client = _FakeClient(3)
    store.col = _FakeCol()

    ids = [f"tickets/t{i}.md#0" for i in range(10)]
    store.upsert(ids, [[0.0]] * 10, ["t"] * 10, [{"source": "tickets"}] * 10)
    assert [len(b) for b in store.col.upserts] == [3, 3, 3, 1]
    assert [i for b in store.col.upserts for i in b] == ids

    store.delete(ids)
    assert [len(b) for b in store.col.deletes] == [3, 3, 3, 1]
