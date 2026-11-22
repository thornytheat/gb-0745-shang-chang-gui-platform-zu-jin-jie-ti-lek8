from time import perf_counter

from app.errors import InputError
from app.manifest import manifest
from app.modules.m1_intake.reader import open_payload
from app.modules.m1_intake.service import parse_events
from app.modules.m2_contracts.service import bind_sales, version_conflicts
from app.modules.m2_contracts.versions import normalize_contracts
from app.modules.m3_calendar.service import exempt_days
from app.modules.m5_refunds.credits import match_refunds
from app.modules.m8_closure.allocation import allocate
from app.modules.m8_closure.notice import closure_identity, open_notice


def prepare_ledger(raw):
    """Reuse the rent pipeline's verified intake, binding and refund matching."""
    payload = open_payload(raw)
    contracts = normalize_contracts(payload["contracts"])
    exempt = exempt_days(payload["holidays"])
    events, isolated, duplicates = parse_events(payload["events"])
    sales, invalid_sales = bind_sales(contracts, events)
    refunds, invalid_refunds = match_refunds(events, sales)
    isolated = sorted(isolated + invalid_sales + invalid_refunds, key=lambda item: item["行号"])
    return {
        "contracts": contracts,
        "exempt": exempt,
        "sales": sales,
        "refunds": refunds,
        "events": payload["events"],
        "isolated": isolated,
        "duplicates": duplicates,
        "conflicts": version_conflicts(contracts),
    }


def calculate(ledger_raw, notice_raw):
    notice = open_notice(notice_raw)
    ledger = prepare_ledger(ledger_raw)
    run_id, parameters = closure_identity(ledger_raw, notice)
    started = perf_counter()

    ledger_shops = {c["shop"] for c in ledger["contracts"]}
    missing = [shop for shop in notice["shops"] if shop not in ledger_shops]
    if missing:
        raise InputError(
            "NOTICE_SHOP_UNKNOWN",
            "停业告知中的柜台在账簿中无合同：" + "、".join(missing),
        )

    result = allocate(
        ledger["contracts"],
        ledger["exempt"],
        ledger["sales"],
        ledger["refunds"],
        ledger["isolated"],
        ledger["events"],
        notice,
    )
    if not result["可发布补偿"]:
        # 全部挂起时不发布：不写库、不新增补偿，旧版本保持不变，并报出挂起原因。
        summary = "；".join(
            f"{row['柜台']} {row['原因码']}" for row in result["挂起柜台"]
        )
        raise InputError("NOTHING_PUBLISHABLE", f"全部柜台挂起，未生成补偿：{summary}")

    result["运行清单"] = manifest(run_id, parameters, round((perf_counter() - started) * 1000))
    result["账簿核对"] = {
        "收到交易行": len(ledger["events"]),
        "采信销售": len(ledger["sales"]),
        "已配对退单": len(ledger["refunds"]),
        "隔离": len(ledger["isolated"]),
        "重复": len(ledger["duplicates"]),
        "合同冲突": ledger["conflicts"],
    }
    result["隔离明细"] = ledger["isolated"]
    return result


def run_once(store, ledger_raw, notice_raw):
    notice = open_notice(notice_raw)
    key, _ = closure_identity(ledger_raw, notice)
    existing = store.get(key)
    if existing is not None:
        return existing, True
    # 先完整计算，失败会抛错且不会触碰已发布版本。
    data = calculate(ledger_raw, notice_raw)
    return store.commit(notice["notice"], key, data)
