// tv-tips Alpine.js application

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
    loadingRecs: false,
    recsContext: "now",
    zapToast: "",
    _zapTimer: null,

    // Now & Next
    nowNext: [],
    loadingNow: false,
    lastRefresh: "—",
    staleBanner: false,

    // EPG range
    epgEvents: [],
    loadingEpg: false,
    epgContext: "tonight",
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

    // Remote control
    remoteMsg: "",
    _remoteMsgTimer: null,
    remoteScreenshotUrl: "",
    _remoteScreenshotTimer: null,
    remoteSending: false,
    remoteScreenshotLoading: false,

    // Modal
    modalOpen: false,
    modalEvent: null,
    modalChannel: "",

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
      const cookie = this._getCookie("tv_tips_lang");
      if (cookie) return cookie;
      const browser = (navigator.language || "de").split("-")[0].toLowerCase();
      document.cookie = `tv_tips_lang=${browser}; path=/; max-age=31536000; SameSite=Lax`;
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
        if (this.page === "epg")    this.loadEpg();
        if (this.page === "now")    this.loadNowNext();
        if (this.page === "recs")   this.loadRecs();
        if (this.page === "admin")  this.loadAdminStatus();
        if (this.page === "remote") this._startScreenshotPoll();
      };
      window.addEventListener("hashchange", route);

      await this.loadReceivers();

      const cookieUser = this._getCookie("tv_tips_user");
      this.currentUser = this.users.find(u => u.slug === cookieUser) || this.users[0] || null;

      const cookieReceiver = this._getCookie("tv_tips_receiver");
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
        if (this.page === "now")  this.loadNowNext();
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
              const slug = this._getCookie("tv_tips_user");
              this.currentUser = this.users.find(u => u.slug === slug) || this.users[0];
            }
          }
        }
        // Keep selectedReceiver in sync when receivers list refreshes
        if (this.selectedReceiver) {
          this.selectedReceiver = this.receivers.find(r => r.name === this.selectedReceiver.name) || null;
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
      } catch (e) {
        console.error("loadRecs failed:", e);
      } finally {
        this.loadingRecs = false;
      }
    },

    setRecsContext(ctx) {
      this.recsContext = ctx;
      this.recsData = null;
      this.loadRecs();
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
      this.zapToast = msg;
      clearTimeout(this._zapTimer);
      this._zapTimer = setTimeout(() => { this.zapToast = ""; }, 3500);
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
        else if (this.epgContext === "2h")  url += "hours=2";
        else if (this.epgContext === "4h")  url += "hours=4";
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
      const rows = document.querySelectorAll(".epg-row[data-end]");
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
      const recv = this.selectedReceiver ? encodeURIComponent(this.selectedReceiver.name) : "";
      const url = `/api/remote/screenshot?${recv ? "receiver=" + recv + "&" : ""}t=${Date.now()}`;
      this.remoteScreenshotLoading = true;
      const img = new Image();
      img.onload = () => { this.remoteScreenshotUrl = url; this.remoteScreenshotLoading = false; };
      img.onerror = () => { this.remoteScreenshotUrl = ""; this.remoteScreenshotLoading = false; };
      img.src = url;
    },

    _startScreenshotPoll() {
      this.refreshScreenshot();
      if (!this._remoteScreenshotTimer) {
        this._remoteScreenshotTimer = setInterval(() => this.refreshScreenshot(), 5000);
      }
    },

    _stopScreenshotPoll() {
      if (this._remoteScreenshotTimer) {
        clearInterval(this._remoteScreenshotTimer);
        this._remoteScreenshotTimer = null;
      }
      this.remoteScreenshotUrl = "";
    },

    _remoteToast(msg) {
      this.remoteMsg = msg;
      clearTimeout(this._remoteMsgTimer);
      this._remoteMsgTimer = setTimeout(() => { this.remoteMsg = ""; }, 2000);
    },

    // ── User switching ────────────────────────────────────────────────────────

    async setUser(slug) {
      this.currentUser = this.users.find(u => u.slug === slug) || this.users[0];
      document.cookie = `tv_tips_user=${slug}; path=/; max-age=31536000; SameSite=Lax`;
      this.recsData = null;
      await this.loadLikes();
      if (this.page === "recs") await this.loadRecs();
      if (this.page === "now")  await this.loadNowNext();
      if (this.page === "epg")  await this.loadEpg();
    },

    setReceiver(name) {
      if (name === null) {
        this.selectedReceiver = null;
        document.cookie = "tv_tips_receiver=; path=/; max-age=0; SameSite=Lax";
      } else {
        this.selectedReceiver = this.receivers.find(r => r.name === name) || null;
        if (this.selectedReceiver)
          document.cookie = `tv_tips_receiver=${name}; path=/; max-age=31536000; SameSite=Lax`;
      }
    },

    // ── Helpers ──────────────────────────────────────────────────────────────

    showDetail(event, channelName) {
      this.modalEvent = event;
      this.modalChannel = channelName;
      this.modalOpen = true;
    },

    piconUrl(piconPath) {
      if (!piconPath) return "";
      const best = [...this.receivers]
        .filter(r => r.online)
        .sort((a, b) => a.priority - b.priority)[0];
      return best ? `http://${best.ip}${piconPath}` : "";
    },

    formatTime(isoStr) {
      if (!isoStr) return "";
      const d = new Date(isoStr + "Z");
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
