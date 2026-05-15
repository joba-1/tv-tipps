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

    // Receivers
    receivers: [],

    // Admin
    adminStatus: null,
    adminRefreshing: false,
    adminMsg: "",

    // Remote control
    remoteMsg: "",
    _remoteMsgTimer: null,
    remoteScreenshotUrl: "",
    _remoteScreenshotTimer: null,
    remoteSending: false,

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
          this._toast(data.woke
            ? this.t("msg.zap_woke", { receiver: data.receiver_name })
            : this.t("msg.zap_ok",   { receiver: data.receiver_name })
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
      } catch (e) {
        console.error("loadEpg failed:", e);
      } finally {
        this.loadingEpg = false;
      }
    },

    setEpgContext(ctx) {
      this.epgContext = ctx;
      this.loadEpg();
    },

    // ── Admin ────────────────────────────────────────────────────────────────

    async loadAdminStatus() {
      try {
        const res = await fetch("/api/admin/status");
        if (!res.ok) return;
        this.adminStatus = await res.json();
        this.receivers = this.adminStatus.receivers;
      } catch (_) {}
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
      const recv = this.selectedReceiver ? encodeURIComponent(this.selectedReceiver.name) : "";
      this.remoteScreenshotUrl =
        `/api/remote/screenshot?${recv ? "receiver=" + recv + "&" : ""}t=${Date.now()}`;
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
