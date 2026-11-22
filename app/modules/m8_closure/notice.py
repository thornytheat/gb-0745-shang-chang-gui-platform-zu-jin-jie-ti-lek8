import hashlib
import json
from datetime import date

from app.config import CLOSURE_RULE_VERSION, MAX_SOURCE_BYTES, MISSING
from app.errors import InputError
from app.modules.m1_intake.amounts import cents
from app.modules.m1_intake.reader import reject_constant, unique_fields
from app.modules.m3_calendar.month import month_window

REQUIRED = ("notice", "mall", "start", "end", "reference", "pool", "shops")
MAX_SHOPS = 200
MAX_WINDOW_DAYS = 366


def text_field(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label}须为文本")
    text = value.strip()
    if not text or len(text) > 100 or text.upper() in MISSING:
        raise ValueError(f"{label}为空或过长")
    return text


def open_notice(raw):
    if not raw.strip():
        raise InputError("EMPTY_NOTICE", "停业告知为空")
    if len(raw) > MAX_SOURCE_BYTES:
        raise InputError("TOO_LARGE", "停业告知超过 256 KiB")
    try:
        data = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=unique_fields, parse_constant=reject_constant
        )
    except (ValueError, RecursionError) as exc:
        raise InputError("BAD_NOTICE", "停业告知 JSON、编码或字段无效") from exc
    if not isinstance(data, dict) or not set(REQUIRED) <= set(data):
        raise InputError("BAD_NOTICE", "停业告知字段不完整")
    try:
        notice_id = text_field(data["notice"], "告知号")
        mall = text_field(data["mall"], "商场")
        start = date.fromisoformat(data["start"])
        end = date.fromisoformat(data["end"])
        if end < start or (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError("停业日期区间无效")
        reference = data["reference"]
        reference_start, reference_end = month_window(reference)
        if reference_end > start:
            raise ValueError("参考账期须完整且早于停业开始")
        pool = cents(data["pool"])
        if pool < 0:
            raise ValueError("补偿池不可为负")
        shops_raw = data["shops"]
        if not isinstance(shops_raw, list) or not shops_raw or len(shops_raw) > MAX_SHOPS:
            raise ValueError("柜台清单须为 1 至 200 个柜台")
        shops = [text_field(item, "柜台") for item in shops_raw]
        if len(set(shops)) != len(shops):
            raise ValueError("柜台清单重复")
    except InputError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise InputError("BAD_NOTICE", "停业告知日期、补偿池或柜台无效") from exc
    normalized = {
        "notice": notice_id,
        "mall": mall,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "reference": reference,
        "pool": pool,
        "shops": sorted(shops),
    }
    digest = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {
        "notice": notice_id,
        "mall": mall,
        "start": start,
        "end": end,
        "reference": reference,
        "reference_start": reference_start,
        "reference_end": reference_end,
        "pool": pool,
        "shops": sorted(shops),
        "digest": digest,
        # 停业告知按自然日理解，起止当天均停业。
        "days": (end - start).days + 1,
    }


def closure_identity(ledger_raw, notice):
    parameters = {
        "规则": CLOSURE_RULE_VERSION,
        "告知摘要": notice["digest"],
        "账簿摘要": hashlib.sha256(ledger_raw).hexdigest(),
        "告知号": notice["notice"],
        "商场": notice["mall"],
        "停业起": notice["start"].isoformat(),
        "停业止": notice["end"].isoformat(),
        "停业天数": notice["days"],
        "参考账期": notice["reference"],
        "补偿池分": notice["pool"],
        "柜台": sorted(notice["shops"]),
    }
    encoded = json.dumps(parameters, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()[:24], parameters
