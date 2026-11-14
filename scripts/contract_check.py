"""Contract gate: checks the API against the agreed contract v1.1.

Runs the app in-process (FastAPI TestClient, no network) and verifies the
points the frontend relies on (handoff repo PREGUNTAS.md P-001 and P-003):
unified ErrorResponse envelope and latitude range [-90, 90] with Apollo sites.

Exit code 1 if any check fails. Production deploys are blocked until it passes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


# P-001: ErrorResponse schema published in OpenAPI
schemas = app.openapi().get("components", {}).get("schemas", {})
check("openapi: ErrorResponse schema", "ErrorResponse" in schemas)

# P-001: invalid coordinates -> 422 with {"error": {"code": "ERR_INVALID_COORDINATES"}}
r = client.get("/api/v1/terrain/patch", params={"lat": 95, "lon": 10})
body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
code = body.get("error", {}).get("code") if isinstance(body.get("error"), dict) else None
check(
    "lat=95 -> 422 ERR_INVALID_COORDINATES",
    r.status_code == 422 and code == "ERR_INVALID_COORDINATES",
    f"status={r.status_code} body={str(body)[:120]}",
)

# P-003: latitude range [-90, 90] (Apollo 11 at 0.674, 23.473)
r = client.get("/api/v1/terrain/patch", params={"lat": 0.674, "lon": 23.473})
check("terrain/patch accepts lat=0.674 (Apollo 11)", r.status_code == 200, f"status={r.status_code}")

r = client.post(
    "/api/v1/evaluate",
    json={"lat": 0.674, "lon": 23.473, "start_utc": "2026-12-01T00:00:00Z"},
)
check("evaluate accepts lat=0.674 (Apollo 11)", r.status_code == 200, f"status={r.status_code}")

# P-003: catalog includes Apollo sites
r = client.get("/api/v1/sites")
ids = [s.get("id") for s in r.json().get("sites", [])] if r.status_code == 200 else []
check("sites includes apollo-11", "apollo-11" in ids, f"count={len(ids)}")

width = max(len(name) for name, _, _ in results)
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail if not ok else ''}".rstrip())

failed = [name for name, ok, _ in results if not ok]
print(f"\ncontract: {len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
