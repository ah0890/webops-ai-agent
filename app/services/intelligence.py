"""Price intelligence computed from *trusted* historical observations only."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from app.database.models import TRUSTED_STATUSES, Observation, utcnow


@dataclass
class PricePoint:
    observed_at: datetime
    price: float | None
    currency: str | None
    availability: str


@dataclass
class ProductStats:
    observations: int = 0
    current_price: float | None = None
    previous_price: float | None = None
    currency: str | None = None
    absolute_change: float | None = None
    percentage_change: float | None = None
    change_7d_pct: float | None = None
    change_30d_pct: float | None = None
    lowest_price: float | None = None
    highest_price: float | None = None
    average_price: float | None = None
    trend: str = "insufficient_data"  # up | down | flat | insufficient_data
    availability: str | None = None
    availability_changes: int = 0
    last_observed_at: datetime | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["last_observed_at"] = self.last_observed_at.isoformat() if self.last_observed_at else None
        return d


def pct_change(current: float | None, previous: float | None) -> float | None:
    """((current - previous) / previous) × 100, or None when undefined."""
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 4)


def _change_since(points: list[PricePoint], days: int, now: datetime) -> float | None:
    """Change vs the latest observation that is at least `days` old. None if history is too short."""
    cutoff = now - timedelta(days=days)
    ref = next((p for p in reversed(points) if p.observed_at <= cutoff and p.price is not None), None)
    cur = next((p for p in reversed(points) if p.price is not None), None)
    if ref is None or cur is None:
        return None
    return pct_change(cur.price, ref.price)


def _trend(prices: list[float], flat_band_pct: float = 1.0) -> str:
    if len(prices) < 3:
        return "insufficient_data"
    n = len(prices)
    xs = range(n)
    mx, my = (n - 1) / 2, sum(prices) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, prices, strict=True)) / denom
    total_move_pct = slope * (n - 1) / my * 100 if my else 0
    if abs(total_move_pct) < flat_band_pct:
        return "flat"
    return "up" if total_move_pct > 0 else "down"


def compute_stats(points: list[PricePoint], now: datetime | None = None) -> ProductStats:
    """`points` must be chronological and already filtered to trusted observations."""
    now = now or utcnow()
    stats = ProductStats(observations=len(points))
    if not points:
        return stats
    priced = [p for p in points if p.price is not None]
    last = points[-1]
    stats.last_observed_at = last.observed_at
    stats.availability = last.availability
    stats.availability_changes = sum(
        1 for a, b in zip(points, points[1:], strict=False)
        if a.availability != b.availability and "unknown" not in (a.availability, b.availability)
    )
    if not priced:
        return stats
    stats.current_price = priced[-1].price
    stats.currency = priced[-1].currency
    if len(priced) >= 2:
        stats.previous_price = priced[-2].price
        stats.absolute_change = round(priced[-1].price - priced[-2].price, 4)
        stats.percentage_change = pct_change(priced[-1].price, priced[-2].price)
    values = [p.price for p in priced]
    stats.lowest_price, stats.highest_price = min(values), max(values)
    stats.average_price = round(sum(values) / len(values), 4)
    stats.change_7d_pct = _change_since(priced, 7, now)
    stats.change_30d_pct = _change_since(priced, 30, now)
    stats.trend = _trend(values[-10:])
    return stats


def trusted_points(session: Session, product_id: int, until_id: int | None = None) -> list[PricePoint]:
    q = select(Observation).where(
        Observation.product_id == product_id, col(Observation.extraction_status).in_(TRUSTED_STATUSES)
    )
    if until_id is not None:
        q = q.where(Observation.id <= until_id)
    rows = session.exec(q.order_by(Observation.observed_at, Observation.id)).all()
    return [PricePoint(r.observed_at, r.price, r.currency, r.availability) for r in rows]


def previous_trusted(session: Session, product_id: int, exclude_run_id: int | None = None) -> Observation | None:
    q = select(Observation).where(
        Observation.product_id == product_id, col(Observation.extraction_status).in_(TRUSTED_STATUSES)
    )
    if exclude_run_id is not None:
        q = q.where(Observation.run_id != exclude_run_id)
    return session.exec(q.order_by(col(Observation.observed_at).desc(), col(Observation.id).desc())).first()
