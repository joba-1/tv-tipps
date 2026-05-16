from datetime import datetime
from sqlalchemy import (
    Integer, String, Text, Float, Boolean,
    ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Receiver(Base):
    __tablename__ = "receivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=99, server_default="99")
    power_method: Mapped[str] = mapped_column(String(16), nullable=False, default="none", server_default="none")
    wol_mac: Mapped[str | None] = mapped_column(String(32))
    intertechno_family: Mapped[str] = mapped_column(String(4), nullable=False, default="", server_default="")
    intertechno_device: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    has_genre: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    default_user: Mapped[str | None] = mapped_column(String(64))
    power_state: Mapped[str | None] = mapped_column(String(32))  # on|standby|deep_standby|unknown
    last_seen: Mapped[datetime | None] = mapped_column()

    availability: Mapped[list["ChannelAvailability"]] = relationship(back_populates="receiver")
    bouquets: Mapped[list["Bouquet"]] = relationship(back_populates="receiver")


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sref: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    picon_path: Mapped[str | None] = mapped_column(String(256))
    last_seen: Mapped[datetime | None] = mapped_column()

    availability: Mapped[list["ChannelAvailability"]] = relationship(back_populates="channel")
    epg_events: Mapped[list["EpgEvent"]] = relationship(back_populates="channel")
    bouquet_channels: Mapped[list["BouquetChannel"]] = relationship(back_populates="channel")


class ChannelAvailability(Base):
    __tablename__ = "channel_availability"

    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("receivers.id"), primary_key=True)

    channel: Mapped["Channel"] = relationship(back_populates="availability")
    receiver: Mapped["Receiver"] = relationship(back_populates="availability")


class Bouquet(Base):
    __tablename__ = "bouquets"
    __table_args__ = (UniqueConstraint("receiver_id", "bref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("receivers.id"), nullable=False)
    bref: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    position: Mapped[int | None] = mapped_column(Integer)
    last_seen: Mapped[datetime | None] = mapped_column()

    receiver: Mapped["Receiver"] = relationship(back_populates="bouquets")
    bouquet_channels: Mapped[list["BouquetChannel"]] = relationship(back_populates="bouquet")


class BouquetChannel(Base):
    __tablename__ = "bouquet_channels"

    bouquet_id: Mapped[int] = mapped_column(ForeignKey("bouquets.id"), primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    position: Mapped[int | None] = mapped_column(Integer)

    bouquet: Mapped["Bouquet"] = relationship(back_populates="bouquet_channels")
    channel: Mapped["Channel"] = relationship(back_populates="bouquet_channels")


class EpgEvent(Base):
    __tablename__ = "epg_events"
    __table_args__ = (
        UniqueConstraint("channel_id", "start_time"),
        Index("idx_epg_time", "start_time", "end_time"),
        Index("idx_epg_channel_time", "channel_id", "start_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    event_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    short_desc: Mapped[str | None] = mapped_column(Text)
    long_desc: Mapped[str | None] = mapped_column(Text)
    start_time: Mapped[datetime] = mapped_column(nullable=False)
    end_time: Mapped[datetime] = mapped_column(nullable=False)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    genre: Mapped[str | None] = mapped_column(String(64))  # text-derived, no DVB codes in OpenWebif
    cached_at: Mapped[datetime] = mapped_column(nullable=False)

    channel: Mapped["Channel"] = relationship(back_populates="epg_events")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class UserBouquetFilter(Base):
    __tablename__ = "user_bouquet_filters"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    bouquet_id: Mapped[int] = mapped_column(ForeignKey("bouquets.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserChannelFilter(Base):
    __tablename__ = "user_channel_filters"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ViewingSession(Base):
    __tablename__ = "viewing_sessions"
    __table_args__ = (
        Index("idx_sessions_user_time", "user_id", "started_at"),
        Index("idx_sessions_channel_time", "channel_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("receivers.id"), nullable=False)
    epg_event_id: Mapped[int | None] = mapped_column(ForeignKey("epg_events.id"))
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column()
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # ir_remote|webapp_zap|rc_page
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    attribution_method: Mapped[str | None] = mapped_column(String(32))


class RcEvent(Base):
    __tablename__ = "rc_events"
    __table_args__ = (Index("idx_rc_events", "user_id", "pressed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    receiver_id: Mapped[int] = mapped_column(ForeignKey("receivers.id"), nullable=False)
    key_name: Mapped[str] = mapped_column(String(32), nullable=False)
    pressed_at: Mapped[datetime] = mapped_column(nullable=False)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)


class RecommendationCache(Base):
    __tablename__ = "recommendation_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(nullable=False)
    valid_until: Mapped[datetime] = mapped_column(nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)


class UserLike(Base):
    __tablename__ = "user_likes"
    __table_args__ = (UniqueConstraint("user_id", "epg_event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    epg_event_id: Mapped[int | None] = mapped_column(ForeignKey("epg_events.id"))
    # Snapshot fields so a like survives EPG cleanup
    title: Mapped[str] = mapped_column(Text, nullable=False)
    channel_name: Mapped[str | None] = mapped_column(String(256))
    genre: Mapped[str | None] = mapped_column(String(64))
    sentiment: Mapped[str] = mapped_column(String(8), nullable=False, default="like", server_default="like")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class Translation(Base):
    __tablename__ = "translations"

    lang: Mapped[str] = mapped_column(String(8), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # "manual" | "ai"
    generated_at: Mapped[datetime] = mapped_column(nullable=False)
