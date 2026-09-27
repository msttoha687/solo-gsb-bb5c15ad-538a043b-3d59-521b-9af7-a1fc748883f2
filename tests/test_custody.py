"""保管人交接与告警归责的 API 集成测试。"""

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


def _handover(client, sid, custodian, effective_time, note=None):
    payload = {"custodian": custodian, "effective_time": effective_time}
    if note is not None:
        payload["note"] = note
    return client.post(f"/samples/{sid}/custody-handovers", json=payload)


def _custody(client, sid):
    r = client.get(f"/samples/{sid}/alert-custody")
    assert r.status_code == 200, r.text
    return r.json()


def _three_alert_sample(client):
    """构造三条告警：08:00 / 09:00 两次高温越限 + 12:30 第 3 次循环超限。"""

    _create_sample(client)
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T08:00:00Z", "temperature_c": 20},
            {"event_id": "e2", "event_time": "2026-09-20T08:30:00Z", "temperature_c": 2},
            {"event_id": "e3", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 20},
            {"event_id": "e4", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2},
            {"event_id": "e5", "event_time": "2026-09-20T10:00:00Z", "temperature_c": -22},
            {"event_id": "e6", "event_time": "2026-09-20T10:30:00Z", "temperature_c": 2},
            {"event_id": "e7", "event_time": "2026-09-20T11:00:00Z", "temperature_c": -22},
            {"event_id": "e8", "event_time": "2026-09-20T11:30:00Z", "temperature_c": 2},
            {"event_id": "e9", "event_time": "2026-09-20T12:00:00Z", "temperature_c": -22},
            {"event_id": "e10", "event_time": "2026-09-20T12:30:00Z", "temperature_c": 2},
        ],
    )
    alerts = client.get("/alerts?sample_id=s1").json()
    by_time = {a["event_time"]: a for a in alerts}
    assert len(alerts) == 3
    return by_time


# ---------------- 交接登记与查询 ----------------


def test_handover_registration_listing_and_backfill(client) -> None:
    _create_sample(client)

    # 样本不存在：登记 / 查询均 404
    assert _handover(client, "nope", "张三", "2026-09-20T08:00:00Z").status_code == 404
    assert client.get("/samples/nope/custody-handovers").status_code == 404
    assert client.get("/samples/nope/alert-custody").status_code == 404

    # 先登记较晚的交接，再补录较早交接：列表始终按生效时间升序
    r = _handover(client, "s1", "张三", "2026-09-20T09:00:00Z", note="早班接管")
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["sample_id"] == "s1"
    assert first["custodian"] == "张三"
    assert first["note"] == "早班接管"
    assert first["effective_time"].startswith("2026-09-20T09:00:00")
    assert first["created_at"]

    r = _handover(client, "s1", "李四", "2026-09-20T08:00:00Z")
    assert r.status_code == 201
    second = r.json()
    assert second["note"] is None

    handovers = client.get("/samples/s1/custody-handovers").json()
    assert [h["custodian"] for h in handovers] == ["李四", "张三"]
    # 补录的较早交接（后登记、id 更大）按生效时间排在前面，而非登记顺序
    assert [h["id"] for h in handovers] == [second["id"], first["id"]]


def test_handover_time_normalization(client) -> None:
    _create_sample(client)
    # 带时区换算为 UTC；无时区按 UTC 处理
    r = _handover(client, "s1", "张三", "2026-09-20T10:00:00+02:00")
    assert r.status_code == 201
    assert r.json()["effective_time"].startswith("2026-09-20T08:00:00")
    r = _handover(client, "s1", "李四", "2026-09-20T09:00:00")
    assert r.status_code == 201
    assert r.json()["effective_time"].startswith("2026-09-20T09:00:00")


