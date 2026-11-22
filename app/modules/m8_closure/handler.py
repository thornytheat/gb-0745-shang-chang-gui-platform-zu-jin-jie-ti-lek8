import json
from io import BytesIO

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file, url_for

from app.config import CLOSURE_SAMPLES
from app.errors import InputError
from app.modules.m8_closure.service import run_once

routes = Blueprint("closure", __name__)

SAMPLE_LABELS = {code: item["label"] for code, item in CLOSURE_SAMPLES.items()}


def store():
    return current_app.extensions["closure-runs"]


def sample_bytes(name):
    if name not in CLOSURE_SAMPLES:
        raise InputError("BAD_SAMPLE", "请选择可用样例")
    return CLOSURE_SAMPLES[name]["ledger"].read_bytes(), CLOSURE_SAMPLES[name]["notice"].read_bytes()


def find_run(run_id):
    result = store().get(run_id)
    if result is None:
        abort(404)
    return result


def read_pair():
    """Return (ledger_raw, notice_raw) from uploads, else the selected sample."""
    ledger = request.files.get("file")
    notice = request.files.get("notice")
    if ledger and ledger.filename:
        notice_raw = notice.read() if notice and notice.filename else b""
        if not notice_raw:
            raise InputError("BAD_NOTICE", "上传账簿时须同时提供停业告知")
        return ledger.read(), notice_raw
    sample = request.form.get("sample", "east")
    return sample_bytes(sample)


def resolve_run():
    chosen = request.args.get("run")
    if chosen:
        return find_run(chosen)
    ledger_raw, notice_raw = sample_bytes(request.args.get("sample", "east"))
    run, _ = run_once(store(), ledger_raw, notice_raw)
    return run


@routes.get("/work/closure")
def work():
    run = resolve_run()
    notice_id = run["停业告知"]["告知号"]
    chosen = request.args.get("run")
    if chosen:
        sample = None
    else:
        sample = request.args.get("sample", "east")
    return render_template(
        "closure.html",
        active="closure",
        title="停业补偿",
        stages=current_app.config["STAGE_LINKS"],
        run=run,
        meta=run["运行清单"],
        samples=SAMPLE_LABELS,
        sample=sample,
        reused=request.args.get("reuse"),
        versions=store().versions(notice_id),
        notice_id=notice_id,
        pool_ok=run["补偿池勾稽"]["已分配分"] <= run["补偿池勾稽"]["补偿池分"],
    )


@routes.get("/api/closure/<run_id>")
def inspect(run_id):
    run = find_run(run_id)
    return jsonify(
        {
            key: run[key]
            for key in (
                "运行清单",
                "停业告知",
                "补偿池勾稽",
                "可发布补偿",
                "挂起柜台",
                "账簿核对",
                "隔离明细",
                "版本",
                "状态",
            )
        }
    )


@routes.get("/api/closure/notice/<path:notice_id>/versions")
def notice_versions(notice_id):
    return jsonify(store().versions(notice_id))


@routes.get("/api/closure/export/<run_id>")
def export(run_id):
    run = find_run(run_id)
    body = json.dumps(run, ensure_ascii=False, indent=2) + "\n"
    return send_file(
        BytesIO(body.encode()),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"closure-{run['运行清单']['批次']}.json",
    )


@routes.post("/api/closure/runs")
def execute():
    if request.is_json:
        sample = (request.get_json(silent=True) or {}).get("sample", "east")
        ledger_raw, notice_raw = sample_bytes(sample)
    else:
        if not request.files and not request.form:
            raise InputError("BAD_REQUEST", "请求字段无效")
        ledger_raw, notice_raw = read_pair()
    run, reused = run_once(store(), ledger_raw, notice_raw)
    pool = run["补偿池勾稽"]
    return (
        jsonify(
            id=run["运行清单"]["批次"],
            notice=run["停业告知"]["告知号"],
            version=run.get("版本"),
            status=run.get("状态", "published"),
            reused=reused,
            location=url_for("closure.work", run=run["运行清单"]["批次"]),
            pool=pool["补偿池分"],
            allocated=pool["已分配分"],
            remaining=pool["未分配分"],
            publishable=pool["可发布柜台"],
            suspended=pool["挂起柜台"],
        ),
        200 if reused else 201,
    )
