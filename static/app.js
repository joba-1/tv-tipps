// tv-tips Alpine.js application

const USERS = [
  { slug: "alice", name: "Alice" },
  { slug: "bob",   name: "Bob"   },
];

function tvApp() {
  return {
    // Routing
    page: "now",

    // User
    users: USERS,
    currentUser: null,

    // Now & Next
    nowNext: [],
    loadingNow: false,
    lastRefresh: "—",
    staleBanner: false,

    // EPG range
    epgEvents: [],
    loadingEpg: false,
    epgContext: "tonight",

    // Receiver status
    receiverStatus: [],

    // Modal
    modalOpen: false,
    modalEvent: null,
    modalChannel: "",

    // Auto-refresh timer
    _nowNextTimer: null,

    async init() {
      // Hash-based routing
      const route = () => {
        const hash = window.location.hash.replace("#", "").replace(/^\//, "");
        this.page = hash || "now";
        if (this.page === "epg") this.loadEpg();
      };
      window.addEventListener("hashchange", route);
      route();

      // Restore user from cookie
      const cookieUser = this._getCookie("tv_tips_user");
      this.currentUser = USERS.find(u => u.slug === cookieUser) || USERS[0];

      // Initial load
      await this.loadNowNext();
      this.loadReceiverStatus();

      // Auto-refresh now & next every 2 min
      this._nowNextTimer = setInterval(() => this.loadNowNext(), 120_000);
      // Refresh receiver status every 30s
      setInterval(() => this.loadReceiverStatus(), 30_000);
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
        else if (this.epgContext === "2h") url += "hours=2";
        else if (this.epgContext === "4h") url += "hours=4";
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) throw new Error(res.statusText);
        this.epgEvents = await res.json();
      } catch (e) {
        console.error("loadEpg failed:", e);
      } finally {
        this.loadingEpg = false;
      }
    },

    async loadReceiverStatus() {
      try {
        const res = await fetch("/api/admin/status");
        if (!res.ok) return;
        const data = await res.json();
        this.receiverStatus = data.receivers;
        const anyOffline = data.receivers.some(r => !r.online || r.power_state !== "on");
        if (anyOffline && this.nowNext.length > 0) this.staleBanner = true;
      } catch (_) { /* silent */ }
    },

    setEpgContext(ctx) {
      this.epgContext = ctx;
      this.loadEpg();
    },

    async setUser(slug) {
      this.currentUser = USERS.find(u => u.slug === slug) || USERS[0];
      // Set cookie and reload data
      document.cookie = `tv_tips_user=${slug}; path=/; max-age=31536000; SameSite=Lax`;
      await this.loadNowNext();
      if (this.page === "epg") await this.loadEpg();
    },

    showDetail(event, channelName) {
      this.modalEvent = event;
      this.modalChannel = channelName;
      this.modalOpen = true;
    },

    piconUrl(ch) {
      // Picons are served from the receiver directly
      // We proxy them through /picon/ route (added later) or link directly
      // For now, use box15 as primary source
      if (!ch.picon_path) return "";
      return `http://192.168.1.15${ch.picon_path}`;
    },

    formatTime(isoStr) {
      if (!isoStr) return "";
      const d = new Date(isoStr + "Z"); // treat as UTC
      return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    },

    isPrimeTime(ev) {
      if (!ev?.start_time) return false;
      const d = new Date(ev.start_time + "Z");
      const h = d.toLocaleString("de-DE", { hour: "2-digit", timeZone: "Europe/Berlin" });
      const hour = parseInt(h, 10);
      return hour >= 20 && hour < 21;
    },

    _getCookie(name) {
      const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
      return match ? decodeURIComponent(match[1]) : null;
    },
  };
}
