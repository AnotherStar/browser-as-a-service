"""Dump the OpenAPI schema to ../client/openapi.json (no server needed)."""
import json
import pathlib

from app.main import app

out = pathlib.Path(__file__).resolve().parent.parent / "client" / "openapi.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {out}")
