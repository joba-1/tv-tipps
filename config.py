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
    # The floor, not the flat window, is what protects against an early plateau:
    # a transponder whose first sample lands before the schedule tables arrive
    # can read flat twice and exit with only present/following data. Several
    # transponders did exactly that on 2026-08-09 (exit at the 15 s floor with
    # saturated_after_sec=5, 5-8x fewer events than the night before), so the
    # floor now outlasts that window.
    epg_wake_dwell_min_sec: int = 25
    epg_wake_dwell_max_sec: int = 120
    # Measured 2026-08-08: across 19 transponders, growth never once paused for a
    # sample and resumed — the largest gap between two growth samples was 7 s, so
    # two flat samples is slack over anything the box has actually done. Same-day
    # EPG does not depend on this window: DVB repeats the first day's schedule at
    # ≤10 s (ETSI TS 101 211), so MIN alone covers a full first-day cycle, and it
    # is only the far future (≤30 s cadence) that a short window could clip.
    epg_wake_dwell_flat_sec: int = 10
    epg_wake_sample_sec: int = 5
    # Hard cap on the whole tour: with every transponder hitting its ceiling the
    # box would stay powered for transponders × MAX. Whatever is not visited by
    # then is left to the next night rather than burning the user's electricity.
    epg_wake_tour_max_sec: int = 1800
    # Harvesting while the box already sits in light standby costs nothing a
    # viewer can notice: no boot, so no HDMI-CEC pulse and no TV switching
    # itself on. Look for that opportunity this often, and do not repeat the
    # tour more than once per cooldown.
    epg_opportunistic_check_min: int = 10
    epg_opportunistic_cooldown_h: int = 6
    # The nightly wake is the expensive path — it boots the box, and the boot
    # turns the TV on. Stale EPG is a mild annoyance; a TV waking the household
    # at 03:30 is not, so the bar for waking is deliberately high.
    #
    # Judging that by horizon alone is wrong: broadcasters differ by a factor of
    # five in how far ahead they transmit (measured 2026-08-16 — ZDF/ARD ~320 h,
    # RTL ~166 h, ProSieben ~130 h, TELE 5 and DMAX 62 h). A median across all
    # channels is dragged under the threshold by stations that were never deep
    # to begin with, while the ones actually watched are still days ahead.
    #
    # So the question is near-term *coverage* on the channels that matter: does
    # each still reach far enough ahead to be useful? A 2-day broadcaster passes
    # while it is current and fails only once it has genuinely run dry.
    epg_coverage_hours: int = 36
    # 0.85 puts the wake at roughly day 4 of no harvesting at all (measured
    # 2026-08-16 against the real decay curve: day 2 → 0.91, day 4 → 0.85,
    # day 5 → 0.76). 0.7 would have waited until day 6.
    epg_night_wake_below_coverage: float = 0.85
    # Which channels count. Empty = derive from what the household actually
    # watches (confirmed viewing sessions in this window); set a comma-separated
    # list of channel names to pin it by hand instead.
    epg_important_channels: str = ""
    epg_important_days: int = 60
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
