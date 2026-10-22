"""HTTP API: парковки, машины, стоянки, выписка. Хранение — в памяти процесса."""
from datetime import date, datetime
from itertools import count
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.fee import parking_fee

app = FastAPI(title="agent-gates: платная парковка")


class Store:
    def __init__(self) -> None:
        self.lots: dict[int, dict] = {}
        self.cars: dict[int, dict] = {}
        self.sessions: dict[int, dict] = {}
        self.ids = count(1)


store = Store()


def reset_store() -> None:
    global store
    store = Store()


class LotIn(BaseModel):
    name: str
    tariff: Literal["hourly", "fixed"]
    rate: int = Field(gt=0)


class CarIn(BaseModel):
    plate: str


class SessionOpen(BaseModel):
    car_id: int
    lot_id: int
    start: datetime


class SessionClose(BaseModel):
    end: datetime


@app.post("/lots", status_code=201)
def create_lot(body: LotIn) -> dict:
    lot = {"id": next(store.ids), **body.model_dump()}
    store.lots[lot["id"]] = lot
    return lot


@app.post("/cars", status_code=201)
def create_car(body: CarIn) -> dict:
    car = {"id": next(store.ids), **body.model_dump()}
    store.cars[car["id"]] = car
    return car


@app.post("/sessions", status_code=201)
def open_session(body: SessionOpen) -> dict:
    if body.car_id not in store.cars:
        raise HTTPException(404, "машина не найдена")
    if body.lot_id not in store.lots:
        raise HTTPException(404, "парковка не найдена")
    if any(s["car_id"] == body.car_id and s["status"] == "open" for s in store.sessions.values()):
        raise HTTPException(409, "у машины уже есть открытая стоянка")
    session = {
        "id": next(store.ids),
        **body.model_dump(),
        "end": None,
        "status": "open",
        "fee": None,
        "long": False,
    }
    store.sessions[session["id"]] = session
    return session


@app.post("/sessions/{session_id}/close")
def close_session(session_id: int, body: SessionClose) -> dict:
    session = store.sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "стоянка не найдена")
    if session["status"] == "closed":
        raise HTTPException(409, "стоянка уже закрыта")
    lot = store.lots[session["lot_id"]]
    try:
        fee, long = parking_fee(lot["tariff"], lot["rate"], session["start"], body.end)
    except ValueError as e:
        raise HTTPException(422, str(e))
    session.update(end=body.end, status="closed", fee=fee, long=long)
    return session


@app.get("/statement")
def statement(date_from: date, date_to: date) -> list[dict]:
    rows: dict[int, dict] = {}
    for s in store.sessions.values():
        if s["status"] != "closed" or not (date_from <= s["start"].date() < date_to):
            continue
        row = rows.setdefault(
            s["car_id"],
            {"car_id": s["car_id"], "plate": store.cars[s["car_id"]]["plate"], "sessions": 0, "total": 0},
        )
        row["sessions"] += 1
        row["total"] += s["fee"]
    return sorted(rows.values(), key=lambda r: r["car_id"])
