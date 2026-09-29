"""Расчёт оплаты смены (SPEC.md, R2–R5)."""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

MAX_PAID_MINUTES = 12 * 60


def shift_pay(rate_type: str, rate: int, start: datetime, end: datetime) -> tuple[int, bool]:
    """Возвращает (оплата в рублях, long)."""
    if end <= start:
        raise ValueError("конец смены должен быть позже начала")
    minutes = int((end - start).total_seconds() // 60)
    long = minutes > MAX_PAID_MINUTES
    if rate_type == "fixed":
        return rate, long
    if rate_type == "hourly":
        paid = min(minutes, MAX_PAID_MINUTES)
        amount = (Decimal(rate) * paid / 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(amount), long
    raise ValueError(f"неизвестный тип ставки: {rate_type}")
