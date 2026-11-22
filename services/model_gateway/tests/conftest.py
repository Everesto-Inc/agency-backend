import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("MODEL_GATEWAY_CLIENTS", "ev_research=" + "s" * 40 + ",edge=" + "e" * 40)
