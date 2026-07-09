"""SQLAlchemy models for Sweat Box."""
import os
from datetime import datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Time,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


def _c_to_f(c: int) -> int:
    return round(c * 9 / 5 + 32)

DB_PATH = os.environ.get("DB_PATH")
if not DB_PATH:
    # Fall back to local dir if /data doesn't exist (local dev)
    DB_PATH = "/data/sweatbox.db" if os.path.exists("/data") else os.path.join(os.path.dirname(__file__), "sweatbox.db")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class FamilyMember(Base):
    __tablename__ = "family_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    pin_hash = Column(String, nullable=True)
    email = Column(String, nullable=True, unique=True)
    password_hash = Column(String, nullable=True)
    reset_token = Column(String, nullable=True)
    reset_token_expires = Column(DateTime, nullable=True)
    # pending | approved | rejected
    status = Column(String, default="pending")
    is_admin = Column(Integer, default=0)
    default_temp = Column(Integer, default=90)   # °C
    default_time = Column(Integer, default=60)   # minutes
    max_temp = Column(Integer, nullable=True)     # °C — admin-set limit; None = no limit
    color = Column(String, default="#F97316")    # hex color for calendar display
    # JSON text: {"preheat": true, "signup": true, "booking": true} — null = all defaults on
    notification_prefs = Column(Text, nullable=True)
    live_activity_all_sessions = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    bookings = relationship("Booking", back_populates="member", cascade="all, delete-orphan")

    def get_notification_prefs(self) -> dict:
        import json as _json
        try:
            return _json.loads(self.notification_prefs) if self.notification_prefs else {}
        except Exception:
            return {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "status": self.status,
            "is_admin": bool(self.is_admin),
            "default_temp": self.default_temp,
            "default_time": self.default_time,
            "max_temp": self.max_temp,
            "color": self.color,
            "notification_prefs": self.get_notification_prefs(),
            "live_activity_all_sessions": bool(self.live_activity_all_sessions),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_public_dict(self) -> dict:
        """Dict safe to return to unauthenticated callers (login screen / migration picker)."""
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "default_temp": self.default_temp,
            "default_time": self.default_time,
            "max_temp": self.max_temp,
            "has_credentials": self.email is not None,
        }


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    member_id = Column(Integer, ForeignKey("family_members.id"), nullable=False)
    date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    target_temp = Column(Integer)   # °C
    on_time = Column(Integer)       # minutes
    # scheduled | preheating | active | completed | cancelled
    status = Column(String, default="scheduled")
    session_ending_notified_at = Column(DateTime, nullable=True)
    share_token = Column(String, nullable=True)  # public RSVP link token (unique when set)
    created_at = Column(DateTime, default=datetime.utcnow)

    member = relationship("FamilyMember", back_populates="bookings")
    participants = relationship("BookingParticipant", back_populates="booking", cascade="all, delete-orphan")
    invites = relationship("BookingInvite", back_populates="booking", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "member_id": self.member_id,
            "member_name": self.member.name if self.member else None,
            "member_color": self.member.color if self.member else None,
            "date": self.date.isoformat(),
            "start_time": self.start_time.strftime("%H:%M"),
            "end_time": self.end_time.strftime("%H:%M"),
            "target_temp": self.target_temp,
            "on_time": self.on_time,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BookingParticipant(Base):
    __tablename__ = "booking_participants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    member_id = Column(Integer, ForeignKey("family_members.id", ondelete="CASCADE"), nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    booking = relationship("Booking", back_populates="participants")
    member = relationship("FamilyMember")

    __table_args__ = (
        UniqueConstraint("booking_id", "member_id", name="uq_booking_participant_member"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "booking_id": self.booking_id,
            "member_id": self.member_id,
            "member_name": self.member.name if self.member else None,
            "member_color": self.member.color if self.member else None,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
        }


class BookingInvite(Base):
    """An invitation for a member to join a booking.

    Deliberately separate from BookingParticipant: participants confer control
    rights, so a pending/declined invite must not create a participant row.
    A "yes" RSVP additionally creates the BookingParticipant.
    """
    __tablename__ = "booking_invites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    member_id = Column(Integer, ForeignKey("family_members.id", ondelete="CASCADE"), nullable=False)
    invited_by = Column(Integer, ForeignKey("family_members.id", ondelete="SET NULL"), nullable=True)
    # invited | yes | no | maybe
    status = Column(String, default="invited")
    invited_at = Column(DateTime, nullable=True)
    responded_at = Column(DateTime, nullable=True)

    booking = relationship("Booking", back_populates="invites")
    member = relationship("FamilyMember", foreign_keys=[member_id])

    __table_args__ = (
        UniqueConstraint("booking_id", "member_id", name="uq_booking_invite_member"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "booking_id": self.booking_id,
            "member_id": self.member_id,
            "member_name": self.member.name if self.member else None,
            "member_color": self.member.color if self.member else None,
            "status": self.status,
            "invited_at": self.invited_at.isoformat() if self.invited_at else None,
            "responded_at": self.responded_at.isoformat() if self.responded_at else None,
        }


class Preset(Base):
    __tablename__ = "presets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)   # slug: quick, hot, …
    label = Column(String, nullable=False)               # display name
    target_temp = Column(Integer, nullable=False)        # °C
    on_time = Column(Integer, nullable=False)            # minutes
    steam_en = Column(Integer, default=0)
    target_rh = Column(Integer, nullable=True)           # % humidity (steam only)
    sort_order = Column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "target_temp": self.target_temp,
            "target_temp_f": _c_to_f(self.target_temp),
            "on_time": self.on_time,
            "steam_en": self.steam_en,
            "target_rh": self.target_rh,
        }


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    member_id = Column(Integer, ForeignKey("family_members.id", ondelete="CASCADE"), nullable=False)
    endpoint = Column(Text, unique=True, nullable=False)
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class NativeDevice(Base):
    __tablename__ = "native_devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    member_id = Column(Integer, ForeignKey("family_members.id", ondelete="CASCADE"), nullable=False)
    native_device_id = Column(String, unique=True, nullable=False)
    apns_token = Column(Text, nullable=True)
    # "sandbox" (dev/Xcode) or "production" (TestFlight/App Store). Set from
    # the client at token-registration time — sandbox and production tokens
    # come from different APNs infrastructures and are not interchangeable.
    apns_environment = Column(String, default="production")
    platform = Column(String, default="ios")
    app_version = Column(String, nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


class LiveActivityPushToStartToken(Base):
    __tablename__ = "live_activity_push_to_start_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    member_id = Column(Integer, ForeignKey("family_members.id", ondelete="CASCADE"), nullable=False)
    native_device_id = Column(String, ForeignKey("native_devices.native_device_id", ondelete="CASCADE"), nullable=False)
    token = Column(Text, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("native_device_id", name="uq_live_activity_push_to_start_native_device"),
    )


class LiveActivityToken(Base):
    __tablename__ = "live_activity_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    member_id = Column(Integer, ForeignKey("family_members.id", ondelete="CASCADE"), nullable=False)
    native_device_id = Column(String, ForeignKey("native_devices.native_device_id", ondelete="CASCADE"), nullable=False)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=True)
    token = Column(Text, unique=True, nullable=False)
    activity_state = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ControlLog(Base):
    __tablename__ = "control_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    member_id = Column(Integer, ForeignKey("family_members.id", ondelete="SET NULL"), nullable=True)
    member_name = Column(String, nullable=True)   # denormalised — persists after member deletion
    # on | off | set | preset
    action = Column(String, nullable=False)
    target_temp = Column(Integer, nullable=True)  # °C
    on_time = Column(Integer, nullable=True)       # minutes
    preset_name = Column(String, nullable=True)
    notes = Column(String, nullable=True)          # JSON-encoded extra fields (light, fan, steamEn…)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "member_id": self.member_id,
            "member_name": self.member_name,
            "action": self.action,
            "target_temp": self.target_temp,
            "target_temp_f": _c_to_f(self.target_temp) if self.target_temp is not None else None,
            "on_time": self.on_time,
            "preset_name": self.preset_name,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GuestRsvp(Base):
    """A non-member RSVP made through a booking's public share link."""
    __tablename__ = "guest_rsvps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(Integer, ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    # yes | no | maybe
    status = Column(String, nullable=False)
    # Per-guest secret so a guest can change their answer without an account
    guest_secret = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
        }


class DeviceStateLog(Base):
    """Per-minute device telemetry while the heater is running — feeds preheat estimates."""
    __tablename__ = "device_state_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Naive local time (app_now()) — segment math must line up with booking/preheat logic
    ts = Column(DateTime, nullable=False, index=True)
    temperature = Column(Integer, nullable=True)     # °C current
    target_temp = Column(Integer, nullable=True)     # °C
    active = Column(Integer, nullable=True)          # heater session on/off
    heat_on = Column(Integer, nullable=True)         # heating element actually drawing
    remaining_time = Column(Integer, nullable=True)  # minutes
    outdoor_temp = Column(Float, nullable=True)      # °C from Open-Meteo

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "temperature": self.temperature,
            "target_temp": self.target_temp,
            "active": self.active,
            "heat_on": self.heat_on,
            "remaining_time": self.remaining_time,
            "outdoor_temp": self.outdoor_temp,
        }


