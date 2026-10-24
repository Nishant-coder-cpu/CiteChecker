"""CiteCheck: evidence-backed academic citation audits."""

from pathlib import Path

from dotenv import load_dotenv

# Modules read settings (PAYMENT_MODE, AGENTIC_MODE, timeouts) at import time,
# so .env must be loaded before any of them is imported.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
