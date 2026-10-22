"""Плата за стоянку (SPEC.md, R2–R5)."""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

MAX_PAID_MINUTES = 12 * 60


def parking_fee(tariff: str, rate: int, start: datetime, end: datetime) -> tuple[int, bool]:
    """Возвращает (плата в рублях, long)."""
    if end <= start:
        raise ValueError("выезд должен быть позже въезда")
    minutes = int((end - start).total_seconds() // 60)
    long = minutes > MAX_PAID_MINUTES
    if tariff == "fixed":
        return rate, long
    if tariff == "hourly":
        paid = min(minutes, MAX_PAID_MINUTES)
        amount = (Decimal(rate) * paid / 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(amount), long
    raise ValueError(f"неизвестный тариф: {tariff}")
