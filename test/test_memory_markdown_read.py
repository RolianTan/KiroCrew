"""Tests for the markdown memory read surface.

Covers ``kirocrew memory show`` (the documented-but-previously-missing
command) and ``kirocrew memory export --include-markdown``, plus the
``MemoryStore`` readers behind them. The most important guard is that
``export`` WITHOUT the flag stays byte-identical to its previous shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import cli_commands
from kiro_crew.memory import MemoryStore

# ── Helpers ──


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(workspace=tmp_path / "ws")


def _populated_store(tmp_path: Path) -> MemoryStore:
    ms = _store(tmp_path)
    ms.init()
    ms.write_preferences("# User Preferences\n\n- prefers pytest\n")
    ms.write_projects("# Active Projects\n\n- shipping the read API\n")
    history = tmp_path / "ws" / "memory" / "history"
    (history / "2026-01-01.md").write_text(
        "# 2026-01-01\n\n#### 09:00\nold day\n", encoding="utf-8"
    )
    (history / "2026-03-05.md").write_text(
        "# 2026-03-05\n\n#### 10:00\nnew day\n", encoding="utf-8"
    )
    (history / "2026-02-02.md").write_text(
        "# 2026-02-02\n\n#### 11:00\nmid day\n", encoding="utf-8"
    )
    (history / "notes.md").write_text("not a daily file\n", encoding="utf-8")
    return ms


def _show_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "mem_action": "show",
        "target": None,
        "format": "md",
        "since": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class _EmptyVectorStore:
    """Stub with exactly the surface ``_memory_cmd`` export uses."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def init(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_all_semantic(self) -> list:
        return []

    def get_episodic_list(self, limit: int = 0) -> list:
        return []

    def get_events(self, limit: int = 0) -> list:
        return []

    def import_memory(self, data: dict) -> dict:
        return {"semantic": 0, "episodic": 0, "skipped": 0}


# ── MemoryStore readers ──


