# SPDX-License-Identifier: Apache-2.0
"""Issue #232 — config parity: CLI > env > yaml > code default.

Covers:
- ``--max-files`` gains ``ATHENAEUM_MAX_FILES`` env + ``librarian.max_files``
  yaml, mirroring the #220 ``--max-api-calls`` precedence chain.
- New ``models:`` yaml section for the three previously env-only model knobs
  (``models.classify`` / ``models.write`` / ``models.topic``). Env wins over
  yaml per knob; the resolver model stays at ``resolve.model`` (untouched).
- Regression guard: none of the new keys are seeded into ``config._DEFAULTS``
  (the #231 shadowing bug) — yaml is read only when the operator set it.

No live API calls; every client is a fake.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

import athenaeum.contradictions as contradictions_mod
import athenaeum.query_topics as query_topics_mod
import athenaeum.tiers as tiers_mod
from athenaeum.cli import main
from athenaeum.config import _DEFAULTS, load_config
from athenaeum.librarian import DEFAULT_MAX_FILES, librarian_max_files, run
from athenaeum.models import AutoMemoryFile, RawFile

# ---------------------------------------------------------------------------
# librarian_max_files resolver (env > yaml > default)
# ---------------------------------------------------------------------------


class TestLibrarianMaxFiles:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_FILES", raising=False)
        assert DEFAULT_MAX_FILES == 50
        assert librarian_max_files() == DEFAULT_MAX_FILES
        assert librarian_max_files({}) == DEFAULT_MAX_FILES

    def test_yaml_wins_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_FILES", raising=False)
        assert librarian_max_files({"librarian": {"max_files": 7}}) == 7

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_FILES", "3")
        assert librarian_max_files({"librarian": {"max_files": 7}}) == 3

    def test_bool_yaml_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`max_files: yes` must not silently become a window of 1."""
        monkeypatch.delenv("ATHENAEUM_MAX_FILES", raising=False)
        assert (
            librarian_max_files({"librarian": {"max_files": True}}) == DEFAULT_MAX_FILES
        )

    def test_negative_yaml_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_FILES", raising=False)
        assert (
            librarian_max_files({"librarian": {"max_files": -1}}) == DEFAULT_MAX_FILES
        )

    @pytest.mark.parametrize("bad_env", ["banana", "-2", ""])
    def test_bad_env_falls_through_to_yaml(
        self, bad_env: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_MAX_FILES", bad_env)
        assert librarian_max_files({"librarian": {"max_files": 7}}) == 7

    def test_non_dict_librarian_section_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_MAX_FILES", raising=False)
        assert librarian_max_files({"librarian": "oops"}) == DEFAULT_MAX_FILES


# ---------------------------------------------------------------------------
# run() resolves max_files when the CLI didn't pin it
# ---------------------------------------------------------------------------


def _seed_knowledge_root(tmp_path: Path, n_files: int) -> Path:
    """Minimal knowledge root: wiki/, raw/sessions/ with *n_files*, git repo."""
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    for i in range(n_files):
        (sessions / f"2024041{i}T120000Z-aabbccd{i}.md").write_text(
            f"Raw intake file number {i}.\n", encoding="utf-8"
        )
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


class TestRunResolvesMaxFiles:
    def test_env_caps_window_when_arg_omitted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=2)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ATHENAEUM_MAX_FILES", "1")
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
        )

        assert rc == 0
        messages = [r.getMessage() for r in caplog.records]
        assert any("processing 1 of 2 files" in m for m in messages), messages

    def test_explicit_arg_wins_over_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = _seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ATHENAEUM_MAX_FILES", "1")
        caplog.set_level(logging.INFO, logger="athenaeum")

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            dry_run=True,
            max_files=2,
        )

        assert rc == 0
        messages = [r.getMessage() for r in caplog.records]
        assert any("processing 2 of 3 files" in m for m in messages), messages


# ---------------------------------------------------------------------------
# CLI --max-files (flag wins; default defers to the resolver)
# ---------------------------------------------------------------------------


