#!/usr/bin/env python3
"""Read-only Gemini list-models helper (no generate_content calls).

Prints:
  (a) model ids containing "flash-lite" (exact API ids — copy into config yourself)
  (b) the currently configured pipeline_model / model_id primary

Never writes config. Safe to run when the generate quota is exhausted.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from src.config_loader import PROJECT_ROOT, load_model_config


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key == "your-gemini-api-key-here":
        print("GEMINI_API_KEY missing or placeholder in .env", file=sys.stderr)
        return 2

    cfg = load_model_config()
    primary = str(cfg.get("pipeline_model") or cfg.get("model_id") or "")

    try:
        import google.generativeai as genai
    except ImportError:
        print("google-generativeai not installed", file=sys.stderr)
        return 2

    genai.configure(api_key=key)
    flash_lite: list[str] = []
    all_ids: list[str] = []
    for model in genai.list_models():
        name = getattr(model, "name", None) or ""
        # API returns "models/<id>"; normalize to bare id for config.
        model_id = name.split("/", 1)[-1] if name else ""
        if not model_id:
            continue
        all_ids.append(model_id)
        if "flash-lite" in model_id.lower():
            flash_lite.append(model_id)

    print("=== Gemini list-models (read-only) ===")
    print(f"(b) Configured primary pipeline_model: {primary}")
    print(f"(a) Model ids containing 'flash-lite' ({len(flash_lite)}):")
    if flash_lite:
        for mid in sorted(flash_lite):
            print(f"  - {mid}")
    else:
        print("  (none found)")
    print(f"Total models visible with this key: {len(all_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
