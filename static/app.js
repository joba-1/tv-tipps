// tv-tips Alpine.js application

function tvApp() {
  return {
    // Routing
    page: "recs",

    // Users
    users: [],
    currentUser: null,

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

    // Modal
    modalOpen: false,
    modalEvent: null,
    modalChannel: "",

    async init() {
      const route = () => {
        const hash = window.location.hash.replace(/^#\/?/, "");
        this.page = hash || "recs";
        if (this.page === "epg")   this.loadEpg();
        if (this.page === "now")   this.loadNowNext();
        if (this.page === "recs")  this.loadRecs();
        if (this.page === "admin") this.loadAdminStatus();
      };
      window.addEventListener("hashchange", route);

      await this.loadReceivers();

      // Restore user from cookie; fall back to first from API
      const cookieUser = this._getCookie("tv_tips_user");
      this.currentUser = this.users.find(u => u.slug === cookieUser) || this.users[0] || null;

      route();

      // Periodic refresh
      setInterval(() => this.loadReceivers(), 30_000);
      setInterval(() => {
        if (this.page === "recs") this.loadRecs();
        if (this.page === "now")  this.loadNowNext();
      }, 120_000);
    },

    async loadReceivers() {
      try {
        const res = await fetch("/api/receivers");
        if (!res.ok) return;
        this.receivers = await res.json();
        // Also fetch users if not loaded yet
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
        const anyOffline = this.receivers.some(r => !r.online || r.power_state !== "on");
        if (anyOffline && (this.nowNext.length > 0 || (this.recsData && this.recsData.recommendations && this.recsData.recommendations.length > 0))) {
          this.staleBanner = true;
        }
      } catch (_) {}
    },

    async loadRecs() {
      this.loadingRecs = true;
      try {
        const res = await fetch(`/api/recommendations?context=${this.recsContext}`, { credentials: "include" });
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
        const res = await fetch("/api/remote/zap", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sref }),
        });
        const data = await res.json();
        if (data.ok) {
          this._toast(data.woke ? `⚡ ${data.receiver_name} aufgeweckt & umgeschaltet` : `▶ Auf ${data.receiver_name} umgeschaltet`);
        } else {
          this._toast("⚠️ Zap fehlgeschlagen");
        }
      } catch (e) {
        this._toast("⚠️ Verbindungsfehler");
      }
    },

    _toast(msg) {
      this.zapToast = msg;
      clearTimeout(this._zapTimer);
      this._zapTimer = setTimeout(() => { this.zapToast = ""; }, 3500);
    },

    async loadNowNext() {
      this.loadingNow = true;
      try {
        const res = await fetch("/api/now-next", { credentials: "include" });
        if (!res.ok) throw new Error(res.statusText);
        this.nowNext = await res.json();
        this.staleBanner = this.nowNext.some(ch => ch.stale);
        this.lastRefresh = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
      } catch (e) {
        console.error("loadNowNext failed:", e);
      } finally {
        this.loadingNow = false;
      }
    },

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
        const res = await fetch(`/api/admin/refresh?target=${target}`, { method: "POST" });
        const data = await res.json();
        this.adminMsg = data.ok ? `✓ ${target} aktualisiert` : `⚠️ Fehler`;
        await this.loadAdminStatus();
      } catch (_) {
        this.adminMsg = "⚠️ Verbindungsfehler";
      } finally {
        this.adminRefreshing = false;
      }
    },

    async receiverPower(name, action) {
      try {
        const res = await fetch(`/api/admin/power?receiver=${encodeURIComponent(name)}&action=${action}`, { method: "POST" });
        const data = await res.json();
        this.adminMsg = data.ok
          ? `✓ ${name}: ${action === "wake" ? "aufgeweckt" : "schlafen gelegt"} (${data.power_method})`
          : `⚠️ ${data.error || "Fehler"}`;
        setTimeout(() => this.loadReceivers(), 3000);
      } catch (_) {
        this.adminMsg = "⚠️ Verbindungsfehler";
      }
    },

    async setUser(slug) {
      this.currentUser = this.users.find(u => u.slug === slug) || this.users[0];
      document.cookie = `tv_tips_user=${slug}; path=/; max-age=31536000; SameSite=Lax`;
      this.recsData = null;
      if (this.page === "recs")  await this.loadRecs();
      if (this.page === "now")   await this.loadNowNext();
      if (this.page === "epg")   await this.loadEpg();
    },

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
      return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    },

    _getCookie(name) {
      const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
      return match ? decodeURIComponent(match[1]) : null;
    },
  };
}
