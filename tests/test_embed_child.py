"""SessionEnd の分離子プロセス (`index --embed`) の負荷抑制と上限。"""

import os

from myak import config, indexer, vec
from tests.idx_helpers import rec, text, write


def test_embed_flag_limits_and_throttles(db_env, tmp_path, monkeypatch, capsys):
    p = write(tmp_path / "s1.jsonl", [rec("u1", "user", text(1)), rec("a1", "assistant", text(2))])
    seen = {}
    nices = []
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setattr(os, "nice", lambda n: nices.append(n))

    def fake(*, allow_download=False, limit=None):
        seen["limit"] = limit
        seen["omp"] = os.environ.get("OMP_NUM_THREADS")
        return 0, "stub"

    monkeypatch.setattr(vec, "try_embed_pending", fake)
    indexer.run_from_cli([f"--file={p}", "--embed"])
    assert seen == {"limit": config.HOOK_EMBED_LIMIT, "omp": "2"}
    assert config.HOOK_EMBED_LIMIT == 500
    assert nices == [10]
    assert "Embedding skipped: stub" in capsys.readouterr().out
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
