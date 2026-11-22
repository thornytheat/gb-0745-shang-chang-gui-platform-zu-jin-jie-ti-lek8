from collections import Counter
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.modules.m2_contracts.versions import version_on
from app.modules.m3_calendar.month import days_between
from app.modules.m3_calendar.service import billable_days, prorated_floor

CLOSURE_REASONS = {
    "REF_SALES_INCOMPLETE": "参考期存在被隔离或冲突的收银/退单记录，净销售不完整，无法复核损失",
    "REF_SALES_UNVERIFIABLE": "参考期隔离记录无法判定日期，净销售完整性未知",
    "REF_CONTRACT_GAP": "参考期合同存在空档，无法确定应参考的合同版本",
    "REF_CONTRACT_OVERLAP": "参考期合同版本重叠，无法确定应参考的合同版本",
    "CLOSURE_CONTRACT_GAP": "停业期合同存在空档，停业归属无法确定",
    "CLOSURE_CONTRACT_OVERLAP": "停业期合同版本重叠，停业归属无法确定",
    "NO_REFERENCE_SALES": "参考期没有有效净销售，无法建立停业损失基数",
    "UNKNOWN_SHOP": "告知柜台在账簿中没有可用合同",
}


def money(cents):
    sign = "−" if cents < 0 else ""
    integer, remainder = divmod(abs(cents), 100)
    return f"{sign}{integer:,}.{remainder:02d}"


def _scan_contracts(contracts, shop, start, end_exclusive):
    """Distinct versions plus the gap/overlap counts across [start, end_exclusive)."""
    versions = set()
    problems = Counter()
    for day in days_between(start, end_exclusive):
        contract, error = version_on(contracts, shop, day)
        if error:
            problems[error] += 1
        else:
            versions.add(contract["version"])
    return sorted(versions), problems


def _line_index(raw_events):
    """Map 1-based source line to shop and day, tolerating malformed rows."""
    index = {}
    for line, row in enumerate(raw_events, 1):
        if not isinstance(row, dict):
            continue
        shop = row.get("shop")
        try:
            day = date.fromisoformat(row.get("day"))
        except (TypeError, ValueError):
            day = None
        if isinstance(shop, str) and shop.strip():
            index[line] = (shop.strip(), day)
    return index


def _net_reference(sales, refunds, shop, ref_start, ref_end, known_by):
    """Reference-period sales net of refunds already known when the notice starts.

    Refunds posted on or after the closure start are excluded so the loss base
    cannot change retroactively.
    """
    reference = {
        sale["id"]: sale
        for sale in sales.values()
        if sale["shop"] == shop and ref_start <= sale["day"] < ref_end
    }
    deductions = Counter()
    for refund in refunds:
        if refund["origin"] in reference and refund["day"] < known_by:
            deductions[refund["origin"]] += refund["cents"]
    net = sum(sale["cents"] for sale in reference.values())
    net -= sum(deductions.values())
    return net, len(reference)


def _floor_shares(contracts, exempt, reference):
    """Prorated reference-month floor per shop, used only for the comparison column."""
    days, _, month_days = billable_days(contracts, exempt, reference)
    lookup = {(c["shop"], c["version"]): c for c in contracts}
    shares = {}
    for (shop, version), count in days.items():
        shares[shop] = shares.get(shop, 0) + prorated_floor(
            lookup[(shop, version)]["floor"], count, month_days
        )
    return shares


def _split(target, weights):
    """Largest-remainder split: exact integer total, deterministic shop-id tie-break."""
    total = sum(weights.values())
    if target <= 0 or total <= 0:
        return {key: 0 for key in weights}
    shares, remainders = {}, []
    for key in sorted(weights):
        quotient, remainder = divmod(target * weights[key], total)
        shares[key] = quotient
        remainders.append((remainder, key))
    leftover = target - sum(shares.values())
    for _, key in sorted(remainders, key=lambda item: (-item[0], item[1]))[:leftover]:
        shares[key] += 1
    return shares


def _loss(net, reference_days, closure_days):
    return int(
        (Decimal(net) * closure_days / reference_days).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )


