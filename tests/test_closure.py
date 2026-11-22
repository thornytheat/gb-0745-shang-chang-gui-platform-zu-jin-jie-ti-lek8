import io
from pathlib import Path

import pytest
from app.modules.m8_closure.allocation import money
from app.modules.m8_closure.notice import open_notice
from app.modules.m8_closure.service import calculate, prepare_ledger, run_once
from app.modules.m8_closure.store import ClosureStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def east_bytes():
    return (ROOT / "sample_data" / "east.json").read_bytes()


@pytest.fixture(scope="module")
def notice_bytes():
    return (ROOT / "sample_data" / "closure.json").read_bytes()


@pytest.fixture(scope="module")
def east_run(east_bytes, notice_bytes):
    return calculate(east_bytes, notice_bytes)


@pytest.fixture
def closure_store(tmp_path):
    return ClosureStore(tmp_path / "closure.sqlite")


def test_sample_pool_is_never_exceeded(east_run):
    pool = east_run["补偿池勾稽"]
    assert pool["已分配分"] <= pool["补偿池分"]
    assert pool["已分配分"] + pool["未分配分"] == pool["补偿池分"]
    assert sum(row["补偿分"] for row in east_run["可发布补偿"]) == pool["已分配分"]


def test_pool_caps_when_losses_exceed_pool(east_run):
    pool = east_run["补偿池勾稽"]
    assert pool["可发布损失分"] > pool["补偿池分"]
    assert pool["是否封顶"] is True
    assert pool["可分配上限分"] == pool["补偿池分"]


def test_highest_loss_shop_gets_more_than_rent_share(east_run):
    by_shop = {row["柜台"]: row for row in east_run["可发布补偿"]}
    # 101 的销售与保底都高于 102，按损失分配应高于按保底份额的对照值。
    assert by_shop["101"]["补偿分"] > by_shop["101"]["对照保底份额分"]
    assert by_shop["101"]["补偿分"] == max(row["补偿分"] for row in east_run["可发布补偿"])
    # 102 的保底高、销售低，按损失分配少于按租金份额，正是本题要修正的偏差。
    assert by_shop["102"]["补偿分"] < by_shop["102"]["对照保底份额分"]


def test_every_publishable_row_has_days_and_basis(east_run):
    for row in east_run["可发布补偿"]:
        assert row["停业天数"] == 9
        assert row["停业起"] == "2026-11-20" and row["停业止"] == "2026-11-28"
        assert row["参考账期"] == "2026-10"
        assert row["依据"]


def test_incomplete_reference_sales_suspended(east_run):
    suspended = {row["柜台"]: row for row in east_run["挂起柜台"]}
    assert suspended["103"]["原因码"] == "REF_SALES_INCOMPLETE"
    assert "251" in suspended["103"]["依据"]


def test_contract_problems_suspended(east_run):
    suspended = {row["柜台"]: row for row in east_run["挂起柜台"]}
    assert suspended["104"]["原因码"] == "CLOSURE_CONTRACT_OVERLAP"
    assert suspended["105"]["原因码"] == "CLOSURE_CONTRACT_GAP"


def test_suspended_not_mixed_into_publishable(east_run):
    published = {row["柜台"] for row in east_run["可发布补偿"]}
    suspended = {row["柜台"] for row in east_run["挂起柜台"]}
    assert published.isdisjoint(suspended)
    assert published | suspended == set(east_run["运行清单"]["柜台"])


def test_allocation_conserves_split():
    from app.modules.m8_closure.allocation import _split

    result = _split(100, {"a": 1, "b": 1, "c": 1})
    assert sum(result.values()) == 100
    assert max(result.values()) - min(result.values()) <= 1


def test_zero_loss_gets_zero_share():
    from app.modules.m8_closure.allocation import _split

    assert _split(0, {"a": 5, "b": 5}) == {"a": 0, "b": 0}


def test_money_format():
    assert money(68965) == "689.65"
    assert money(-2000) == "−20.00"


def test_rerun_adds_no_compensation(closure_store, east_bytes, notice_bytes):
    first, reused = run_once(closure_store, east_bytes, notice_bytes)
    second, again = run_once(closure_store, east_bytes, notice_bytes)
    assert reused is False and again is True
    assert first["运行清单"]["批次"] == second["运行清单"]["批次"]
    assert len(closure_store.versions(first["停业告知"]["告知号"])) == 1