def test_handover_duplicate_same_moment_rejected(client) -> None:
    _create_sample(client)
    assert _handover(client, "s1", "张三", "2026-09-20T08:00:00Z").status_code == 201
    # 同一样本同一时刻仅能登记一条（等价写法同样冲突）
    assert _handover(client, "s1", "李四", "2026-09-20T08:00:00Z").status_code == 409
    assert _handover(client, "s1", "李四", "2026-09-20T10:00:00+02:00").status_code == 409
    # 不同时刻 / 不同样本不受影响
    assert _handover(client, "s1", "李四", "2026-09-20T09:00:00Z").status_code == 201
    _create_sample(client, sid="s2")
    assert _handover(client, "s2", "李四", "2026-09-20T08:00:00Z").status_code == 201


def test_handover_validation(client) -> None:
    _create_sample(client)
    # custodian 为空 / 缺失 / 缺生效时间
    assert _handover(client, "s1", "", "2026-09-20T08:00:00Z").status_code == 422
    r = client.post(
        "/samples/s1/custody-handovers",
        json={"effective_time": "2026-09-20T08:00:00Z"},
    )
    assert r.status_code == 422
    r = client.post("/samples/s1/custody-handovers", json={"custodian": "张三"})
    assert r.status_code == 422
    # 非法时间格式
    assert _handover(client, "s1", "张三", "not-a-time").status_code == 422


# ---------------- 告警归责查询 ----------------


def test_alert_custody_intervals_and_exact_boundary(client) -> None:
    by_time = _three_alert_sample(client)
    a0800 = by_time["2026-09-20T08:00:00Z"]["id"]
    a0900 = by_time["2026-09-20T09:00:00Z"]["id"]
    a1230 = by_time["2026-09-20T12:30:00Z"]["id"]

    # 无交接：全部责任人为空
    items = _custody(client, "s1")
    assert [i["alert_id"] for i in items] == [a0800, a0900, a1230]
    assert all(i["custodian"] is None and i["handover_id"] is None for i in items)

    # 登记 09:00 张三：08:00 在首条之前为空；09:00 恰在交接时刻归新保管人
    h1 = _handover(client, "s1", "张三", "2026-09-20T09:00:00Z").json()
    items = _custody(client, "s1")
    assert [(i["custodian"], i["handover_id"]) for i in items] == [
        (None, None),
        ("张三", h1["id"]),
        ("张三", h1["id"]),
    ]

    # 补录更早的 08:00 李四：08:00 恰在交接时刻归李四（新保管人），其余不变
    h2 = _handover(client, "s1", "李四", "2026-09-20T08:00:00Z").json()
    items = _custody(client, "s1")
    assert [(i["alert_id"], i["custodian"], i["handover_id"]) for i in items] == [
        (a0800, "李四", h2["id"]),
        (a0900, "张三", h1["id"]),
        (a1230, "张三", h1["id"]),
    ]

    # 再补 12:30 王五：12:30 的循环超限告警恰在交接时刻归王五
    h3 = _handover(client, "s1", "王五", "2026-09-20T12:30:00Z").json()
    items = _custody(client, "s1")
    assert [(i["custodian"], i["handover_id"]) for i in items] == [
        ("李四", h2["id"]),
        ("张三", h1["id"]),
        ("王五", h3["id"]),
    ]
    # 返回字段即告警 ID、事件时间、保管人与交接 ID
    assert set(items[0]) == {"alert_id", "event_time", "custodian", "handover_id"}
    assert items[0]["event_time"].startswith("2026-09-20T08:00:00")


