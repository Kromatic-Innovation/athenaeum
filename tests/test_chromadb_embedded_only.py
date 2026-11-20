"""SECURITY.md says athenaeum uses chromadb embedded and never starts or
connects to a chromadb server (athenaeum#2913). This guard keeps that true:
it fails if anything under ``src/athenaeum`` names a server-mode entry point.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

# Server-mode identifiers. Embedded use is ``chromadb.PersistentClient``.
_SERVER_MODE = re.compile(
    r"\b(?:AsyncHttpClient|HttpClient|chromadb\.server|trust_remote_code)\b"
    r"|\bchroma\s+run\b"
)


def find_server_mode_references(root: Path) -> list[str]:
    """Return ``path:line: text`` for each server-mode reference under ``root``."""
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _SERVER_MODE.search(line):
                hits.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    return hits


def test_src_never_starts_or_connects_to_a_chromadb_server() -> None:
    assert SRC.is_dir()
    assert find_server_mode_references(SRC) == []


def test_scanner_flags_server_mode_client_in_a_fake_tree(tmp_path: Path) -> None:
    """Negative control: a server-mode client added to the tree must be caught."""
    (tmp_path / "clean.py").write_text(
        "import chromadb\nc = chromadb.PersistentClient(path='x')\n", encoding="utf-8"
    )
    assert find_server_mode_references(tmp_path) == []
    for bad in (
        "c = chromadb.HttpClient(host='example.net')",
        "c = chromadb.AsyncHttpClient(host='example.net')",
        "import chromadb.server",
        "subprocess.run(['chroma', 'run'])  # chroma run --path x",
        "Embedder(trust_remote_code=True)",
    ):
        (tmp_path / "bad.py").write_text(bad + "\n", encoding="utf-8")
        hits = find_server_mode_references(tmp_path)
        assert len(hits) == 1 and hits[0].startswith("bad.py:1:"), bad