def _migrate_db():
    """Add columns/tables that SQLAlchemy's create_all won't add to existing tables."""
    migrations = [
        "ALTER TABLE family_members ADD COLUMN max_temp INTEGER",
        "ALTER TABLE family_members ADD COLUMN notification_prefs TEXT",
        "ALTER TABLE bookings ADD COLUMN session_ending_notified_at DATETIME",
        "ALTER TABLE family_members ADD COLUMN email TEXT",
        "ALTER TABLE family_members ADD COLUMN password_hash TEXT",
        "ALTER TABLE family_members ADD COLUMN reset_token TEXT",
        "ALTER TABLE family_members ADD COLUMN reset_token_expires DATETIME",
        "ALTER TABLE family_members ADD COLUMN live_activity_all_sessions INTEGER DEFAULT 0",
        "ALTER TABLE native_devices ADD COLUMN apns_environment TEXT DEFAULT 'production'",
        "ALTER TABLE bookings ADD COLUMN share_token TEXT",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # Column/change already applied — safe to ignore
        # Partial unique indexes — enforce uniqueness only on non-NULL values (SQLite compatible)
        for index_stmt in (
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_fm_email ON family_members (email) WHERE email IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_booking_share_token ON bookings (share_token) WHERE share_token IS NOT NULL",
        ):
            try:
                conn.execute(text(index_stmt))
                conn.commit()
            except Exception:
                pass


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_db()
