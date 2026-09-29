"""HTTP API: объекты, сотрудники, смены, ведомость. Хранение — в памяти процесса."""
from datetime import date, datetime
from itertools import count
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.pay import shift_pay

app = FastAPI(title="agent-gates: смены и оплата")


class Store:
    def __init__(self) -> None:
        self.sites: dict[int, dict] = {}
        self.employees: dict[int, dict] = {}
        self.shifts: dict[int, dict] = {}
        self.ids = count(1)


store = Store()


def reset_store() -> None:
    global store
    store = Store()


class SiteIn(BaseModel):
    name: str
    rate_type: Literal["hourly", "fixed"]
    rate: int = Field(gt=0)


class EmployeeIn(BaseModel):
    name: str


class ShiftOpen(BaseModel):
    employee_id: int
    site_id: int
    start: datetime


class ShiftClose(BaseModel):
    end: datetime


@app.post("/sites", status_code=201)
def create_site(body: SiteIn) -> dict:
    site = {"id": next(store.ids), **body.model_dump()}
    store.sites[site["id"]] = site
    return site


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict:
    employee = {"id": next(store.ids), **body.model_dump()}
    store.employees[employee["id"]] = employee
    return employee


@app.post("/shifts", status_code=201)
def open_shift(body: ShiftOpen) -> dict:
    if body.employee_id not in store.employees:
        raise HTTPException(404, "сотрудник не найден")
    if body.site_id not in store.sites:
        raise HTTPException(404, "объект не найден")
    if any(s["employee_id"] == body.employee_id and s["status"] == "open" for s in store.shifts.values()):
        raise HTTPException(409, "у сотрудника уже есть открытая смена")
    shift = {
        "id": next(store.ids),
        **body.model_dump(),
        "end": None,
        "status": "open",
        "pay": None,
        "long": False,
    }
    store.shifts[shift["id"]] = shift
    return shift


@app.post("/shifts/{shift_id}/close")
def close_shift(shift_id: int, body: ShiftClose) -> dict:
    shift = store.shifts.get(shift_id)
    if shift is None:
        raise HTTPException(404, "смена не найдена")
    if shift["status"] == "closed":
        raise HTTPException(409, "смена уже закрыта")
    site = store.sites[shift["site_id"]]
    try:
        pay, long = shift_pay(site["rate_type"], site["rate"], shift["start"], body.end)
    except ValueError as e:
        raise HTTPException(422, str(e))
    shift.update(end=body.end, status="closed", pay=pay, long=long)
    return shift


@app.get("/payroll")
def payroll(date_from: date, date_to: date) -> list[dict]:
    rows: dict[int, dict] = {}
    for s in store.shifts.values():
        if s["status"] != "closed" or not (date_from <= s["start"].date() < date_to):
            continue
        row = rows.setdefault(
            s["employee_id"],
            {"employee_id": s["employee_id"], "name": store.employees[s["employee_id"]]["name"], "shifts": 0, "total": 0},
        )
        row["shifts"] += 1
        row["total"] += s["pay"]
    return sorted(rows.values(), key=lambda r: r["employee_id"])