def _capture_librarian_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace librarian.run with a kwargs-capturing stub returning 0."""
    import athenaeum.librarian as librarian_mod

    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(librarian_mod, "run", fake_run)
    return captured


class TestMaxFilesCLI:
    def test_default_passes_none_so_resolver_decides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_librarian_run(monkeypatch)
        rc = main(["run", "--knowledge-root", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert "max_files" in captured
        assert captured["max_files"] is None

    def test_explicit_flag_passes_value_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_librarian_run(monkeypatch)
        rc = main(
            [
                "run",
                "--knowledge-root",
                str(tmp_path),
                "--dry-run",
                "--max-files",
                "7",
            ]
        )
        assert rc == 0
        assert captured["max_files"] == 7

    @pytest.mark.parametrize("bad", ["0", "-3", "banana"])
    def test_rejects_zero_negative_and_garbage(
        self, bad: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["run", "--max-files", bad])
        assert excinfo.value.code == 2
        assert "--max-files" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Model knobs: env > yaml models.<knob> > code default
# ---------------------------------------------------------------------------

_MODEL_CASES: list[tuple[Callable[..., str], str, str, str]] = [
    (
        tiers_mod._get_classify_model,
        "ATHENAEUM_CLASSIFY_MODEL",
        "classify",
        tiers_mod.DEFAULT_CLASSIFY_MODEL,
    ),
    (
        tiers_mod._get_write_model,
        "ATHENAEUM_WRITE_MODEL",
        "write",
        tiers_mod.DEFAULT_WRITE_MODEL,
    ),
    (
        contradictions_mod._get_model,
        "ATHENAEUM_CLASSIFY_MODEL",
        "classify",
        contradictions_mod.DEFAULT_CONTRADICTION_MODEL,
    ),
    (
        query_topics_mod._get_topic_model,
        "ATHENAEUM_TOPIC_MODEL",
        "topic",
        query_topics_mod.DEFAULT_TOPIC_MODEL,
    ),
]

_MODEL_IDS = ["tiers-classify", "tiers-write", "contradictions-classify", "topic"]


@pytest.mark.parametrize(
    ("getter", "env_var", "yaml_key", "default"), _MODEL_CASES, ids=_MODEL_IDS
)
class TestModelKnobResolution:
    def test_default_when_unset(
        self,
        getter: Callable[..., str],
        env_var: str,
        yaml_key: str,
        default: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getter() == default  # no-arg back-compat
        assert getter(None) == default
        assert getter({}) == default

    def test_yaml_wins_over_default(
        self,
        getter: Callable[..., str],
        env_var: str,
        yaml_key: str,
        default: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getter({"models": {yaml_key: "yaml-model"}}) == "yaml-model"

    def test_env_wins_over_yaml(
        self,
        getter: Callable[..., str],
        env_var: str,
        yaml_key: str,
        default: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(env_var, "env-model")
        assert getter({"models": {yaml_key: "yaml-model"}}) == "env-model"

    @pytest.mark.parametrize(
        "bad_models",
        ["oops", {"__other__": "x"}, None],
        ids=["scalar-section", "key-missing", "none-section"],
    )
    def test_malformed_yaml_falls_back(
        self,
        getter: Callable[..., str],
        env_var: str,
        yaml_key: str,
        default: str,
        bad_models: object,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getter({"models": bad_models}) == default

    def test_non_string_or_blank_yaml_value_falls_back(
        self,
        getter: Callable[..., str],
        env_var: str,
        yaml_key: str,
        default: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        assert getter({"models": {yaml_key: 3}}) == default
        assert getter({"models": {yaml_key: "   "}}) == default


# ---------------------------------------------------------------------------
# Plumbing: yaml-configured models reach the actual API calls
# ---------------------------------------------------------------------------


def _fake_anthropic_client(payload_text: str) -> MagicMock:
    """MagicMock mirroring anthropic.Anthropic().messages.create(...)."""
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    client.messages.create.return_value = response
    return client


def _write_am(scope_dir: Path, filename: str, body: str) -> AutoMemoryFile:
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        "---\nname: probe\ntype: feedback\n---\n" + body + "\n", encoding="utf-8"
    )
    return AutoMemoryFile(
        path=path, origin_scope=scope_dir.name, memory_type="feedback", name="probe"
    )


class TestModelPlumbing:
    def test_tier2_classify_uses_yaml_classify_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CLASSIFY_MODEL", raising=False)
        client = _fake_anthropic_client("[]")
        raw = RawFile(
            path=tmp_path / "20240410T120000Z-aabbccdd.md",
            source="sessions",
            timestamp="20240410T120000Z",
            uuid8="aabbccdd",
            _content="Met with Alice Zhang about product strategy.",
        )
        tiers_mod.tier2_classify(
            raw,
            [],
            ["person"],
            ["active"],
            ["open"],
            client,
            config={"models": {"classify": "yaml-classify-model"}},
        )
        assert client.messages.create.call_args.kwargs["model"] == "yaml-classify-model"

    def test_detect_contradictions_uses_yaml_classify_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CLASSIFY_MODEL", raising=False)
        client = _fake_anthropic_client('{"detected": false}')
        m1 = _write_am(tmp_path / "scope", "a.md", "Claim A.")
        m2 = _write_am(tmp_path / "scope", "b.md", "Claim B.")
        contradictions_mod.detect_contradictions(
            [m1, m2], client, config={"models": {"classify": "yaml-detect-model"}}
        )
        assert client.messages.create.call_args.kwargs["model"] == "yaml-detect-model"

    def test_extract_topics_uses_yaml_topic_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anthropic

        monkeypatch.delenv("ATHENAEUM_TOPIC_MODEL", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        captured: dict[str, Any] = {}

        class _FakeClient:
            def __init__(self, **_: Any) -> None:
                self.messages = self

            def create(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                response = MagicMock()
                response.content = [MagicMock(text='["Return Path"]')]
                return response

        monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
        topics = query_topics_mod.extract_topics(
            "Tell me about Return Path",
            config={"models": {"topic": "yaml-topic-model"}},
        )
        assert topics == ["Return Path"]
        assert captured["model"] == "yaml-topic-model"

    def test_cli_query_topics_passes_loaded_config(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The hook entry point must load athenaeum.yaml and hand it to
        extract_topics — otherwise models.topic is dead config on the only
        production path that calls it."""
        captured: dict[str, Any] = {}

        def fake_extract(
            prompt: str, timeout: float = 3.0, config: Any = None
        ) -> list[str]:
            captured["config"] = config
            return ["a-topic"]

        monkeypatch.setattr("athenaeum.query_topics.extract_topics", fake_extract)
        monkeypatch.setattr(
            "athenaeum.config.load_config",
            lambda knowledge_root=None: {"models": {"topic": "cfg-model"}},
        )
        rc = main(["query-topics", "hello world prompt"])
        assert rc == 0
        assert captured["config"] == {"models": {"topic": "cfg-model"}}
        assert "a-topic" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# #231 regression guard + template documentation