def test_alert_custody_reflects_recompute_no_stale(client) -> None:
    """迟到读数 / 配置变更引起告警重算后，归责查询立即反映最新告警集合。"""

    _create_sample(client)
    _handover(client, "s1", "张三", "2026-09-20T08:00:00Z")
    _submit(
        client,
        "s1",
        [
            {"event_id": "e1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 20},
            {"event_id": "e2", "event_time": "2026-09-20T09:30:00Z", "temperature_c": 2},
        ],
    )
    items = _custody(client, "s1")
    assert len(items) == 1 and items[0]["custodian"] == "张三"

    # 放宽允许区间：越限告警被重算清除，归责查询不再返回过期条目
    assert client.patch("/samples/s1", json={"max_allowed_c": 30}).status_code == 200
    assert _custody(client, "s1") == []

    # 收紧回 8：告警重新出现（新告警行），归责即时生效
    assert client.patch("/samples/s1", json={"max_allowed_c": 8}).status_code == 200
    items = _custody(client, "s1")
    assert len(items) == 1 and items[0]["custodian"] == "张三"

    # 迟到读数在交接生效之前越限（07:00 越限、07:30 回到区间内）：
    # 重算后新增一条 07:00 告警，归责为空（首条交接之前）；09:00 告警仍归张三
    _submit(
        client,
        "s1",
        [
            {"event_id": "late1", "event_time": "2026-09-20T07:00:00Z", "temperature_c": 50},
            {"event_id": "late2", "event_time": "2026-09-20T07:30:00Z", "temperature_c": 2},
        ],
    )
    items = _custody(client, "s1")
    assert len(items) == 2
    assert (items[0]["custodian"], items[0]["handover_id"]) == (None, None)
    assert items[0]["event_time"].startswith("2026-09-20T07:00:00")
    assert items[1]["custodian"] == "张三"
    assert items[1]["handover_id"] is not None


def test_alert_custody_backfilled_handover_reassigns_existing_alerts(client) -> None:
    """补录交接不持久化归责：已有告警的归责随查询即时更新。"""

    _create_sample(client)
    _submit(
        client,
        "s1",
        [{"event_id": "e1", "event_time": "2026-09-20T09:00:00Z", "temperature_c": 20}],
    )
    assert _custody(client, "s1")[0]["custodian"] is None

    h = _handover(client, "s1", "张三", "2026-09-20T08:00:00Z").json()
    item = _custody(client, "s1")[0]
    assert item["custodian"] == "张三"
    assert item["handover_id"] == h["id"]

    # 再补一条更早的交接：10:00 之后仍归张三，09:00 告警不受影响
    _handover(client, "s1", "李四", "2026-09-20T07:00:00Z")
    assert _custody(client, "s1")[0]["custodian"] == "张三"


def test_alert_custody_keeps_ack_semantics(client) -> None:
    """归责查询不影响既有告警确认语义；已确认告警同样参与归责。"""

    by_time = _three_alert_sample(client)
    a0800 = by_time["2026-09-20T08:00:00Z"]["id"]
    _handover(client, "s1", "张三", "2026-09-20T07:00:00Z")

    # 确认一条告警：归责查询仍返回全部当前告警（含已确认）
    r = client.post(f"/alerts/{a0800}/ack", json={"acknowledged": True})
    assert r.json()["acknowledged"] is True
    items = _custody(client, "s1")
    assert len(items) == 3
    assert all(i["custodian"] == "张三" for i in items)

    # 归责查询不改写确认状态
    alerts = {a["id"]: a for a in client.get("/alerts?sample_id=s1").json()}
    assert alerts[a0800]["acknowledged"] is True
    assert client.get("/samples/s1/state").json()["unacknowledged_alert_count"] == 2

    # 迟到读数触发重算：确认状态按既有规则保留，归责同步反映新告警集合
    _submit(
        client,
        "s1",
        [{"event_id": "late", "event_time": "2026-09-20T09:15:00Z", "temperature_c": 3}],
    )
    alerts = {a["id"]: a for a in client.get("/alerts?sample_id=s1").json()}
    assert alerts[a0800]["acknowledged"] is True
    assert len(_custody(client, "s1")) == 3


def test_handovers_cascade_on_sample_delete(client) -> None:
    _create_sample(client)
    _handover(client, "s1", "张三", "2026-09-20T08:00:00Z")
    _handover(client, "s1", "李四", "2026-09-20T09:00:00Z")

    assert client.delete("/samples/s1").status_code == 204
    assert client.get("/samples/s1/custody-handovers").status_code == 404
    assert client.get("/samples/s1/alert-custody").status_code == 404

    # 同名样本重建后交接记录已被级联清理
    _create_sample(client)
    assert client.get("/samples/s1/custody-handovers").json() == []
