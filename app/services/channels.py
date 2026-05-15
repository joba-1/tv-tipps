"""Fetch and cache bouquet/channel data from receivers."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.models import Receiver, Channel, ChannelAvailability, Bouquet, BouquetChannel
from app.enigma.client import EnigmaClient
from app.enigma.parser import parse_all_services
from app.logging_setup import get_logger

log = get_logger(__name__)


async def refresh_channels(receiver: Receiver, client: EnigmaClient, db: Session) -> int:
    """Fetch full service list from receiver, upsert into DB. Returns channel count."""
    raw = await client.get_all_services()
    if raw is None:
        log.warning("channels.fetch_failed", receiver=receiver.name)
        return 0

    bouquets = parse_all_services(raw)
    now = datetime.utcnow()
    total_channels = 0

    for pb in bouquets:
        # Upsert bouquet
        stmt = (
            sqlite_insert(Bouquet)
            .values(receiver_id=receiver.id, bref=pb.bref, name=pb.name, position=pb.position, last_seen=now)
            .on_conflict_do_update(
                index_elements=["receiver_id", "bref"],
                set_={"name": pb.name, "position": pb.position, "last_seen": now},
            )
        )
        db.execute(stmt)
        db.flush()
        bouquet_obj = db.query(Bouquet).filter_by(receiver_id=receiver.id, bref=pb.bref).one()

        for pc in pb.channels:
            # Upsert canonical channel
            stmt = (
                sqlite_insert(Channel)
                .values(sref=pc.sref, name=pc.name, picon_path=pc.picon_path, last_seen=now)
                .on_conflict_do_update(
                    index_elements=["sref"],
                    set_={"name": pc.name, "last_seen": now},
                )
            )
            db.execute(stmt)
            db.flush()
            channel_obj = db.query(Channel).filter_by(sref=pc.sref).one()

            # Upsert receiver availability
            stmt = (
                sqlite_insert(ChannelAvailability)
                .values(channel_id=channel_obj.id, receiver_id=receiver.id)
                .on_conflict_do_nothing()
            )
            db.execute(stmt)

            # Upsert bouquet membership
            stmt = (
                sqlite_insert(BouquetChannel)
                .values(bouquet_id=bouquet_obj.id, channel_id=channel_obj.id, position=pc.position)
                .on_conflict_do_update(
                    index_elements=["bouquet_id", "channel_id"],
                    set_={"position": pc.position},
                )
            )
            db.execute(stmt)
            total_channels += 1

        receiver.last_seen = now

    db.commit()
    log.info("channels.refreshed", receiver=receiver.name, bouquets=len(bouquets), channels=total_channels)
    return total_channels


def get_default_bouquet_ids(db: Session) -> list[int]:
    """Return IDs of 'Favourites'-type bouquets across all receivers, as the cold-start default."""
    keywords = ("favorit", "favourite", "favorite")
    bouquets = db.query(Bouquet).all()
    ids = [b.id for b in bouquets if any(kw in b.name.lower() for kw in keywords)]
    if not ids:
        # Fall back to position=0 bouquet per receiver
        ids = [b.id for b in bouquets if b.position == 0]
    return ids or [b.id for b in bouquets[:1]]


def get_channels_for_user(user_id: int | None, db: Session) -> list[Channel]:
    """Return channels visible to a user based on their bouquet/channel filters.
    If user_id is None or user has no saved preferences, defaults to Favourites bouquets.
    """
    from app.models import UserBouquetFilter, UserChannelFilter

    has_filters = False
    if user_id is not None:
        has_filters = db.query(UserBouquetFilter).filter_by(user_id=user_id).count() > 0

    if not has_filters:
        # New user or no-user: show Favourites bouquets only
        default_bouquet_ids = get_default_bouquet_ids(db)
        channel_ids_list = [
            bc.channel_id for bc in
            db.query(BouquetChannel.channel_id)
            .filter(BouquetChannel.bouquet_id.in_(default_bouquet_ids))
            .all()
        ]
        return db.query(Channel).filter(Channel.id.in_(channel_ids_list)).order_by(Channel.name).all()

    enabled_bouquet_ids = (
        db.query(UserBouquetFilter.bouquet_id)
        .filter(UserBouquetFilter.user_id == user_id, UserBouquetFilter.enabled.is_(True))
        .subquery()
    )

    channel_ids_in_enabled = (
        db.query(BouquetChannel.channel_id)
        .filter(BouquetChannel.bouquet_id.in_(enabled_bouquet_ids))
        .subquery()
    )

    excluded_channel_ids = (
        db.query(UserChannelFilter.channel_id)
        .filter(UserChannelFilter.user_id == user_id, UserChannelFilter.excluded.is_(True))
        .subquery()
    )

    has_exclusions = db.query(UserChannelFilter).filter(
        UserChannelFilter.user_id == user_id, UserChannelFilter.excluded.is_(True)
    ).count() > 0

    query = db.query(Channel).filter(Channel.id.in_(channel_ids_in_enabled))
    if has_exclusions:
        query = query.filter(Channel.id.notin_(excluded_channel_ids))

    return query.order_by(Channel.name).all()