def allocate(contracts, exempt, sales, refunds, isolated, raw_events, notice):
    """Split the fixed pool across publishable shops; unverifiable shops are suspended."""
    index = _line_index(raw_events)
    isolated_lines = []
    for item in isolated:
        shop, day = index.get(item["行号"], (None, None))
        if shop:
            isolated_lines.append((shop, item["行号"], day))

    reference = notice["reference"]
    ref_start, ref_end = notice["reference_start"], notice["reference_end"]
    reference_days = (ref_end - ref_start).days
    closure_days = notice["days"]
    closure_end = notice["end"] + timedelta(days=1)

    publishable, suspended, losses = [], [], {}

    for shop in notice["shops"]:
        ref_versions, ref_problems = _scan_contracts(contracts, shop, ref_start, ref_end)
        close_versions, close_problems = _scan_contracts(contracts, shop, notice["start"], closure_end)
        net, sale_days = _net_reference(
            sales, refunds, shop, ref_start, ref_end, notice["start"]
        )
        incomplete_lines = sorted(
            line
            for (line_shop, line, day) in isolated_lines
            if line_shop == shop and day is not None and ref_start <= day < ref_end
        )
        undated_lines = sorted(
            line for (line_shop, line, day) in isolated_lines if line_shop == shop and day is None
        )

        reason = None
        if not ref_versions and not ref_problems and not close_versions and not close_problems:
            reason = "UNKNOWN_SHOP"
        elif ref_problems.get("NO_CONTRACT"):
            reason = "REF_CONTRACT_GAP"
        elif ref_problems.get("OVERLAP"):
            reason = "REF_CONTRACT_OVERLAP"
        elif close_problems.get("NO_CONTRACT"):
            reason = "CLOSURE_CONTRACT_GAP"
        elif close_problems.get("OVERLAP"):
            reason = "CLOSURE_CONTRACT_OVERLAP"
        elif incomplete_lines:
            reason = "REF_SALES_INCOMPLETE"
        elif undated_lines:
            reason = "REF_SALES_UNVERIFIABLE"
        elif sale_days == 0 or net <= 0:
            reason = "NO_REFERENCE_SALES"

        if reason:
            details = []
            if ref_problems:
                details.append("参考期合同" + ("空档" if ref_problems.get("NO_CONTRACT") else "重叠"))
            if close_problems:
                details.append("停业期合同" + ("空档" if close_problems.get("NO_CONTRACT") else "重叠"))
            if incomplete_lines:
                details.append("参考期隔离行号 " + "、".join(str(line) for line in incomplete_lines))
            if undated_lines:
                details.append("无法判定日期的隔离行号 " + "、".join(str(line) for line in undated_lines))
            suspended.append(
                {
                    "柜台": shop,
                    "停业天数": closure_days,
                    "原因码": reason,
                    "说明": CLOSURE_REASONS[reason],
                    "依据": "；".join(details) or "见原因码",
                    "参考合同版本": "、".join(ref_versions) or "无",
                    "停业合同版本": "、".join(close_versions) or "无",
                }
            )
            continue

        loss = _loss(net, reference_days, closure_days)
        losses[shop] = loss
        publishable.append(
            {
                "柜台": shop,
                "停业起": notice["start"].isoformat(),
                "停业止": notice["end"].isoformat(),
                "停业天数": closure_days,
                "参考账期": reference,
                "参考日数": reference_days,
                "参考净销售分": net,
                "日均销售分": int(
                    (Decimal(net) / reference_days).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                ),
                "停业损失分": loss,
                "合同版本": "、".join(ref_versions),
            }
        )

    total_loss = sum(losses.values())
    target = min(notice["pool"], total_loss)
    shares = _split(target, losses)
    floor_weights = _floor_shares(contracts, exempt, reference)
    comparison = _split(target, {shop: floor_weights.get(shop, 0) for shop in losses})

    for row in publishable:
        loss, share = losses[row["柜台"]], shares[row["柜台"]]
        row["补偿分"] = share
        row["损失占比万分"] = int(Decimal(loss) * 10000 / total_loss) if total_loss else 0
        row["补偿占比万分"] = int(Decimal(share) * 10000 / target) if target else 0
        row["对照保底份额分"] = comparison[row["柜台"]]
        row["对照差异分"] = share - comparison[row["柜台"]]
        row["依据"] = (
            f"{row['合同版本']}；参考 {reference_days} 日净销售 {money(row['参考净销售分'])} 元"
            f" ÷ {reference_days} 日 = 日均 {money(row['日均销售分'])} 元，"
            f"× 停业 {closure_days} 日 = 损失 {money(loss)} 元；"
            f"损失占可发布合计 {money(total_loss)} 元的 "
            f"{row['损失占比万分'] / 100:.2f}%，分配池 {money(target)} 元"
            f" → 补偿 {money(share)} 元"
        )

    allocated = sum(shares.values())
    return {
        "停业告知": {
            "告知号": notice["notice"],
            "商场": notice["mall"],
            "停业起": notice["start"].isoformat(),
            "停业止": notice["end"].isoformat(),
            "停业天数": closure_days,
            "参考账期": reference,
            "参考起": ref_start.isoformat(),
            "参考止": (ref_end - timedelta(days=1)).isoformat(),
            "柜台数": len(notice["shops"]),
            "补偿池分": notice["pool"],
        },
        "补偿池勾稽": {
            "补偿池分": notice["pool"],
            "可发布损失分": total_loss,
            "可分配上限分": target,
            "已分配分": allocated,
            "未分配分": notice["pool"] - allocated,
            "是否封顶": total_loss > notice["pool"],
            "停业天数": closure_days,
            "可发布柜台": len(publishable),
            "挂起柜台": len(suspended),
            "舍入方法": "按损失比例整数分摊，最大余数补齐，合计不超过补偿池",
            "挂起口径": "挂起柜台不进入分配池，其份额留在补偿池，不自动摊给可发布柜台",
            "对照口径": (
                f"同池按参考月保底份额分摊合计 {money(sum(comparison.values()))} 元，仅作对照，不入账"
            ),
        },
        "可发布补偿": publishable,
        "挂起柜台": suspended,
    }
