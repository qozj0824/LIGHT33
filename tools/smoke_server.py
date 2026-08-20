from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402


def main() -> None:
    client = TestClient(app)
    health = client.get("/health")
    index = client.get("/")
    javascript = client.get("/static/app.js")
    assert health.status_code == 200 and health.json()["ok"] is True
    assert index.status_code == 200 and "NØXIS" in index.text
    assert javascript.status_code == 200
    print("server smoke test: PASS")


if __name__ == "__main__":
    main()
