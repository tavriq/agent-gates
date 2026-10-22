"""Сценарии приёмки S1–S8 из SPEC.md."""
import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client():
    main.reset_store()
    return TestClient(main.app)


def setup(client, tariff="hourly", rate=400):
    lot = client.post("/lots", json={"name": "У вокзала", "tariff": tariff, "rate": rate}).json()
    car = client.post("/cars", json={"plate": "А123ВС77"}).json()
    return lot["id"], car["id"]


def open_session(client, car, lot, start):
    return client.post("/sessions", json={"car_id": car, "lot_id": lot, "start": start})


def park(client, tariff, rate, start, end):
    lot, car = setup(client, tariff, rate)
    session = open_session(client, car, lot, start).json()
    return client.post(f"/sessions/{session['id']}/close", json={"end": end})


def test_s1_open_session(client):
    lot, car = setup(client)
    r = open_session(client, car, lot, "2026-09-01T08:00:00")
    assert r.status_code == 201
    assert r.json()["status"] == "open"


def test_s2_second_open_session_rejected(client):
    lot, car = setup(client)
    open_session(client, car, lot, "2026-09-01T08:00:00")
    r = open_session(client, car, lot, "2026-09-01T09:00:00")
    assert r.status_code == 409
    assert len(main.store.sessions) == 1


def test_s3_hourly(client):
    r = park(client, "hourly", 400, "2026-09-01T08:00:00", "2026-09-01T16:30:00")
    assert r.json()["fee"] == 3400


def test_s4_fixed_does_not_depend_on_duration(client):
    r = park(client, "fixed", 1800, "2026-09-01T09:00:00", "2026-09-01T10:00:00")
    assert r.json()["fee"] == 1800


def test_s5_long_session_capped_at_12h(client):
    r = park(client, "hourly", 400, "2026-09-01T06:00:00", "2026-09-01T20:00:00").json()
    assert r["fee"] == 4800
    assert r["long"] is True


def test_s6_exit_before_entry(client):
    lot, car = setup(client)
    session = open_session(client, car, lot, "2026-09-01T10:00:00").json()
    r = client.post(f"/sessions/{session['id']}/close", json={"end": "2026-09-01T09:00:00"})
    assert r.status_code == 422
    assert main.store.sessions[session["id"]]["status"] == "open"


def test_s7_close_twice(client):
    lot, car = setup(client)
    session = open_session(client, car, lot, "2026-09-01T08:00:00").json()
    client.post(f"/sessions/{session['id']}/close", json={"end": "2026-09-01T10:00:00"})
    r = client.post(f"/sessions/{session['id']}/close", json={"end": "2026-09-01T11:00:00"})
    assert r.status_code == 409


def test_s8_statement_only_closed_in_period(client):
    lot, car = setup(client, "hourly", 400)
    for day in ("01", "02"):
        s = open_session(client, car, lot, f"2026-09-{day}T08:00:00").json()
        client.post(f"/sessions/{s['id']}/close", json={"end": f"2026-09-{day}T10:00:00"})
    open_session(client, car, lot, "2026-09-03T08:00:00")
    rows = client.get("/statement", params={"date_from": "2026-09-01", "date_to": "2026-09-04"}).json()
    assert rows == [{"car_id": car, "plate": "А123ВС77", "sessions": 2, "total": 1600}]
