"""保管人交接与告警归责的 API 集成测试（临时 SQLite 文件覆盖 DB_PATH）。"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    import app.database as database
    import app.services as services
    import app.main as main

    importlib.reload(database)
    importlib.reload(services)
    importlib.reload(main)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def _create_sample(client, sid="s1", **kw):
    payload = {
        "id": sid,
        "name": "测试样本",
        "freeze_threshold_c": -20,
        "thaw_threshold_c": 0,
        "min_allowed_c": -80,
        "max_allowed_c": 8,
        "max_freeze_thaw_cycles": 2,
    }
    payload.update(kw)
    r = client.post("/samples", json=payload)
    assert r.status_code == 201, r.text
    return r


def _submit(client, sid, readings):
    return client.post(f"/samples/{sid}/readings", json={"readings": readings})


def _handover(client, sid, custodian, effective_time):
    return client.post(
        f"/samples/{sid}/custody-handovers",
        json={"custodian": custodian, "effective_time": effective_time},
    )


def _attribution(client, sid):
    r = client.get(f"/samples/{sid}/alert-attribution")
    assert r.status_code == 200, r.text
    return r.json()


def test_handover_register_list_and_backfill(client) -> None:
    """登记交接（含补录较早交接），列表按生效时间升序返回。"""

    _create_sample(client)
    r = _handover(client, "s1", "李四", "2026-09-20T12:00:00Z")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sample_id"] == "s1"
    assert body["custodian"] == "李四"
    assert body["effective_time"].startswith("2026-09-20T12:00:00")
    assert body["id"] > 0 and body["created_at"]

    # 补录更早的交接：允许，且列表中排入正确时间位置
    assert _handover(client, "s1", "张三", "2026-09-20T08:00:00Z").status_code == 201
    assert _handover(client, "s1", "王五", "2026-09-20T16:00:00Z").status_code == 201
    # 无时区时间按 UTC 处理
    assert _handover(client, "s1", "赵六", "2026-09-20T18:00:00").status_code == 201

    rows = client.get("/samples/s1/custody-handovers").json()
    assert [(x["custodian"], x["effective_time"][:19]) for x in rows] == [
        ("张三", "2026-09-20T08:00:00"),
        ("李四", "2026-09-20T12:00:00"),
        ("王五", "2026-09-20T16:00:00"),
        ("赵六", "2026-09-20T18:00:00"),
    ]
    # 无时区输入被归一化为 UTC（响应中以 Z 或 +00:00 偏移表示）
    assert rows[3]["effective_time"].endswith(("Z", "+00:00"))


def test_handover_sample_not_found(client) -> None:
    assert _handover(client, "ghost", "张三", "2026-09-20T08:00:00Z").status_code == 404
    assert client.get("/samples/ghost/custody-handovers").status_code == 404
    assert client.get("/samples/ghost/alert-attribution").status_code == 404


def test_handover_duplicate_moment_rejected(client) -> None:
    """同一样本同一时刻仅能登记一条；不同样本互不影响。"""

    _create_sample(client)
    _create_sample(client, sid="s2")
    assert _handover(client, "s1", "张三", "2026-09-20T08:00:00Z").status_code == 201
    # 同一时刻再次登记（即使保管人不同）-> 409
    r = _handover(client, "s1", "李四", "2026-09-20T08:00:00Z")
    assert r.status_code == 409
    # 同一时刻的不同时区写法归一化后仍视为同一时刻
    assert _handover(client, "s1", "李四", "2026-09-20T16:00:00+08:00").status_code == 409
    # 另一相同样本同一时刻不受影响
    assert _handover(client, "s2", "李四", "2026-09-20T08:00:00Z").status_code == 201
    assert len(client.get("/samples/s1/custody-handovers").json()) == 1


def test_handover_validation(client) -> None:
    _create_sample(client)
    # 保管人为空 / 缺字段 -> 422
    assert _handover(client, "s1", "", "2026-09-20T08:00:00Z").status_code == 422
    assert client.post(
        "/samples/s1/custody-handovers", json={"custodian": "张三"}
    ).status_code == 422
    assert client.post(
        "/samples/s1/custody-handovers",
        json={"effective_time": "2026-09-20T08:00:00Z"},
    ).status_code == 422


def test_alert_attribution_intervals(client) -> None:
    """左闭右开责任区间：首条之前责任人为空，区间之间归对应保管人。"""

    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "e2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
            {"event_id": "e3", "event_time": "2026-09-20T10:00:00Z", "temperature_c": 20},
            {"event_id": "e4", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2},
        ],
    )
    h1 = _handover(client, "s1", "张三", "2026-09-20T09:00:00Z").json()
    h2 = _handover(client, "s1", "李四", "2026-09-20T12:00:00Z").json()

    rows = _attribution(client, "s1")
    assert len(rows) == 2
    # 按告警事件时间升序；首条交接（09:00）之前责任人为空
    assert rows[0]["event_time"].startswith("2026-09-20T08:00:00")
    assert rows[0]["custodian"] is None
    assert rows[0]["handover_id"] is None
    # [09:00, 12:00) 归张三
    assert rows[1]["event_time"].startswith("2026-09-20T10:00:00")
    assert rows[1]["custodian"] == "张三"
    assert rows[1]["handover_id"] == h1["id"]

    # 最后一条交接之后由该保管人负责到底
    _submit(
        client,
        "s1",
        [{"event_id": "e5", "event_time": "2026-09-20T12:30:00Z", "temperature_c": -90}],
    )
    rows = _attribution(client, "s1")
    assert len(rows) == 3
    assert rows[2]["event_time"].startswith("2026-09-20T12:30:00")
    assert rows[2]["custodian"] == "李四"
    assert rows[2]["handover_id"] == h2["id"]


def test_alert_attribution_exact_handover_moment(client) -> None:
    """告警事件时间恰好等于交接生效时刻时归新保管人（左闭）。"""

    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85},
            {"event_id": "e2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2},
        ],
    )
    _handover(client, "s1", "张三", "2026-09-20T08:00:00Z")
    h2 = _handover(client, "s1", "李四", "2026-09-20T09:00:00Z").json()

    rows = _attribution(client, "s1")
    assert len(rows) == 1
    assert rows[0]["event_time"].startswith("2026-09-20T09:00:00")
    assert rows[0]["custodian"] == "李四"
    assert rows[0]["handover_id"] == h2["id"]


def test_attribution_reflects_backfilled_handover(client) -> None:
    """补录较早交接后，既有告警的归责查询立即反映最新结果。"""

    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -85},
            {"event_id": "e2", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2},
        ],
    )
    _handover(client, "s1", "张三", "2026-09-20T08:00:00Z")
    rows = _attribution(client, "s1")
    assert [r["custodian"] for r in rows] == ["张三"]

    # 补录一条更早的交接：10:00 的告警仍归张三（[08:00, ...) 被 09:30 截断前的部分不变）
    h_early = _handover(client, "s1", "李四", "2026-09-20T09:30:00Z").json()
    rows = _attribution(client, "s1")
    assert len(rows) == 1
    assert rows[0]["custodian"] == "李四"
    assert rows[0]["handover_id"] == h_early["id"]


def test_attribution_reflects_recompute(client) -> None:
    """迟到读数引起告警重算后，归责查询只含最新告警，不残留过期归责。"""

    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "e2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
        ],
    )
    _handover(client, "s1", "张三", "2026-09-20T07:00:00Z")
    before = _attribution(client, "s1")
    assert len(before) == 1
    assert before[0]["event_time"].startswith("2026-09-20T08:00:00")
    assert before[0]["custodian"] == "张三"

    # 迟到读数 07:30 也在越限：越限告警重新锚定到 07:30，旧告警被清除
    _submit(
        client,
        "s1",
        [{"event_id": "late", "event_time": "2026-09-20T07:30:00Z", "temperature_c": -90}],
    )
    rows = _attribution(client, "s1")
    assert len(rows) == 1
    assert rows[0]["alert_id"] != before[0]["alert_id"]
    assert rows[0]["event_time"].startswith("2026-09-20T07:30:00")
    assert rows[0]["custodian"] == "张三"

    # 再补录 07:15 的交接：07:30 的告警改归新保管人
    h = _handover(client, "s1", "李四", "2026-09-20T07:15:00Z").json()
    rows = _attribution(client, "s1")
    assert len(rows) == 1
    assert rows[0]["custodian"] == "李四"
    assert rows[0]["handover_id"] == h["id"]


def test_attribution_keeps_ack_semantics(client) -> None:
    """归责查询不影响既有告警确认语义；确认状态也不影响归责结果。"""

    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": -85},
            {"event_id": "e2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
        ],
    )
    alert = client.get("/alerts?sample_id=s1").json()[0]
    r = client.post(f"/alerts/{alert['id']}/ack", json={"acknowledged": True})
    assert r.json()["acknowledged"] is True

    h = _handover(client, "s1", "张三", "2026-09-20T07:00:00Z").json()
    # 已确认告警仍参与归责
    rows = _attribution(client, "s1")
    assert len(rows) == 1
    assert rows[0]["alert_id"] == alert["id"]
    assert rows[0]["custodian"] == "张三"
    assert rows[0]["handover_id"] == h["id"]
    # 确认状态未被归责查询/交接登记改动
    assert client.get(f"/alerts/{alert['id']}").json()["acknowledged"] is True

    # 取消确认与批量确认语义不变
    r = client.post(f"/alerts/{alert['id']}/ack", json={"acknowledged": False})
    assert r.json()["acknowledged"] is False
    n = client.post("/samples/s1/alerts/ack", json={"acknowledged": True}).json()["updated"]
    assert n == 1
    assert client.get("/samples/s1/state").json()["unacknowledged_alert_count"] == 0
    rows = _attribution(client, "s1")
    assert len(rows) == 1 and rows[0]["custodian"] == "张三"


def test_attribution_empty_cases(client) -> None:
    """无告警时归责为空；无交接记录时责任人为空。"""

    _create_sample(client)
    # 无交接、无告警
    assert _attribution(client, "s1") == []

    # 有交接、无告警
    _handover(client, "s1", "张三", "2026-09-20T08:00:00Z")
    assert _attribution(client, "s1") == []

    # 有告警、无交接（另一样本）：责任人为空
    _create_sample(client, sid="s2")
    _submit(
        client,
        "s2",
        [{"event_id": "x1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85}],
    )
    rows = _attribution(client, "s2")
    assert len(rows) == 1
    assert rows[0]["custodian"] is None
    assert rows[0]["handover_id"] is None


def test_handover_cascade_on_sample_delete(client) -> None:
    """删除样本级联清理交接记录；同名重建后不带旧交接。"""

    _create_sample(client)
    _handover(client, "s1", "张三", "2026-09-20T08:00:00Z")
    _submit(
        client,
        "s1",
        [{"event_id": "e1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": -85}],
    )
    assert len(_attribution(client, "s1")) == 1

    assert client.delete("/samples/s1").status_code == 204
    _create_sample(client)
    assert client.get("/samples/s1/custody-handovers").json() == []
    assert _attribution(client, "s1") == []
