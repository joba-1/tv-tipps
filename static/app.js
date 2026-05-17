// tv-tipps Alpine.js application

function tvApp() {
  return {
    // Routing
    page: "recs",

    // i18n
    lang: "de",
    i18nStrings: {},
    i18nStatus: "ready",
    _i18nPollTimer: null,

    // Users
    users: [],
    currentUser: null,

    // Receiver selector
    selectedReceiver: null,

    // Recommendations
    recsData: null,
    // Default to "loading" so the spinner shows immediately on page mount —
    // otherwise the empty-state ("no programmes") flashes in the gap between
    // Alpine init and the first fetch.
    loadingRecs: true,
    recsContext: "now",
    zapToast: "",
    _zapTimer: null,

    // Cross-context match lookup: event_id → match_score (0..1). Built from all
    // four contexts so EPG / Now-Next can show the same badge when the AI also
    // picked that event in some Tips view.
    recsMap: {},

    // Active recording timers indexed for fast lookup against an EPG event.
    // Keys: "eit:<event_id>" and "sref:<sref>:<rounded5min>" (covers ±2 min padding).
    // Value: the raw timer entry from OWIF — used to delete via (sref, begin, end).
    timersMap: {},

    // Now & Next
    nowNext: [],
    loadingNow: true,
    lastRefresh: "—",
    staleBanner: false,

    // EPG range
    epgEvents: [],
    loadingEpg: true,
    epgContext: "4h",
    epgSearchQuery: "",
    _epgSearchTimer: null,

    // Likes / dislikes
    likedIds: new Set(),
    dislikedIds: new Set(),

    // Receivers
    receivers: [],

    // Admin
    adminStatus: null,
    adminRefreshing: false,
    adminMsg: "",
    prefsDraft: {},      // slug → current textarea value (editable)
    prefsSaved: {},      // slug → last saved value (for dirty detection)
    prefsSaving: null,   // slug currently being saved (disables button)
    newReceiver: { name: "", ip: "", location: "", priority: 99, power_method: "none", wol_mac: "", intertechno_family: "", intertechno_device: 1, has_genre: false },
    newUser: { slug: "", name: "" },

    // Receiver auto-deselect
    _receiverStandbyAt: null,

    // Remote control
    remoteMsg: "",
    _remoteMsgTimer: null,
    remoteScreenshotUrl: "",
    _remoteScreenshotTimer: null,
    screenshotPollActive: false,
    remoteSending: false,
    remoteScreenshotLoading: false,

    // Modal
    modalOpen: false,
    modalEvent: null,
    modalChannel: "",
    modalSref: "",
    modalIsLive: false,

    // ── i18n helpers ────────────────────────────────────────────────────────

    t(key, params = {}) {
      let s = this.i18nStrings[key] || key;
      for (const [k, v] of Object.entries(params)) {
        s = s.replace(`{${k}}`, v);
      }
      return s;
    },

    tGenre(genreDe) {
      if (!genreDe) return "";
      const normalized = genreDe.toLowerCase().trim().replace(/\s+/g, "_");
      const key = "genre." + normalized;
      return this.i18nStrings[key] || genreDe;
    },

    async _detectLang() {
      const cookie = this._getCookie("tv_tipps_lang");
      if (cookie) return cookie;
      const browser = (navigator.language || "de").split("-")[0].toLowerCase();
      document.cookie = `tv_tipps_lang=${browser}; path=/; max-age=31536000; SameSite=Lax`;
      return browser;
    },

    async _loadI18n() {
      try {
        const res = await fetch(`/api/i18n/${this.lang}`);
        if (!res.ok) return;
        const data = await res.json();
        this.i18nStrings = data.strings;
        this.i18nStatus = data.status;
        if (data.status === "pending" && !this._i18nPollTimer) {
          this._i18nPollTimer = setInterval(() => this._loadI18n(), 15_000);
        }
        if (data.status === "ready" && this._i18nPollTimer) {
          clearInterval(this._i18nPollTimer);
          this._i18nPollTimer = null;
        }
      } catch (_) {}
    },

    // ── Init ────────────────────────────────────────────────────────────────

    async init() {
      // Language first so the first render is already localised
      this.lang = await this._detectLang();
      await this._loadI18n();

      const route = () => {
        const prevPage = this.page;
        const hash = window.location.hash.replace(/^#\/?/, "");
        this.page = hash || "recs";
        if (prevPage === "remote" && this.page !== "remote") this._stopScreenshotPoll();
        if (prevPage === "recs"   && this.page !== "recs")   this._cancelFastRecsPoll();
        if (this.page === "epg")    { this.loadEpg();     this.loadRecsMap(); this.loadTimers(); }
        if (this.page === "now")    { this.loadNowNext(); this.loadRecsMap(); this.loadTimers(); }
        if (this.page === "recs")   { this.loadRecs();    this.loadTimers(); }
        if (this.page === "admin")  this.loadAdminStatus();
        if (this.page === "remote") this._startScreenshotPoll();
      };
      window.addEventListener("hashchange", route);

      await this.loadReceivers();

      const cookieUser = this._getCookie("tv_tipps_user");
      this.currentUser = this.users.find(u => u.slug === cookieUser) || this.users[0] || null;
      if (this.currentUser && !cookieUser) {
        document.cookie = `tv_tipps_user=${this.currentUser.slug}; path=/; max-age=31536000; SameSite=Lax`;
      }

      const cookieReceiver = this._getCookie("tv_tipps_receiver");
      this.selectedReceiver = this.receivers.find(r => r.name === cookieReceiver) || null;

      await this.loadLikes();

      if (this.receivers.length === 0 || this.users.length === 0) {
        if (!window.location.hash || window.location.hash === "#" || window.location.hash === "#recs") {
          window.location.hash = "#admin";
        }
      }

      route();

      setInterval(() => this.loadReceivers(), 30_000);
      setInterval(() => {
        if (this.page === "recs") this.loadRecs();
        if (this.page === "now")  { this.loadNowNext(); this.loadRecsMap(); }
        if (this.page === "epg")  this.loadRecsMap();
        if (this.page === "epg" || this.page === "now" || this.page === "recs") this.loadTimers();
      }, 120_000);
    },

    // ── Receivers / users ───────────────────────────────────────────────────

    async loadReceivers() {
      try {
        const res = await fetch("/api/receivers");
        if (!res.ok) return;
        this.receivers = await res.json();
        if (this.users.length === 0) {
          const ur = await fetch("/api/users");
          if (ur.ok) {
            this.users = await ur.json();
            if (!this.currentUser && this.users.length > 0) {
              const slug = this._getCookie("tv_tipps_user");
              this.currentUser = this.users.find(u => u.slug === slug) || this.users[0];
            }
          }
        }
        // Keep selectedReceiver in sync when receivers list refreshes
        if (this.selectedReceiver) {
          this.selectedReceiver = this.receivers.find(r => r.name === this.selectedReceiver.name) || null;
        }
        // Auto-deselect receiver if it has been inactive for 30 minutes
        if (this.selectedReceiver) {
          const isActive = this.selectedReceiver.online && this.selectedReceiver.power_state === "on";
          if (!isActive) {
            if (!this._receiverStandbyAt) this._receiverStandbyAt = Date.now();
            else if (Date.now() - this._receiverStandbyAt > 30 * 60 * 1000) {
              this.setReceiver(null);
              this._receiverStandbyAt = null;
            }
          } else {
            this._receiverStandbyAt = null;
          }
        } else {
          this._receiverStandbyAt = null;
        }
      } catch (_) {}
    },

    // ── Recommendations ─────────────────────────────────────────────────────

    async loadRecs() {
      this.loadingRecs = true;
      try {
        const res = await fetch(
          `/api/recommendations?context=${this.recsContext}`,
          { credentials: "include" }
        );
        if (!res.ok) throw new Error(res.statusText);
        this.recsData = await res.json();
        // Server says it's still warming an LLM ranking → poll faster until it lands.
        if (this.recsData?.regenerating && this.page === "recs") {
          this._scheduleFastRecsPoll();
        } else {
          this._cancelFastRecsPoll();
        }
      } catch (e) {
        console.error("loadRecs failed:", e);
      } finally {
        this.loadingRecs = false;
      }
    },

    _scheduleFastRecsPoll() {
      if (this._fastRecsTimer) return; // already queued
      this._fastRecsTimer = setTimeout(() => {
        this._fastRecsTimer = null;
        if (this.page === "recs") this.loadRecs();
      }, 15_000);
    },

    _cancelFastRecsPoll() {
      if (this._fastRecsTimer) {
        clearTimeout(this._fastRecsTimer);
        this._fastRecsTimer = null;
      }
    },

    async loadRecsMap() {
      // Aggregate AI picks across all contexts → { event_id: match_score }.
      // All four calls hit warm caches (~5 ms each).
      const map = {};
      const ctxs = ["now", "next", "prime", "today"];
      await Promise.all(ctxs.map(async (ctx) => {
        try {
          const res = await fetch(`/api/recommendations?context=${ctx}`,
                                  { credentials: "include" });
          if (!res.ok) return;
          const data = await res.json();
          for (const r of (data.recommendations || [])) {
            if (!r.id) continue;
            // When the same event is picked in multiple contexts, keep the strongest match.
            if (!(r.id in map) || r.match_score > map[r.id]) {
              map[r.id] = r.match_score;
            }
          }
        } catch (_) {}
      }));
      this.recsMap = map;
    },

    recsMatch(eventId) {
      return this.recsMap[eventId];  // undefined → no badge
    },

    async loadTimers() {
      try {
        const recv = this.selectedReceiver
          ? `?receiver=${encodeURIComponent(this.selectedReceiver.name)}`
          : "";
        const res = await fetch(`/api/remote/timers${recv}`, { credentials: "include" });
        if (!res.ok) return;
        const data = await res.json();
        const map = {};
        for (const t of (data.timers || [])) {
          // Skip cancelled/disabled timers
          if (t.disabled) continue;
          if (t.eit) map[`eit:${t.eit}`] = t;
          if (t.serviceref && typeof t.begin === "number") {
            // Round begin to nearest 5-min slot — handles our 2 min pre-padding
            // plus minor broadcaster slip without inventing duplicate keys.
            const rounded = Math.round(t.begin / 300) * 300;
            map[`sref:${t.serviceref}:${rounded}`] = t;
          }
        }
        this.timersMap = map;
      } catch (_) {}
    },

    findTimer(ev) {
      if (!ev) return null;
      // Prefer eit match — cheapest and most reliable.
      if (ev.event_id != null) {
        const byEit = this.timersMap[`eit:${ev.event_id}`];
        if (byEit) return byEit;
      }
      // Fallback: serviceref + start_time, allowing for the 2-min pre-padding.
      const start = this._parseEventTime(ev.start_time);
      if (!start || !ev.sref) return null;
      const startSec = Math.floor(start.getTime() / 1000);
      for (const delta of [-120, 0, 120]) {
        const rounded = Math.round((startSec - 120 + delta) / 300) * 300;
        const hit = this.timersMap[`sref:${ev.sref}:${rounded}`];
        if (hit) return hit;
      }
      return null;
    },

    async removeTimer() {
      const t = this.findTimer(this.modalEvent);
      if (!t) return;
      try {
        const body = {
          sref: t.serviceref,
          begin: t.begin,
          end: t.end,
        };
        if (this.selectedReceiver) body.receiver = this.selectedReceiver.name;
        const res = await fetch("/api/remote/timer/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          this._toast(this.t("msg.timer_removed",
            { receiver: data.receiver_location || data.receiver_name }));
          // Optimistic local update so the badge disappears right away.
          this.timersMap = Object.fromEntries(
            Object.entries(this.timersMap).filter(([, v]) =>
              !(v.serviceref === t.serviceref && v.begin === t.begin && v.end === t.end))
          );
        } else {
          this._toast(this.t("msg.timer_remove_fail"));
        }
      } catch (_) {
        this._toast(this.t("msg.connect_error"));
      }
    },

    setRecsContext(ctx) {
      this.recsContext = ctx;
      this.recsData = null;
      this._cancelFastRecsPoll();
      this.loadRecs();
    },

    canRecord() {
      // A recording timer makes sense only for events that haven't ended yet.
      const end = this._parseEventTime(this.modalEvent?.end_time);
      return !!(this.modalSref && end && end.getTime() > Date.now());
    },

    async recordEvent() {
      if (!this.modalSref || !this.modalEvent) return;
      try {
        const body = {
          sref: this.modalSref,
          start_time: this.modalEvent.start_time,
          end_time: this.modalEvent.end_time,
          title: this.modalEvent.title || "",
          short_desc: this.modalEvent.short_desc || "",
          event_id: this.modalEvent.event_id ?? null,
        };
        if (this.selectedReceiver) body.receiver = this.selectedReceiver.name;
        const res = await fetch("/api/remote/record", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          this._toast(this.t("msg.record_ok",
            { receiver: data.receiver_location || data.receiver_name }));
          // Re-fetch the timer list so the badge shows up right away.
          this.loadTimers();
        } else {
          // Surface the OWIF reason (often "conflict with…" or "no event matched"),
          // which is what actually changes between retries.
          const reason = data.message ? ` — ${data.message}` : "";
          this._toast(this.t("msg.record_fail") + reason);
        }
      } catch (_) {
        this._toast(this.t("msg.connect_error"));
      }
    },

    async watchChannel(sref) {
      try {
        const body = { sref };
        if (this.selectedReceiver) body.receiver = this.selectedReceiver.name;
        const res = await fetch("/api/remote/zap", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          const receiverLabel = data.receiver_location || data.receiver_name;
          this._toast(data.woke
            ? this.t("msg.zap_woke", { receiver: receiverLabel })
            : this.t("msg.zap_ok",   { receiver: receiverLabel })
          );
        } else {
          this._toast(this.t("msg.zap_fail"));
        }
      } catch (_) {
        this._toast(this.t("msg.connect_error"));
      }
    },

    _toast(msg) {
      console.log("[toast]", msg);  // visible in Safari Web Inspector if user enables it
      this.zapToast = msg;
      clearTimeout(this._zapTimer);
      this._zapTimer = setTimeout(() => { this.zapToast = ""; }, 5000);
    },

    // ── Now & Next ──────────────────────────────────────────────────────────

    async loadNowNext() {
      this.loadingNow = true;
      try {
        const res = await fetch("/api/now-next", { credentials: "include" });
        if (!res.ok) throw new Error(res.statusText);
        this.nowNext = await res.json();
        this.staleBanner = this.nowNext.filter(ch => ch.stale).length > 1;
        this.lastRefresh = new Date().toLocaleTimeString(
          this.lang + "-" + this.lang.toUpperCase(),
          { hour: "2-digit", minute: "2-digit" }
        );
      } catch (e) {
        console.error("loadNowNext failed:", e);
      } finally {
        this.loadingNow = false;
      }
    },

    // ── EPG ─────────────────────────────────────────────────────────────────

    async loadEpg() {
      this.loadingEpg = true;
      try {
        let url = "/api/epg?";
        if (this.epgContext === "tonight") url += "context=tonight";
        else if (this.epgContext === "4h")  url += "hours=4";
        else if (this.epgContext === "all") url += "hours=72";
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) throw new Error(res.statusText);
        this.epgEvents = await res.json();
        this.$nextTick(() => this._epgScrollToNow());
      } catch (e) {
        console.error("loadEpg failed:", e);
      } finally {
        this.loadingEpg = false;
      }
    },

    _epgScrollToNow() {
      const now = new Date().toISOString();
      const rows = document.querySelectorAll(".event-row[data-end]");
      for (const row of rows) {
        if (row.dataset.end > now) {
          row.scrollIntoView({ block: "start" });
          return;
        }
      }
    },

    setEpgContext(ctx) {
      this.epgContext = ctx;
      this.epgSearchQuery = "";
      this.loadEpg();
    },

    debouncedEpgSearch() {
      clearTimeout(this._epgSearchTimer);
      const q = this.epgSearchQuery.trim();
      this._epgSearchTimer = setTimeout(() => {
        if (q.length >= 2) this.searchEpg();
        else this.loadEpg();
      }, 300);
    },

    async searchEpg() {
      this.loadingEpg = true;
      try {
        const q = encodeURIComponent(this.epgSearchQuery.trim());
        const res = await fetch(`/api/epg/search?q=${q}`, { credentials: "include" });
        if (!res.ok) throw new Error(res.statusText);
        this.epgEvents = await res.json();
      } catch (e) {
        console.error("searchEpg failed:", e);
      } finally {
        this.loadingEpg = false;
      }
    },

    // ── Likes ───────────────────────────────────────────────────────────────

    async loadLikes() {
      try {
        const res = await fetch("/api/likes", { credentials: "include" });
        if (!res.ok) { this.likedIds = new Set(); this.dislikedIds = new Set(); return; }
        const data = await res.json();
        this.likedIds    = new Set(data.filter(l => l.epg_event_id && l.sentiment !== "dislike").map(l => l.epg_event_id));
        this.dislikedIds = new Set(data.filter(l => l.epg_event_id && l.sentiment === "dislike").map(l => l.epg_event_id));
      } catch (_) { this.likedIds = new Set(); this.dislikedIds = new Set(); }
    },

    async _toggleReaction(eventId, sentiment) {
      if (!eventId) return;
      try {
        const res = await fetch("/api/likes/toggle", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ epg_event_id: eventId, sentiment }),
        });
        if (!res.ok) return;
        const data = await res.json();
        const nextLiked = new Set(this.likedIds);
        const nextDisliked = new Set(this.dislikedIds);
        if (data.liked) { nextLiked.add(eventId); nextDisliked.delete(eventId); }
        else if (data.disliked) { nextDisliked.add(eventId); nextLiked.delete(eventId); }
        else { nextLiked.delete(eventId); nextDisliked.delete(eventId); }
        this.likedIds = nextLiked;
        this.dislikedIds = nextDisliked;
        this.recsData = null;
      } catch (_) {}
    },

    toggleLike(eventId)    { return this._toggleReaction(eventId, "like"); },
    toggleDislike(eventId) { return this._toggleReaction(eventId, "dislike"); },

    // ── Admin ────────────────────────────────────────────────────────────────

    async loadAdminStatus() {
      try {
        const res = await fetch("/api/admin/status");
        if (!res.ok) return;
        this.adminStatus = await res.json();
        this.receivers = this.adminStatus.receivers;
        await this.loadAllPreferences();
      } catch (_) {}
    },

    async loadAllPreferences() {
      const updated = {};
      await Promise.all(this.users.map(async u => {
        try {
          const r = await fetch(`/api/admin/user-preferences?user=${encodeURIComponent(u.slug)}`);
          if (r.ok) {
            const d = await r.json();
            updated[u.slug] = d.preferences || "";
          }
        } catch (_) {}
      }));
      this.prefsDraft = { ...this.prefsDraft, ...updated };
      this.prefsSaved = { ...this.prefsSaved, ...updated };
    },

    prefsChanged(slug) {
      return (this.prefsDraft[slug] ?? "") !== (this.prefsSaved[slug] ?? "");
    },

    async savePreferences(slug) {
      this.prefsSaving = slug;
      try {
        const user = this.users.find(u => u.slug === slug);
        const res = await fetch(`/api/admin/user-preferences?user=${encodeURIComponent(slug)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preferences: this.prefsDraft[slug] || "" }),
        });
        const data = await res.json();
        this.adminMsg = data.ok
          ? this.t("msg.prefs_saved", { name: user?.name || slug })
          : this.t("msg.prefs_error");
        if (data.ok) {
          this.prefsSaved[slug] = this.prefsDraft[slug] ?? "";
          if (this.recsData) this.recsData = null;
        }
      } catch (_) {
        this.adminMsg = this.t("msg.prefs_error");
      } finally {
        this.prefsSaving = null;
      }
    },

    async adminRefresh(target) {
      this.adminRefreshing = true;
      this.adminMsg = "";
      try {
        const res = await fetch(
          `/api/admin/refresh?target=${target}`,
          { method: "POST" }
        );
        const data = await res.json();
        this.adminMsg = data.ok
          ? this.t("msg.refresh_ok", { target })
          : this.t("msg.refresh_error");
        await this.loadAdminStatus();
      } catch (_) {
        this.adminMsg = this.t("msg.connect_error");
      } finally {
        this.adminRefreshing = false;
      }
    },

    async receiverPower(name, action) {
      try {
        const res = await fetch(
          `/api/admin/power?receiver=${encodeURIComponent(name)}&action=${action}`,
          { method: "POST" }
        );
        const data = await res.json();
        this.adminMsg = data.ok
          ? this.t(action === "wake" ? "msg.power_wake_ok" : "msg.power_sleep_ok",
                   { name, method: data.power_method })
          : this.t("msg.power_error", { error: data.error || "?" });
        setTimeout(() => this.loadReceivers(), 3000);
      } catch (_) {
        this.adminMsg = this.t("msg.connect_error");
      }
    },

    async addReceiver() {
      try {
        const body = { ...this.newReceiver };
        if (!body.wol_mac) delete body.wol_mac;
        if (body.power_method !== "intertechno") { delete body.intertechno_family; delete body.intertechno_device; }
        const res = await fetch("/api/admin/receivers", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (res.ok) {
          this.adminMsg = `✓ ${data.name} ${this.t("msg.receiver_added")}`;
          this.newReceiver = { name: "", ip: "", location: "", priority: 99, power_method: "none", wol_mac: "", has_genre: false };
          await this.loadReceivers();
        } else {
          this.adminMsg = `⚠️ ${data.detail || this.t("msg.refresh_error")}`;
        }
      } catch (_) { this.adminMsg = this.t("msg.connect_error"); }
    },

    async deleteReceiver(name) {
      if (!confirm(`${this.t("msg.confirm_delete")} ${name}?`)) return;
      try {
        const res = await fetch(`/api/admin/receivers/${encodeURIComponent(name)}`, { method: "DELETE" });
        const data = await res.json();
        if (res.ok) {
          this.adminMsg = `✓ ${name} ${this.t("msg.deleted")}`;
          await this.loadReceivers();
        } else {
          this.adminMsg = `⚠️ ${data.detail || this.t("msg.refresh_error")}`;
        }
      } catch (_) { this.adminMsg = this.t("msg.connect_error"); }
    },

    async addUser() {
      try {
        const res = await fetch("/api/admin/users", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.newUser),
        });
        const data = await res.json();
        if (res.ok) {
          this.adminMsg = `✓ ${this.newUser.name} ${this.t("msg.user_added")}`;
          this.newUser = { slug: "", name: "" };
          await this.loadUsers();
        } else {
          this.adminMsg = `⚠️ ${data.detail || this.t("msg.refresh_error")}`;
        }
      } catch (_) { this.adminMsg = this.t("msg.connect_error"); }
    },

    async deleteUser(slug) {
      if (!confirm(`${this.t("msg.confirm_delete")} ${slug}?`)) return;
      try {
        const res = await fetch(`/api/admin/users/${encodeURIComponent(slug)}`, { method: "DELETE" });
        const data = await res.json();
        if (res.ok) {
          this.adminMsg = `✓ ${slug} ${this.t("msg.deleted")}`;
          await this.loadUsers();
        } else {
          this.adminMsg = `⚠️ ${data.detail || this.t("msg.refresh_error")}`;
        }
      } catch (_) { this.adminMsg = this.t("msg.connect_error"); }
    },

    async loadUsers() {
      try {
        const res = await fetch("/api/users");
        if (res.ok) this.users = await res.json();
      } catch (_) {}
    },

    // ── Remote control ───────────────────────────────────────────────────────

    async sendKey(key) {
      this.remoteSending = true;
      try {
        const body = { key };
        if (this.selectedReceiver) body.receiver = this.selectedReceiver.name;
        const res = await fetch("/api/remote/key", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        this._remoteToast(data.ok ? "✓" : "⚠️");
        if (data.ok) setTimeout(() => this.refreshScreenshot(), 800);
      } catch (_) {
        this._remoteToast(this.t("msg.connect_error"));
      } finally {
        this.remoteSending = false;
      }
    },

    async remoteOn() {
      const name = this.selectedReceiver?.name;
      if (!name) { this._remoteToast("⚠️"); return; }
      try {
        const res = await fetch(
          `/api/admin/power?receiver=${encodeURIComponent(name)}&action=wake`,
          { method: "POST" }
        );
        const data = await res.json();
        this._remoteToast(data.ok ? "✓" : "⚠️");
        if (data.ok) setTimeout(() => this.refreshScreenshot(), 4000);
      } catch (_) { this._remoteToast("⚠️"); }
    },

    async remoteOff() {
      const name = this.selectedReceiver?.name;
      if (!name) { this._remoteToast("⚠️"); return; }
      try {
        const res = await fetch(
          `/api/admin/power?receiver=${encodeURIComponent(name)}&action=sleep`,
          { method: "POST" }
        );
        const data = await res.json();
        this._remoteToast(data.ok ? "✓" : "⚠️");
      } catch (_) { this._remoteToast("⚠️"); }
    },

    refreshScreenshot() {
      if (this.remoteScreenshotLoading) return;
      if (this.selectedReceiver && (!this.selectedReceiver.online || this.selectedReceiver.power_state === "off")) return;
      const recv = this.selectedReceiver ? encodeURIComponent(this.selectedReceiver.name) : "";
      const url = `/api/remote/screenshot?${recv ? "receiver=" + recv + "&" : ""}t=${Date.now()}`;
      this.remoteScreenshotLoading = true;
      const img = new Image();
      img.onload = () => { this.remoteScreenshotUrl = url; this.remoteScreenshotLoading = false; };
      img.onerror = () => { this.remoteScreenshotUrl = ""; this.remoteScreenshotLoading = false; };
      img.src = url;
    },

    _startScreenshotPoll() {
      this.screenshotPollActive = true;
      this.refreshScreenshot();
      if (!this._remoteScreenshotTimer) {
        this._remoteScreenshotTimer = setInterval(() => this.refreshScreenshot(), 5000);
      }
    },

    _stopScreenshotPoll() {
      this.screenshotPollActive = false;
      if (this._remoteScreenshotTimer) {
        clearInterval(this._remoteScreenshotTimer);
        this._remoteScreenshotTimer = null;
      }
    },

    toggleScreenshotPoll() {
      if (this.screenshotPollActive) this._stopScreenshotPoll();
      else this._startScreenshotPoll();
    },

    _remoteToast(msg) {
      this.remoteMsg = msg;
      clearTimeout(this._remoteMsgTimer);
      this._remoteMsgTimer = setTimeout(() => { this.remoteMsg = ""; }, 2000);
    },

    // ── User switching ────────────────────────────────────────────────────────

    async setUser(slug) {
      this.currentUser = this.users.find(u => u.slug === slug) || this.users[0];
      document.cookie = `tv_tipps_user=${slug}; path=/; max-age=31536000; SameSite=Lax`;
      this.recsData = null;
      this.recsMap = {};
      await this.loadLikes();
      // Timers are receiver-wide, not user-specific, so we don't reset timersMap.
      if (this.page === "recs") await this.loadRecs();
      if (this.page === "now")  { await this.loadNowNext(); this.loadRecsMap(); }
      if (this.page === "epg")  { await this.loadEpg();     this.loadRecsMap(); }
    },

    setReceiver(name) {
      if (name === null) {
        this.selectedReceiver = null;
        document.cookie = "tv_tipps_receiver=; path=/; max-age=0; SameSite=Lax";
      } else {
        this.selectedReceiver = this.receivers.find(r => r.name === name) || null;
        if (this.selectedReceiver)
          document.cookie = `tv_tipps_receiver=${name}; path=/; max-age=31536000; SameSite=Lax`;
      }
      // Timers are per-receiver — refresh so badges reflect the new target.
      this.timersMap = {};
      this.loadTimers();
    },

    // ── Helpers ──────────────────────────────────────────────────────────────

    isLive(event) {
      if (!event) return false;
      if (typeof event.progress_pct === "number" && event.progress_pct > 0) return true;
      const start = this._parseEventTime(event.start_time);
      const end   = this._parseEventTime(event.end_time);
      if (!start || !end) return false;
      const now = Date.now();
      return start.getTime() <= now && now < end.getTime();
    },

    progressPct(event) {
      if (!event) return 0;
      if (typeof event.progress_pct === "number" && event.progress_pct > 0) return event.progress_pct;
      const start = this._parseEventTime(event.start_time);
      const end   = this._parseEventTime(event.end_time);
      if (!start || !end) return 0;
      const now = Date.now();
      const s = start.getTime(), e = end.getTime();
      if (now <= s || e <= s) return 0;
      if (now >= e) return 100;
      return ((now - s) / (e - s)) * 100;
    },

    get nowNextFlat() {
      const out = [];
      for (const ch of this.nowNext) {
        const channel_name = ch.channel_name;
        const sref = ch.sref;
        if (ch.now)  out.push({ ...ch.now,  channel_name, sref, _kind: "now"  });
        if (ch.next) out.push({ ...ch.next, channel_name, sref, _kind: "next" });
      }
      return out;
    },

    displayChannel(name) {
      if (!name) return "";
      let s = name
        .replace(/\bFernsehen\b/gi, "")
        .replace(/Central\+1/gi, "")
        .replace(/HD\b/g, "")
        .replace(/\s+/g, " ")
        .trim()
        .replace(/^[-\s]+|[-\s]+$/g, "");
      return s || name;
    },

    showDetail(event, channelName, sref = null) {
      this.modalEvent = event;
      this.modalChannel = channelName;
      this.modalSref = sref || event?.sref || "";
      this.modalIsLive = this.isLive(event);
      this.modalOpen = true;
    },

    piconUrl(piconPath) {
      if (!piconPath) return "";
      const best = [...this.receivers]
        .filter(r => r.online)
        .sort((a, b) => a.priority - b.priority)[0];
      return best ? `http://${best.ip}${piconPath}` : "";
    },

    _parseEventTime(s) {
      // Accepts:
      //   "2026-05-16T10:55:00"        → naive UTC (server ISO, no offset)
      //   "2026-05-16T10:55:00Z"       → explicit UTC
      //   "2026-05-16T10:55:00+02:00"  → with offset
      //   "2026-05-16 10:55"           → legacy: already in server-local time
      if (!s) return null;
      if (s.includes("T")) {
        const hasTz = /[Zz]|[+-]\d{2}:?\d{2}$/.test(s);
        return new Date(hasTz ? s : s + "Z");
      }
      // Legacy space-separated, treat as local — preserves correct display for
      // stale recommendation cache entries written before the API was switched to ISO UTC.
      return new Date(s.replace(" ", "T"));
    },

    formatTime(isoStr) {
      const d = this._parseEventTime(isoStr);
      if (!d || isNaN(d.getTime())) return "";
      return d.toLocaleTimeString(this.lang, { hour: "2-digit", minute: "2-digit" });
    },

    _getCookie(name) {
      const match = document.cookie.match(
        new RegExp("(?:^|; )" + name + "=([^;]*)")
      );
      return match ? decodeURIComponent(match[1]) : null;
    },
  };
}
