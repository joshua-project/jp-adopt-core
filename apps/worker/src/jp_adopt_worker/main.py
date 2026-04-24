"""CLI entry: `uv run jp-adopt-worker` (wraps `arq` with correct worker class)."""

from __future__ import annotations

import sys


def run_worker() -> None:
    from arq.cli import cli

    sys.argv = ["arq", "jp_adopt_worker.worker_settings.ArqWorkerSettings"]
    cli.main(standalone_mode=True)


if __name__ == "__main__":
    run_worker()
