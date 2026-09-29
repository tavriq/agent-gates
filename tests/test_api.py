"""Сценарии приёмки S1–S8 из SPEC.md."""
import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client():
    main.reset_store()
    return TestClient(main.app)


def setup(client, rate_type="hourly", rate=400):
    site = client.post("/sites", json={"name": "Склад", "rate_type": rate_type, "rate": rate}).json()
    emp = client.post("/employees", json={"name": "Иван"}).json()
    return site["id"], emp["id"]


def open_shift(client, emp, site, start):
    return client.post("/shifts", json={"employee_id": emp, "site_id": site, "start": start})


def work(client, rate_type, rate, start, end):
    site, emp = setup(client, rate_type, rate)
    shift = open_shift(client, emp, site, start).json()
    return client.post(f"/shifts/{shift['id']}/close", json={"end": end})


def test_s1_open_shift(client):
    site, emp = setup(client)
    r = open_shift(client, emp, site, "2026-09-01T08:00:00")
    assert r.status_code == 201
    assert r.json()["status"] == "open"


def test_s2_second_open_shift_rejected(client):
    site, emp = setup(client)
    open_shift(client, emp, site, "2026-09-01T08:00:00")
    r = open_shift(client, emp, site, "2026-09-01T09:00:00")
    assert r.status_code == 409
    assert len(main.store.shifts) == 1


def test_s3_hourly(client):
    r = work(client, "hourly", 400, "2026-09-01T08:00:00", "2026-09-01T16:30:00")
    assert r.json()["pay"] == 3400


def test_s4_fixed_does_not_depend_on_duration(client):
    r = work(client, "fixed", 1800, "2026-09-01T09:00:00", "2026-09-01T14:00:00")
    assert r.json()["pay"] == 1800


def test_s5_long_shift_capped_at_12h(client):
    r = work(client, "hourly", 400, "2026-09-01T06:00:00", "2026-09-01T20:00:00").json()
    assert r["pay"] == 4800
    assert r["long"] is True


def test_s6_end_before_start(client):
    site, emp = setup(client)
    shift = open_shift(client, emp, site, "2026-09-01T10:00:00").json()
    r = client.post(f"/shifts/{shift['id']}/close", json={"end": "2026-09-01T09:00:00"})
    assert r.status_code == 422
    assert main.store.shifts[shift["id"]]["status"] == "open"


def test_s7_close_twice(client):
    site, emp = setup(client)
    shift = open_shift(client, emp, site, "2026-09-01T08:00:00").json()
    client.post(f"/shifts/{shift['id']}/close", json={"end": "2026-09-01T10:00:00"})
    r = client.post(f"/shifts/{shift['id']}/close", json={"end": "2026-09-01T11:00:00"})
    assert r.status_code == 409


def test_s8_payroll_only_closed_in_period(client):
    site, emp = setup(client, "hourly", 400)
    for day in ("01", "02"):
        s = open_shift(client, emp, site, f"2026-09-{day}T08:00:00").json()
        client.post(f"/shifts/{s['id']}/close", json={"end": f"2026-09-{day}T10:00:00"})
    open_shift(client, emp, site, "2026-09-03T08:00:00")
    rows = client.get("/payroll", params={"date_from": "2026-09-01", "date_to": "2026-09-04"}).json()
    assert rows == [{"employee_id": emp, "name": "Иван", "shifts": 2, "total": 1600}]
