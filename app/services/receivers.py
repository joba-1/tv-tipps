"""DB-backed receiver config: replaces settings.receivers throughout the app."""
from __future__ import annotations
from sqlalchemy.orm import Session
from app.models import Receiver
from config import ReceiverConfig


def get_receiver_configs(db: Session) -> list[ReceiverConfig]:
    """Return all receivers from DB as ReceiverConfig objects, sorted by priority."""
    rows = db.query(Receiver).order_by(Receiver.priority, Receiver.name).all()
    return [_to_rcfg(r) for r in rows]


def get_receiver_config(name: str, db: Session) -> ReceiverConfig | None:
    r = db.query(Receiver).filter_by(name=name).first()
    return _to_rcfg(r) if r else None


def _to_rcfg(r: Receiver) -> ReceiverConfig:
    return ReceiverConfig(
        name=r.name,
        ip=r.ip,
        default_user=r.default_user or "",
        priority=r.priority if r.priority is not None else 99,
        has_genre=bool(r.has_genre),
        wol_mac=r.wol_mac,
        power_method=r.power_method or "none",
        intertechno_family=r.intertechno_family or "",
        intertechno_device=r.intertechno_device if r.intertechno_device is not None else 1,
        intertechno_url=r.intertechno_url or "",
        location=r.location or "",
    )