# ---------------------------------------------------------------------------


class TestNoNewDefaultsSeeded:
    """New keys must NOT be seeded into _DEFAULTS — resolvers read yaml
    only-if-set, else fall through to env/code default (issue #231)."""

    def test_models_and_librarian_not_seeded(self, tmp_path: Path) -> None:
        assert "models" not in _DEFAULTS
        assert "librarian" not in _DEFAULTS
        cfg = load_config(tmp_path)
        assert "models" not in cfg
        assert "max_files" not in (cfg.get("librarian") or {})

    def test_user_yaml_models_pass_through(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            "models:\n  classify: my-classify\n  write: my-write\n  topic: my-topic\n"
            "librarian:\n  max_files: 9\n"
        )
        cfg = load_config(tmp_path)
        assert cfg["models"] == {
            "classify": "my-classify",
            "write": "my-write",
            "topic": "my-topic",
        }
        assert cfg["librarian"]["max_files"] == 9


class TestTemplateAdvertisesNewKeys:
    def test_template_documents_max_files(self) -> None:
        from athenaeum.config import _DEFAULT_CONFIG_CONTENT

        assert "#   max_files: 50" in _DEFAULT_CONFIG_CONTENT

    def test_template_documents_models_section(self) -> None:
        from athenaeum.config import _DEFAULT_CONFIG_CONTENT

        assert "# models:" in _DEFAULT_CONFIG_CONTENT
        assert "#   classify:" in _DEFAULT_CONFIG_CONTENT
        assert "#   write:" in _DEFAULT_CONFIG_CONTENT
        assert "#   topic:" in _DEFAULT_CONFIG_CONTENT
