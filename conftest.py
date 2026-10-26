import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_web_outputs(request, tmp_path_factory, monkeypatch):
    if not request.node.path.is_relative_to(Path(__file__).parent / "tests"):
        return
    outputs = tmp_path_factory.mktemp("isolated-web-outputs")
    for name in ("app", "web.app"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "OUTPUTS_DIR"):
            monkeypatch.setattr(module, "OUTPUTS_DIR", outputs)
            if hasattr(module, "BENCH_DIR"):
                bench_dir = outputs / "_benchmarks"
                monkeypatch.setattr(module, "BENCH_DIR", bench_dir)
                monkeypatch.setattr(module, "BENCH_ACCOUNTS_FILE", bench_dir / "accounts.json")
            if hasattr(module, "SESSIONS_DIR"):
                monkeypatch.setattr(module, "SESSIONS_DIR", outputs / "_sessions")
