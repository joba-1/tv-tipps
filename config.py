from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from dataclasses import dataclass, field


@dataclass
class ReceiverConfig:
    name: str
    ip: str
    default_user: str           # primary IR remote user (low-confidence heuristic only)
    priority: int = 99          # lower = preferred for EPG source selection
    has_genre: bool = False     # True if OWIF firmware provides genre strings
    wol_mac: str | None = None  # if set, WOL wake is supported
    power_method: str = "none"  # "wol" | "intertechno" | "none"
    intertechno_family: str = ""  # RF family letter, e.g. "A"
    intertechno_device: int = 1   # RF device number, e.g. 1
    intertechno_url: str = ""     # per-receiver gateway URL, overrides settings.intertechno_url
    standby_newstate: int = 4     # OpenWebif newstate for light standby (VTi=4, openATV=5)
    epg_wake: bool = False        # wake for the nightly full EPG sweep, then power back down
    location: str = ""            # human-readable room name, e.g. "Wohnzimmer"


@dataclass
class UserConfig:
    slug: str
    name: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Format: name:ip:default_user|key=value|key=value,...
    # Pipe-separated flags avoid colon conflict with MAC addresses.
    # Supported flags: priority=<int>, has_genre=true, wol_mac=<MAC>, power_method=wol|intertechno|none
    receivers_raw: str = ""  # optional bootstrap; manage via Admin UI after first run
    users_raw: str = ""     # optional bootstrap; manage via Admin UI after first run

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:latest"

    poll_interval_sec: int = 45
    min_watch_sec: int = 300
    epg_full_refresh_hour: int = 3
    epg_retention_days: int = 30
    # Nightly zap tour on `epg_wake` receivers: a box woken from mains-off boots
    # with an empty EPG cache and only sees the transponder it is tuned to, so
    # the tour is what makes the sweep worth running. The dwell is adaptive —
    # each transponder is held until its EPG stops growing for FLAT_SEC — because
    # a fixed dwell is wrong in both directions (a 1-channel transponder settles
    # in seconds, an 8-channel one needs much longer). MAX caps a transponder
    # that never settles; MIN guards against calling it before EIT starts.
    epg_wake_dwell_min_sec: int = 15
    epg_wake_dwell_max_sec: int = 120
    epg_wake_dwell_flat_sec: int = 20
    epg_wake_sample_sec: int = 5
    # Hard cap on the whole tour: with every transponder hitting its ceiling the
    # box would stay powered for transponders × MAX. Whatever is not visited by
    # then is left to the next night rather than burning the user's electricity.
    epg_wake_tour_max_sec: int = 1800
    # Viewing sessions older than this are pruned nightly. They stop feeding
    # the taste profile after 30 days anyway and only pin their EPG events
    # against cleanup; keep a generous window for the admin activity view.
    session_retention_days: int = 90
    timezone: str = "Europe/Berlin"

    # Prime-time window (local time, global for all users)
    prime_start_hour: int = 20
    prime_end_hour: int = 23

    # IntertechnoGateway for mains-switched receivers (manual web control only)
    intertechno_url: str = ""     # e.g. http://intertechnogw
    intertechno_family: str = ""  # RF family letter, e.g. "A"
    intertechno_device: int = 1   # RF device number, e.g. 1

    ssh_enabled: bool = False
    mock_receivers: bool = False
    db_path: str = "tv_tipps.db"
    log_level: str = "INFO"

    @property
    def receivers(self) -> list[ReceiverConfig]:
        result = []
        for entry in self.receivers_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split("|")
            base = parts[0].split(":")
            if len(base) < 3:
                continue
            rcfg = ReceiverConfig(name=base[0], ip=base[1], default_user=base[2])
            for flag in parts[1:]:
                if "=" not in flag:
                    continue
                k, v = flag.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k == "priority":
                    try:
                        rcfg.priority = int(v)
                    except ValueError:
                        pass  # leave default
                elif k == "has_genre":
                    rcfg.has_genre = v.lower() in ("true", "1", "yes")
                elif k == "wol_mac":
                    rcfg.wol_mac = v
                elif k == "power_method":
                    rcfg.power_method = v.lower()
                elif k == "location":
                    rcfg.location = v
            result.append(rcfg)
        return result

    @property
    def receivers_by_priority(self) -> list[ReceiverConfig]:
        return sorted(self.receivers, key=lambda r: r.priority)

    @property
    def users(self) -> list[UserConfig]:
        result = []
        for entry in self.users_raw.split(","):
            parts = entry.strip().split(":")
            if len(parts) == 2:
                result.append(UserConfig(slug=parts[0], name=parts[1]))
        return result


settings = Settings()