class TestMarkdownSnapshot:
    def test_missing_files_are_normal_not_errors(self, tmp_path: Path) -> None:
        snap = _store(tmp_path).markdown_snapshot()
        for key in ("preferences", "projects"):
            assert snap[key]["content"] == ""
            assert snap[key]["updated_at"] is None
            assert snap[key]["path"]
        assert snap["history"] == []

    def test_reads_content_and_utc_mtime(self, tmp_path: Path) -> None:
        snap = _populated_store(tmp_path).markdown_snapshot()
        assert "- prefers pytest" in snap["preferences"]["content"]
        assert "- shipping the read API" in snap["projects"]["content"]
        for key in ("preferences", "projects"):
            parsed = datetime.fromisoformat(snap[key]["updated_at"])
            assert parsed.utcoffset() is not None and not parsed.utcoffset()
            assert str(tmp_path) in snap[key]["path"]

    def test_history_entries_sorted_dated_and_non_daily_skipped(self, tmp_path: Path) -> None:
        entries = _populated_store(tmp_path).markdown_snapshot()["history"]
        assert [e["date"] for e in entries] == ["2026-01-01", "2026-02-02", "2026-03-05"]
        assert all("updated_at" in e and "path" in e and "content" in e for e in entries)
        assert not any("notes" in e["path"] for e in entries)

    def test_since_filters_history_days(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        entries = ms.markdown_snapshot(since=date(2026, 2, 1))["history"]
        assert [e["date"] for e in entries] == ["2026-02-02", "2026-03-05"]


class TestHistorySnapshotAggregateBounds:
    """Many valid dated files must not yield an unbounded snapshot."""

    def test_entry_count_cap_keeps_newest_days_oldest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        for day in range(1, 10):
            (history / f"2026-01-{day:02d}.md").write_text(f"day {day}\n", encoding="utf-8")
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_ENTRIES", 3)
        entries = ms.read_history_entries()
        assert [e["date"] for e in entries] == ["2026-01-07", "2026-01-08", "2026-01-09"]

    def test_cumulative_byte_cap_trims_older_days(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        for day in range(1, 5):
            (history / f"2026-01-{day:02d}.md").write_text("x" * 100, encoding="utf-8")
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_BYTES", 250)
        entries = ms.read_history_entries()
        # Newest two fit (200 bytes); the third would exceed 250 and stops the walk.
        assert [e["date"] for e in entries] == ["2026-01-03", "2026-01-04"]

    def test_single_oversized_day_is_still_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        (history / "2026-01-01.md").write_text("x" * 100, encoding="utf-8")
        (history / "2026-01-02.md").write_text("y" * 100, encoding="utf-8")
        monkeypatch.setattr(MemoryStore, "_HISTORY_SNAPSHOT_MAX_BYTES", 50)
        entries = ms.read_history_entries()
        # The newest entry always lands even when it alone exceeds the cap
        # (its size is bounded by the per-file read cap, not this one).
        assert [e["date"] for e in entries] == ["2026-01-02"]


class TestMarkdownSnapshotSymlinkGuard:
    """A planted link in the agent-writable memory dir must never leak file
    contents through the read API. No HOME/USERPROFILE overrides here: the
    guard is the lstat-based no-link gate plus in-root containment, which
    reject the escaping link regardless of what is_sensitive_path anchors to
    — and leaving HOME real keeps the exercised gate protecting the real
    credential roots."""

    SECRET = "aws_secret_access_key = SUPERSECRET"

    def _secret_outside_memory_root(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        secret = outside / "credentials"
        secret.write_text(self.SECRET, encoding="utf-8")
        return secret

    def _symlink_or_skip(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows CI
            pytest.skip("symlinks not available on this platform")

    def test_history_symlink_to_outside_file_is_skipped(self, tmp_path: Path) -> None:
        secret = self._secret_outside_memory_root(tmp_path)
        ms = _populated_store(tmp_path)
        history = tmp_path / "ws" / "memory" / "history"
        self._symlink_or_skip(history / "2026-05-05.md", secret)
        snapshot = ms.markdown_snapshot()
        assert self.SECRET not in json.dumps(snapshot)
        assert [e["date"] for e in snapshot["history"]] == [
            "2026-01-01",
            "2026-02-02",
            "2026-03-05",
        ]

    def test_preferences_symlink_to_outside_file_yields_empty_entry(self, tmp_path: Path) -> None:
        secret = self._secret_outside_memory_root(tmp_path)
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        self._symlink_or_skip(tmp_path / "ws" / "memory" / "preferences.md", secret)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None

    def test_symlinked_history_dir_is_refused(self, tmp_path: Path) -> None:
        real_history = tmp_path / "elsewhere" / "history"
        real_history.mkdir(parents=True)
        (real_history / "2026-04-04.md").write_text("# 2026-04-04\nleak\n", encoding="utf-8")
        ms = _store(tmp_path)
        (tmp_path / "ws" / "memory").mkdir(parents=True)
        self._symlink_or_skip(tmp_path / "ws" / "memory" / "history", real_history)
        assert ms.markdown_snapshot()["history"] == []


class TestGuardedReadRobustness:
    """Special, oversized, malformed, and concurrently-rewritten files must
    degrade to empty entries or a consistent retry — never a crash, an OOM
    read, or content paired with another version's metadata."""

    def test_invalid_utf8_yields_empty_entry(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        (tmp_path / "ws" / "memory" / "preferences.md").write_bytes(b"\xff\xfe broken \x80")
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None

    def test_oversized_file_yields_empty_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import hooks

        ms = _store(tmp_path)
        ms.init()
        (tmp_path / "ws" / "memory" / "preferences.md").write_text("x" * 64, encoding="utf-8")
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 16)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None

    def test_fifo_special_file_is_rejected_not_read(self, tmp_path: Path) -> None:
        import os

        if not hasattr(os, "mkfifo"):  # pragma: no cover - Windows CI
            pytest.skip("mkfifo not available on this platform")
        ms = _store(tmp_path)
        ms.init()
        history = tmp_path / "ws" / "memory" / "history"
        os.mkfifo(history / "2026-06-06.md")
        assert ms.markdown_snapshot()["history"] == []

    def test_concurrent_rewrite_retries_to_consistent_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import memory as memory_mod

        ms = _store(tmp_path)
        ms.init()
        prefs_path = tmp_path / "ws" / "memory" / "preferences.md"
        prefs_path.write_text("old version", encoding="utf-8")
        real_reader = memory_mod.safe_read_file_bytes_nolink
        state = {"raced": False}

        def racing_reader(raw: str, **kwargs: object) -> bytes | None:
            data = real_reader(raw, **kwargs)
            if not state["raced"]:
                state["raced"] = True
                prefs_path.write_text("new version longer", encoding="utf-8")
            return data

        monkeypatch.setattr(memory_mod, "safe_read_file_bytes_nolink", racing_reader)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == "new version longer"
        assert prefs["updated_at"] is not None

    def test_file_changing_on_every_read_degrades_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import memory as memory_mod

        ms = _store(tmp_path)
        ms.init()
        prefs_path = tmp_path / "ws" / "memory" / "preferences.md"
        prefs_path.write_text("v0", encoding="utf-8")
        real_reader = memory_mod.safe_read_file_bytes_nolink
        counter = {"n": 0}

        def always_racing(raw: str, **kwargs: object) -> bytes | None:
            data = real_reader(raw, **kwargs)
            counter["n"] += 1
            # Vary the LENGTH each round: the size delta guarantees the
            # before/after comparison sees the change even on filesystems
            # whose mtime granularity is coarser than this loop.
            prefs_path.write_text("v" * (counter["n"] + 2), encoding="utf-8")
            return data

        monkeypatch.setattr(memory_mod, "safe_read_file_bytes_nolink", always_racing)
        prefs = ms.markdown_snapshot()["preferences"]
        assert prefs["content"] == ""
        assert prefs["updated_at"] is None


class TestUncWorkspaceGate:
    """On Windows, an agent-configured UNC workspace must be rejected
    LEXICALLY before any stat/glob/exists — those calls are themselves the
    outbound SMB credential probe."""

    def _unc_store(self, monkeypatch: pytest.MonkeyPatch) -> "object":
        import types

        from kiro_crew import memory as memory_mod
        from kiro_crew.memory import MemoryStore

        store = MemoryStore(workspace=Path("//evil-host/share/ws"))
        # Patch ONLY memory.py's view of os (its sole use is the gate's
        # os.name check) — patching the global os.name would make pathlib
        # dispatch WindowsPath everywhere on a POSIX test host.
        monkeypatch.setattr(memory_mod, "os", types.SimpleNamespace(name="nt"))
        return store

    def test_snapshot_refuses_unc_workspace_without_filesystem_touch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import memory as memory_mod

        ms = self._unc_store(monkeypatch)

        def boom(*args: object, **kwargs: object) -> bytes | None:  # pragma: no cover
            raise AssertionError("filesystem reader must not be reached for a UNC workspace")

        monkeypatch.setattr(memory_mod, "safe_read_file_bytes_nolink", boom)
        snapshot = ms.markdown_snapshot()
        assert snapshot["preferences"]["content"] == ""
        assert snapshot["preferences"]["updated_at"] is None
        assert snapshot["history"] == []

    def test_non_windows_is_unaffected(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        assert "- prefers pytest" in ms.markdown_snapshot()["preferences"]["content"]


# ── kirocrew memory show ──


class TestMemoryShowCli:
    def test_show_each_target_markdown(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        ms = _populated_store(tmp_path)
        expected = {
            "preferences": "- prefers pytest",
            "projects": "- shipping the read API",
            "history": "mid day",
        }
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            for target, marker in expected.items():
                cli_commands._memory_cmd(_show_args(target=target))
                assert marker in capsys.readouterr().out

    def test_show_all_targets_when_omitted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _populated_store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args())
        out = capsys.readouterr().out
        assert "- prefers pytest" in out and "- shipping the read API" in out and "old day" in out

    def test_show_json_returns_structured_entries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _populated_store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args(target="preferences", format="json"))
            prefs = json.loads(capsys.readouterr().out)
            assert set(prefs) == {"path", "updated_at", "content"}
            cli_commands._memory_cmd(_show_args(target="history", format="json"))
            history = json.loads(capsys.readouterr().out)
            assert [e["date"] for e in history] == ["2026-01-01", "2026-02-02", "2026-03-05"]

    def test_show_history_since_filter(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        ms = _populated_store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(
                _show_args(target="history", format="json", since="2026-03-01")
            )
        history = json.loads(capsys.readouterr().out)
        assert [e["date"] for e in history] == ["2026-03-05"]

    def test_show_empty_store_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args(target="preferences"))
        assert capsys.readouterr().out == ""

    def test_since_rejected_for_non_history_target(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with pytest.raises(SystemExit) as excinfo:
                cli_commands._memory_cmd(_show_args(target="preferences", since="2026-01-01"))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "--since applies only to history" in captured.err
        assert captured.out == ""

    def test_invalid_since_date_exits_nonzero_with_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A scheduled JSON consumer must get a failure signal, not non-JSON
        text on stdout with exit 0."""
        ms = _store(tmp_path)
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            with pytest.raises(SystemExit) as excinfo:
                cli_commands._memory_cmd(_show_args(target="history", since="March 1st"))
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Invalid --since date" in captured.err
        assert captured.out == ""

    def test_store_reads_where_the_consolidator_writes(self) -> None:
        """The read surface must anchor where the gateway's consolidator
        (the writer of this layer) writes: the bare MemoryStore default."""
        from kiro_crew.memory import MemoryStore, workspace_dir

        store = cli_commands._markdown_memory_store()
        writer = MemoryStore()
        assert store._memory_dir == writer._memory_dir
        assert str(workspace_dir()) in str(store._memory_dir)

    def test_markdown_output_strips_terminal_control_sequences(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- evil \x1b]0;pwned\x07title\n")
        with patch.object(cli_commands, "_markdown_memory_store", lambda: ms):
            cli_commands._memory_cmd(_show_args(target="preferences"))
        out = capsys.readouterr().out
        assert "\x1b" not in out and "pwned" not in out and "evil" in out


# ── kirocrew memory export --include-markdown ──


class TestAtomicMemoryWrites:
    """Writers publish via temp-file + os.replace so a concurrent reader only
    ever observes committed versions — never a truncated in-progress file."""

    def test_writers_produce_content_with_no_temp_residue(self, tmp_path: Path) -> None:
        ms = _store(tmp_path)
        ms.init()
        ms.write_preferences("# User Preferences\n\n- atomic\n")
        ms.write_projects("- project state\n")
        ms.append_history("an entry")
        memory_dir = tmp_path / "ws" / "memory"
        residue = [p.name for p in memory_dir.rglob("*.tmp")]
        assert residue == []
        snap = ms.markdown_snapshot()
        assert "- atomic" in snap["preferences"]["content"]
        assert "- project state" in snap["projects"]["content"]
        assert len(snap["history"]) == 1

    def test_failed_replace_cleans_up_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import atomic_write as atomic_write_mod

        ms = _store(tmp_path)
        ms.init()

        def boom(src: object, dst: object) -> None:
            raise OSError("simulated replace failure")

        # Patch the rename step inside the atomic_write helper the memory
        # writers delegate to — patching the global os.replace would affect
        # unrelated machinery.
        monkeypatch.setattr(atomic_write_mod, "replace_with_retry", boom)
        with pytest.raises(OSError):
            ms.write_preferences("# User Preferences\n\n- lost\n")
        residue = list((tmp_path / "ws" / "memory").rglob("*.tmp"))
        assert residue == []

    def test_history_glob_ignores_temp_names(self, tmp_path: Path) -> None:
        ms = _populated_store(tmp_path)
        history = tmp_path / "ws" / "memory" / "history"
        (history / ".2026-03-05.md.tmp-999").write_text("partial", encoding="utf-8")
        entries = ms.markdown_snapshot()["history"]
        assert [e["date"] for e in entries] == ["2026-01-01", "2026-02-02", "2026-03-05"]
        assert not any(Path(e["path"]).name.endswith("tmp-999") for e in entries)


class TestMemoryExportMarkdown:
    def _export_args(self, include_markdown: bool) -> argparse.Namespace:
        return argparse.Namespace(
            mem_action="export", output=None, include_markdown=include_markdown
        )

    def test_export_without_flag_is_byte_identical_to_previous_shape(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The regression guard that matters most: no flag, no shape change."""
        with patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore):
            cli_commands._memory_cmd(self._export_args(include_markdown=False))
        expected = json.dumps({"semantic": [], "episodic": [], "events": []}, indent=2, default=str)
        assert capsys.readouterr().out == expected + "\n"

    def test_export_with_flag_adds_markdown_collection(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _populated_store(tmp_path)
        with (
            patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore),
            patch.object(cli_commands, "_markdown_memory_store", lambda: ms),
        ):
            cli_commands._memory_cmd(self._export_args(include_markdown=True))
        data = json.loads(capsys.readouterr().out)
        assert list(data) == ["semantic", "episodic", "events", "markdown"]
        markdown = data["markdown"]
        assert "- prefers pytest" in markdown["preferences"]["content"]
        assert [e["date"] for e in markdown["history"]] == [
            "2026-01-01",
            "2026-02-02",
            "2026-03-05",
        ]

    def test_export_with_flag_handles_empty_markdown_layer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ms = _store(tmp_path)
        with (
            patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore),
            patch.object(cli_commands, "_markdown_memory_store", lambda: ms),
        ):
            cli_commands._memory_cmd(self._export_args(include_markdown=True))
        markdown = json.loads(capsys.readouterr().out)["markdown"]
        assert markdown["preferences"]["content"] == ""
        assert markdown["history"] == []


# ── argparse wiring ──


class TestMemoryImportMarkdownNotice:
    """`memory import` never writes the markdown layer — a payload carrying the
    export-only collection must say so instead of silently dropping it."""

    def _import_args(self, file: str) -> argparse.Namespace:
        return argparse.Namespace(mem_action="import", file=file)

    def test_import_with_markdown_collection_prints_notice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        payload = tmp_path / "export.json"
        payload.write_text(
            json.dumps({"semantic": [], "episodic": [], "markdown": {"history": []}}),
            encoding="utf-8",
        )
        with patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore):
            cli_commands._memory_cmd(self._import_args(str(payload)))
        out = capsys.readouterr().out
        assert "export-only" in out and "NOT imported" in out

    def test_import_without_markdown_collection_prints_no_notice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        payload = tmp_path / "export.json"
        payload.write_text(json.dumps({"semantic": [], "episodic": []}), encoding="utf-8")
        with patch.object(cli_commands, "VectorMemoryStore", _EmptyVectorStore):
            cli_commands._memory_cmd(self._import_args(str(payload)))
        assert "export-only" not in capsys.readouterr().out


class TestMemoryCliWiring:
    def test_memory_show_arguments_parse(self) -> None:
        argv = [
            "kirocrew",
            "memory",
            "show",
            "history",
            "--format",
            "json",
            "--since",
            "2026-01-01",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("kiro_crew.cli_commands._memory_cmd") as mock_cmd,
        ):
            from kiro_crew.cli import main

            main()
        ns = mock_cmd.call_args[0][0]
        assert (ns.mem_action, ns.target, ns.format, ns.since) == (
            "show",
            "history",
            "json",
            "2026-01-01",
        )

    def test_memory_show_target_optional_defaults(self) -> None:
        with (
            patch.object(sys, "argv", ["kirocrew", "memory", "show"]),
            patch("kiro_crew.cli_commands._memory_cmd") as mock_cmd,
        ):
            from kiro_crew.cli import main

            main()
        ns = mock_cmd.call_args[0][0]
        assert (ns.target, ns.format, ns.since) == (None, "md", None)

    def test_memory_export_include_markdown_defaults_off(self) -> None:
        with (
            patch.object(sys, "argv", ["kirocrew", "memory", "export"]),
            patch("kiro_crew.cli_commands._memory_cmd") as mock_cmd,
        ):
            from kiro_crew.cli import main

            main()
        assert mock_cmd.call_args[0][0].include_markdown is False