def test_new_ledger_supersedes_not_stacks(closure_store, east_bytes, notice_bytes):
    first, _ = run_once(closure_store, east_bytes, notice_bytes)
    changed = east_bytes.replace(b'"500"', b'"510"', 1)
    second, reused = run_once(closure_store, changed, notice_bytes)
    assert reused is False
    versions = closure_store.versions(first["停业告知"]["告知号"])
    assert [item["状态"] for item in versions] == ["published", "superseded"]
    assert second["版本"] == 2
    current = closure_store.current(first["停业告知"]["告知号"])
    assert current["运行清单"]["批次"] == second["运行清单"]["批次"]


def test_failed_run_keeps_old_version(closure_store, east_bytes, notice_bytes):
    first, _ = run_once(closure_store, east_bytes, notice_bytes)
    broken = east_bytes[: len(east_bytes) // 2]
    with pytest.raises(Exception):
        run_once(closure_store, broken, notice_bytes)
    versions = closure_store.versions(first["停业告知"]["告知号"])
    assert len(versions) == 1 and versions[0]["状态"] == "published"
    assert closure_store.current(first["停业告知"]["告知号"])["运行清单"]["批次"] == first["运行清单"]["批次"]


def test_notice_validation(notice_bytes):
    bad = notice_bytes.replace(b'"2026-11-28"', b'"2026-11-10"')
    with pytest.raises(Exception):
        open_notice(bad)


def test_reference_must_precede_closure(notice_bytes):
    bad = notice_bytes.replace(b'"2026-10"', b'"2026-11"')
    with pytest.raises(Exception):
        open_notice(bad)


def test_unknown_shop_rejected(east_bytes):
    notice = open_notice(
        b'{"notice":"x","mall":"east","start":"2026-11-20","end":"2026-11-28",'
        b'"reference":"2026-10","pool":"1","shops":["999"]}'
    )
    from app.errors import InputError
    from app.modules.m8_closure.service import prepare_ledger

    ledger = prepare_ledger(east_bytes)
    assert "999" not in {c["shop"] for c in ledger["contracts"]}
    with pytest.raises(InputError):
        from app.modules.m8_closure.service import calculate

        calculate(east_bytes, b'{"notice":"x","mall":"east","start":"2026-11-20",'
                               b'"end":"2026-11-28","reference":"2026-10","pool":"1",'
                               b'"shops":["999"]}')


def test_pool_below_losses_is_capped(east_bytes, notice_bytes):
    import json

    notice = json.loads(notice_bytes)
    notice["pool"] = "10000.00"
    run = calculate(east_bytes, json.dumps(notice).encode())
    pool = run["补偿池勾稽"]
    assert pool["是否封顶"] is False
    assert pool["已分配分"] == pool["可发布损失分"] == 720000
    assert pool["未分配分"] == 280000


def test_closure_api_reuses_and_versions(client):
    first = client.post("/api/closure/runs", json={"sample": "east"})
    second = client.post("/api/closure/runs", json={"sample": "east"})
    assert [first.status_code, second.status_code] == [201, 200]
    assert first.json["id"] == second.json["id"]
    assert second.json["reused"] is True
    assert second.json["allocated"] <= second.json["pool"]
    versions = client.get("/api/closure/notice/东区改造-2026-11/versions").json
    assert len(versions) == 1


def test_closure_api_failure_keeps_version(client, east_bytes, notice_bytes):
    published = client.post("/api/closure/runs", json={"sample": "east"}).json
    broken = client.post(
        "/api/closure/runs",
        data={
            "file": (io.BytesIO(east_bytes[:400]), "e.json"),
            "notice": (io.BytesIO(notice_bytes), "n.json"),
        },
    )
    assert broken.status_code == 422
    versions = client.get("/api/closure/notice/东区改造-2026-11/versions").json
    assert [item["批次"] for item in versions] == [published["id"]]


def test_all_suspended_refuses_to_publish(east_bytes, notice_bytes):
    import json

    from app.errors import InputError

    notice = json.loads(notice_bytes)
    notice["shops"] = ["103", "104", "105"]
    with pytest.raises(InputError) as exc:
        calculate(east_bytes, json.dumps(notice).encode())
    assert exc.value.code == "NOTHING_PUBLISHABLE"


def test_closure_page_lists_days_amount_and_basis(client):
    client.post("/api/closure/runs", json={"sample": "east"})
    page = client.get("/work/closure").get_data(as_text=True)
    for expected in ("停业天数", "补偿 · 元", "停业损失 · 元", "合同依据", "挂起柜台", "补偿池勾稽"):
        assert expected in page
    assert "1,250.00" in page and "750.00" in page
