from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = {"mall": "北馆十一月", "clean": "南馆核对样例"}
CLOSURE_SAMPLES = {
    "east": {
        "label": "东区改造停业告知",
        "ledger": ROOT / "sample_data" / "east.json",
        "notice": ROOT / "sample_data" / "closure.json",
    }
}
DEFAULT_PERIOD = "2026-11"
RULE_VERSION = "rent-1.0"
CLOSURE_RULE_VERSION = "closure-1.0"
MISSING = {"", "-", "N/A", "NULL", "未知"}
MAX_SOURCE_BYTES = 256 * 1024
MAX_SECTION_ROWS = 1000
