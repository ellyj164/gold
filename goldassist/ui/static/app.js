/* Terminal UI controller: wiring between the API, the chart, and the panels. */

(function () {
  "use strict";

  const state = {
    timeframe: "H1",
    layers: {},
    snapshot: null,
    chart: null,
    chatHistory: [],
    signalFilter: "",
    lastChartFetch: 0,
    // The trader's clock. Every timestamp on screen is rendered through it.
    // Filled from /api/clock at boot; the default matches the server's so a
    // failed request degrades to the right answer rather than to UTC.
    clock: { offsetHours: 2, label: "GMT+2" },
    fired: { date: "", side: "", outcome: "", session: "" },
  };

  const $ = (id) => document.getElementById(id);
  const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n) ? "—" : Number(n).toFixed(d));

  /* ------------------------------------------------------------------ time
   *
   * One place, and only one place, that turns a UTC timestamp into something
   * a human reads. Everything the server sends is UTC; the trader works in
   * GMT+2. Before this existed the interface called `toISOString()` in five
   * places, which is UTC with the Z quietly dropped -- so a signal that fired
   * at 14:37 on the trader's clock was labelled 12:37, and the label looked
   * plausible enough that nothing gave the error away.
   *
   * The offset is applied by shifting the epoch and then reading the UTC
   * fields back. That is deliberate: `toLocaleTimeString` with a timeZone
   * would follow the machine's idea of the zone, which is exactly the thing
   * being pinned down here.
   */
  function shifted(value) {
    if (value === null || value === undefined || value === "") return null;
    let ms;
    if (typeof value === "number") {
      ms = value < 1e12 ? value * 1000 : value;       // seconds or milliseconds
    } else if (value instanceof Date) {
      ms = value.getTime();
    } else {
      let text = String(value).trim();
      // A bare "2026-08-20T12:37:05" from SQLite has no zone marker, but every
      // timestamp this application stores is UTC. Say so explicitly, or the
      // browser would read it as local time and shift it twice.
      if (!/[Zz]|[+-]\d\d:?\d\d$/.test(text) && /\d{4}-\d\d-\d\d/.test(text)) {
        text = text.replace(" ", "T") + "Z";
      }
      ms = Date.parse(text);
    }
    if (!Number.isFinite(ms)) return null;
    return new Date(ms + state.clock.offsetHours * 3600000);
  }

  const pad = (n) => String(n).padStart(2, "0");

  /** The true instant a timestamp refers to — never use `shifted` for this.
   *  `shifted` returns a deliberately displaced Date whose UTC fields read as
   *  the trader's wall clock; asking it what session it is would be off by the
   *  offset. Anything that reasons about time rather than printing it uses
   *  this instead. */
  function asDate(value) {
    const d = shifted(value);
    return d ? new Date(d.getTime() - state.clock.offsetHours * 3600000) : new Date();
  }

  /** "14:37" — the time something happened, on the trader's clock. */
  function fmtClock(value) {
    const d = shifted(value);
    return d ? `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` : "—";
  }

  /** "14:37:05" — for the tape, where the second matters. */
  function fmtClockSeconds(value) {
    const d = shifted(value);
    return d ? `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}` : "—";
  }

  /** "2026-08-20" — the trading date, on the trader's clock. */
  function fmtDate(value) {
    const d = shifted(value);
    return d ? `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` : "—";
  }

  /** "2026-08-20 14:37" */
  function fmtStamp(value) {
    const d = shifted(value);
    return d ? `${fmtDate(value)} ${fmtClock(value)}` : "—";
  }

  /** "2026-08-20 14:37 GMT+2" — unambiguous, for tooltips. */
  function fmtFull(value) {
    const d = shifted(value);
    return d ? `${fmtStamp(value)} ${state.clock.label}` : "—";
  }

  /** "20 Aug" — a compact date for a dense table. */
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtDayShort(value) {
    const d = shifted(value);
    return d ? `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}` : "—";
  }

  /*
   * Which session a moment falls in, worked out in the browser so the live
   * clock can label itself between server pushes. The windows are UTC and
   * mirror goldassist/trader/sessions.py; if those ever move, they move here
   * too, and the server value on a signal remains the authority for anything
   * that gets recorded.
   */
  const SESSION_WINDOWS = [
    { name: "rollover", label: "Rollover", from: 21, to: 23, quality: 0.10 },
    { name: "asia", label: "Asian session", from: 23, to: 7, quality: 0.35 },
    { name: "london", label: "London", from: 7, to: 12, quality: 0.85 },
    { name: "overlap", label: "London/NY overlap", from: 12, to: 16, quality: 1.00 },
    { name: "newyork", label: "New York", from: 16, to: 19, quality: 0.60 },
    { name: "late_newyork", label: "Late New York", from: 19, to: 21, quality: 0.35 },
  ];

  function sessionAt(date) {
    const utc = date || new Date();
    const day = utc.getUTCDay();          // 0 Sunday .. 6 Saturday
    const hour = utc.getUTCHours();
    const closed = day === 6
      || (day === 5 && hour >= 21)
      || (day === 0 && hour < 22);
    if (closed) return { name: "closed", label: "Market closed", quality: 0 };
    for (const w of SESSION_WINDOWS) {
      const inside = w.from < w.to
        ? hour >= w.from && hour < w.to
        : hour >= w.from || hour < w.to;   // the Asian window wraps midnight
      if (inside) return w;
    }
    return { name: "asia", label: "Asian session", quality: 0.35 };
  }

  function sessionClass(quality) {
    if (quality >= 0.85) return "prime";
    if (quality >= 0.5) return "fair";
    if (quality > 0) return "thin";
    return "closed";
  }

  /* The header clock. Ticks locally rather than waiting on the socket, so the
   * time on screen is never a stale echo of the last analysis cycle. */
  function startClock() {
    const tick = () => {
      const now = new Date();
      $("clockTime").textContent = fmtClockSeconds(now);
      $("clockZone").textContent = state.clock.label;
      const session = sessionAt(now);
      $("clockSession").textContent = session.label;
      const dot = $("sessionDot");
      if (dot) dot.className = "session-dot " + sessionClass(session.quality);
    };
    tick();
    setInterval(tick, 1000);
  }

  async function loadClock() {
    try {
      const data = await api("/api/clock");
      state.clock.offsetHours = Number(data.offset_hours ?? 2);
      state.clock.label = data.offset || "GMT+2";
    } catch (_) {
      /* keep the default; the app is still perfectly usable */
    }
    // chart.js draws its time axis independently; give it the same offset so
    // the axis and the signal cards cannot disagree about what "14:00" means.
    window.GA_CLOCK_OFFSET_HOURS = state.clock.offsetHours;
    const label = $("firedClockLabel");
    if (label) label.textContent = state.clock.label;
    if (state.chart) state.chart.draw();
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || body.error || detail;
      } catch (_) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // ---------------------------------------------------------------- chart

  function readLayers() {
    const layers = {};
    document.querySelectorAll("#layerToggles input").forEach((el) => {
      layers[el.dataset.layer] = el.checked;
    });
    return layers;
  }

  async function loadChart() {
    try {
      const data = await api(`/api/chart/${state.timeframe}?bars=400`);
      state.chart.setData(data);
      state.lastChartFetch = Date.now();
    } catch (err) {
      console.error("chart load failed", err);
    }
  }

  function initChart() {
    const tooltip = $("chartTooltip");
    state.layers = readLayers();
    state.chart = new GoldChart($("chart"), {
      layers: state.layers,
      onHover: (candle, x, y) => {
        if (!candle) { tooltip.hidden = true; return; }
        const date = fmtStamp(candle.time * 1000);
        tooltip.innerHTML =
          `<b>${date}</b><br>O ${fmt(candle.open)} H ${fmt(candle.high)}<br>` +
          `L ${fmt(candle.low)} C ${fmt(candle.close)}<br>vol ${candle.volume}`;
        tooltip.hidden = false;
        tooltip.style.left = Math.min(x + 14, $("chart").clientWidth - 150) + "px";
        tooltip.style.top = (y + 14) + "px";
      },
    });

    document.querySelectorAll("#timeframeButtons button").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#timeframeButtons button")
          .forEach((b) => b.classList.toggle("active", b === btn));
        state.timeframe = btn.dataset.tf;
        // A different timeframe is different data, so let the view reset.
        state.chart.view = { start: 0, end: 0 };
        loadChart();
      });
    });

    document.querySelectorAll("#layerToggles input").forEach((el) => {
      el.addEventListener("change", () => {
        state.layers = readLayers();
        state.chart.setLayers(state.layers);
      });
    });
  }



  // ---------------------------------------------------------- alert sound

  /*
   * The chime is synthesised with the Web Audio API rather than shipped as an
   * audio file: nothing extra to bundle, and it works offline in the packaged
   * desktop app.
   *
   * Browsers refuse to play audio until the user has interacted with the page,
   * so the context is created lazily and its state is surfaced on the toggle —
   * a chime that silently never plays would be worse than no chime at all.
   */
  /*
   * One tone per lifecycle stage, so the alert is identifiable without looking:
   * a soft double-tap on one note when a setup first appears, a rising pair when
   * price reaches the entry zone, and a brighter, louder rise on confirmation.
   */
  const TONES = {
    watch: { notes: [466, 466], gain: 0.07 },
    enter: { notes: [520, 660], gain: 0.10 },
    ready: { notes: [660, 990], gain: 0.16 },
    // Results have their own shape: a bright three-note rise for a target
    // banked, a low falling pair for a stop. Both are unmistakable without
    // looking at the screen, which is the point of having them.
    won: { notes: [784, 988, 1319], gain: 0.14 },
    lost: { notes: [392, 294], gain: 0.11 },
  };

  const sound = {
    ctx: null,
    muted: localStorage.getItem("goldassist.muted") === "1",
    blocked: false,

    ensure() {
      if (!this.ctx) {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return null;
        this.ctx = new Ctx();
      }
      if (this.ctx.state === "suspended") {
        this.ctx.resume().catch(() => {});
      }
      this.blocked = this.ctx.state === "suspended";
      return this.ctx;
    },

    // Two rising notes: distinct from system sounds, short enough not to annoy.
    play(kind = "ready") {
      if (this.muted) return;
      const ctx = this.ensure();
      if (!ctx || ctx.state !== "running") { this.blocked = true; updateSoundToggle(); return; }

      const tone = TONES[kind] || TONES.enter;
      const notes = tone.notes;
      const gain = tone.gain;
      notes.forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const env = ctx.createGain();
        const at = ctx.currentTime + i * 0.14;
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, at);
        env.gain.setValueAtTime(0.0001, at);
        env.gain.exponentialRampToValueAtTime(gain, at + 0.02);
        env.gain.exponentialRampToValueAtTime(0.0001, at + 0.30);
        osc.connect(env).connect(ctx.destination);
        osc.start(at);
        osc.stop(at + 0.32);
      });
      this.blocked = false;
      updateSoundToggle();
    },

    toggle() {
      this.muted = !this.muted;
      localStorage.setItem("goldassist.muted", this.muted ? "1" : "0");
      if (!this.muted) this.play("enter");   // confirm it works, and unblock
      updateSoundToggle();
    },
  };

  function updateSoundToggle() {
    const btn = $("soundToggle");
    if (!btn) return;
    btn.classList.toggle("muted", sound.muted);
    btn.classList.toggle("blocked", !sound.muted && sound.blocked);
    btn.textContent = sound.muted ? "🔕" : "🔔";
    btn.title = sound.muted
      ? "Signal alert sound is off — click to enable"
      : sound.blocked
        ? "Click once to allow the alert sound"
        : "Signal alert sound is on";
  }

  // Statuses that mean "act now" versus "pay attention".
  const READY_STATES = ["CONFIRMED"];
  const ATTENTION_STATES = ["IN ZONE", "WAITING FOR CONFIRMATION"];

  /*
   * Which lifecycle states announce themselves, and with which tone.
   *
   * WATCH is in here deliberately. It is the state a setup actually spends its
   * time in -- it is on the board, the entry zone is drawn, and the trader needs
   * to know it exists in order to be waiting at the right price. Announcing only
   * from IN ZONE onwards meant a setup could sit for hours in silence and the
   * chime never fired at all.
   *
   * DEVELOPING stays silent: it is the shape of a setup, not yet a setup.
   * ACTIVE and the terminal states stay silent too -- they report on a trade
   * that has already been taken, which is not an alert.
   */
  const ANNOUNCE_KIND = {
    "WATCH": "watch",
    "IN ZONE": "enter",
    "WAITING FOR CONFIRMATION": "enter",
    "CONFIRMED": "ready",
    // The result is the whole point of tracking a signal, so it announces
    // too -- quietly for a scratch, and never for an idea that never filled.
    "TARGET HIT": "won",
    "COMPLETED": "won",
    "STOPPED OUT": "lost",
  };

  const ALERT_TITLE = {
    watch: "Setup on the board",
    enter: "Price is in the entry zone",
    ready: "Confirmed — entry is live",
    won: "Target hit — banked",
    lost: "Stopped out",
  };

  // Terminal statuses, mirroring goldassist/signals/states.py.
  const TERMINAL_STATES = ["TARGET HIT", "COMPLETED", "STOPPED OUT",
                           "INVALIDATED", "EXPIRED"];
  const WON_STATES = ["TARGET HIT", "COMPLETED"];

  // Each (setup, status) pair announces itself exactly once, so a chime never
  // repeats while the same setup sits in the same state tick after tick.
  const announced = new Set();

  function announce(suggestion) {
    if (!suggestion) return;
    const kind = ANNOUNCE_KIND[suggestion.status];
    if (!kind) return;
    const key = `${suggestion.id}:${suggestion.status}`;
    if (announced.has(key)) return;
    announced.add(key);

    sound.play(kind);
    showAlert(suggestion, kind);
    notifyDesktop(suggestion, kind);
    if (kind === "ready") flashBanner();
  }

  /*
   * Every tracked setup announces, not just the one in the banner.
   *
   * The book holds up to eight concurrent setups and the banner shows only the
   * leading one, so hanging the alert off the banner meant the other seven could
   * reach CONFIRMED without making a sound.
   */
  function announceAll(primary, book) {
    const seen = new Set();
    // Retired setups are included because the result is the announcement that
    // matters most: a target banked or a stop hit is news, and it is the only
    // moment the setup is no longer anywhere on the live board.
    [primary]
      .concat((book && book.live) || [])
      .concat(((book && book.retired) || []).slice(0, 6))
      .forEach((s) => {
        if (!s || seen.has(s.id + ":" + s.status)) return;
        seen.add(s.id + ":" + s.status);
        announce(s);
      });
  }

  function flashBanner() {
    const banner = $("signalBanner");
    if (!banner) return;
    banner.classList.remove("ready");
    void banner.offsetWidth;          // restart the animation
    banner.classList.add("ready");
  }

  /*
   * The on-screen alert.
   *
   * A chime on its own only says "something happened" -- by the time the chart
   * is in focus the trader still has to hunt for the level. So the alert leads
   * with the thing needed to act: the entry zone, the preferred price inside it,
   * and how far spot currently is from it. Stop and first target follow, because
   * an entry without them is not a trade.
   */
  function entryLines(s) {
    const zone = s.entry_zone;
    if (!zone) {
      return `<div class="at-entry"><span>Entry</span><b>at market ${fmt(s.price)}</b></div>`;
    }
    const distance = zone.price_inside
      ? "price is inside the zone now"
      : `${fmt(Math.min(Math.abs(s.price - zone.low), Math.abs(s.price - zone.high)))} away`
        + (zone.distance_atr !== null && zone.distance_atr !== undefined
            ? ` · ${Number(zone.distance_atr).toFixed(2)} ATR` : "")
        + (s.price > zone.high ? " below spot" : " above spot");
    return `<div class="at-entry"><span>Enter</span><b>${fmt(zone.low)} – ${fmt(zone.high)}</b></div>`
      + `<div class="at-entry best"><span>Best price</span><b>${fmt(zone.preferred)}</b></div>`
      + `<div class="at-distance">${escapeHtml(distance)}</div>`;
  }

  // A resolved setup needs a different alert entirely: the entry zone is
  // history, and what the trader wants to see is what it paid.
  function resultLines(s) {
    return `<div class="at-entry"><span>Entry</span><b>${fmt(s.fill_price)}</b></div>`
      + `<div class="at-entry"><span>Exit</span><b>${fmt(s.exit_price)}</b></div>`
      + `<div class="at-result ${s.result_pips >= 0 ? "win" : "loss"}">`
      + `${s.result_pips >= 0 ? "+" : ""}${Math.round(s.result_pips)} pips`
      + ` · ${s.r_multiple >= 0 ? "+" : ""}${Number(s.r_multiple).toFixed(2)}R</div>`
      + `<div class="at-distance">closed on ${escapeHtml(s.exit_reason || "resolution")}`
      + ` — recorded to history</div>`;
  }

  function showAlert(s, kind) {
    const host = $("alertToasts");
    if (!host) return;

    const resolved = kind === "won" || kind === "lost";
    const tp1 = (s.targets || [])[0];
    const el = document.createElement("div");
    el.className = `alert-toast ${kind} ${s.direction}`;
    el.innerHTML =
      `<button class="at-close" title="Dismiss">×</button>`
      + `<div class="at-head">`
      + `<span class="at-dir">${s.direction === "bullish" ? "BUY" : "SELL"}</span>`
      + `<span class="at-title">${escapeHtml(ALERT_TITLE[kind] || s.status)}</span></div>`
      + `<div class="at-setup">${escapeHtml(s.setup_label)} · `
      + `${escapeHtml(s.strategy_name || "")} · ${Math.round(s.score)}/100</div>`
      + (resolved ? resultLines(s) : entryLines(s))
      + (resolved ? "" :
         `<div class="at-levels">`
         + `<span>Stop ${fmt((s.invalidation || {}).price)}</span>`
         + `<span>TP1 ${tp1 ? fmt(tp1.price) : "—"}`
         + `${s.reward_risk ? ` · 1:${s.reward_risk.toFixed(2)}` : ""}</span></div>`);

    const dismiss = () => { el.classList.add("leaving"); setTimeout(() => el.remove(), 260); };
    el.querySelector(".at-close").addEventListener("click", (e) => { e.stopPropagation(); dismiss(); });
    el.addEventListener("click", () => { showSignalCard(); dismiss(); });

    host.prepend(el);
    while (host.children.length > 4) host.lastElementChild.remove();

    // A confirmed entry is time-critical and stays until dismissed; the earlier
    // stages clear themselves so the corner does not silt up over a session.
    if (kind !== "ready") setTimeout(dismiss, 20000);
  }

  function notifyDesktop(s, kind) {
    // A desktop notification is a bonus, never a requirement.
    try {
      if (!("Notification" in window)) return;
      const side = s.direction === "bullish" ? "BUY" : "SELL";
      const zone = s.entry_zone;
      const where = zone
        ? `enter ${fmt(zone.low)}–${fmt(zone.high)} (best ${fmt(zone.preferred)})`
        : `at market ${fmt(s.price)}`;
      if (kind === "won" || kind === "lost") {
        const done = () => new Notification(
          `XAUUSD ${side} · ${ALERT_TITLE[kind]}`,
          { body: `${s.setup_label} · ${s.result_pips >= 0 ? "+" : ""}`
                  + `${Math.round(s.result_pips)} pips (${Number(s.r_multiple).toFixed(2)}R)`,
            tag: `${s.id}:${s.status}` });
        if (Notification.permission === "granted") done();
        return;
      }
      const show = () => new Notification(
        `XAUUSD ${side} · ${ALERT_TITLE[kind] || s.status}`,
        { body: `${s.setup_label} · ${Math.round(s.score)}/100`
                + `
${where}
stop ${fmt((s.invalidation || {}).price)}`,
          tag: `${s.id}:${s.status}` });
      if (Notification.permission === "granted") show();
      else if (Notification.permission !== "denied") {
        Notification.requestPermission().then((p) => { if (p === "granted") show(); });
      }
    } catch (_) { /* notifications are optional */ }
  }

  // --------------------------------------------------------- signal banner

  function renderBanner(s) {
    const banner = $("signalBanner");
    if (!banner) return;
    if (!s) {
      banner.hidden = true;
      banner.classList.remove("ready");
      return;
    }

    banner.hidden = false;
    const ready = READY_STATES.includes(s.status);
    banner.className = `signal-banner ${s.direction}${ready ? " ready" : ""}`;

    $("sbDir").textContent = s.direction === "bullish" ? "BUY" : "SELL";
    $("sbSetup").textContent = s.setup_label
      + (s.confirmations && s.confirmations.length
          ? ` +${s.confirmations.length}` : "");
    $("sbSetup").title = [s.strategy_name].concat(
      (s.confirmations || []).map((c) => c.name)).filter(Boolean).join(" + ");
    $("sbStatus").textContent = s.status
      + (s.trigger && !s.trigger.met && s.trigger.outstanding.length
          ? ` · waiting for ${s.trigger.outstanding[0]}` : "");

    const zone = s.entry_zone;
    $("sbZone").textContent = zone ? `${fmt(zone.low)} – ${fmt(zone.high)}` : "—";
    $("sbTrigger").textContent = s.trigger ? (s.trigger.met ? "met" : "waiting") : "—";
    $("sbTrigger").className = s.trigger && s.trigger.met ? "bull" : "";
    $("sbInval").textContent = fmt((s.invalidation || {}).price);

    const tp1 = (s.targets || [])[0];
    $("sbTp1").textContent = tp1
      ? `${fmt(tp1.price)}  ${Math.round(tp1.pips || 0)}p` : "—";
    $("sbRr").textContent = s.reward_risk ? `1:${s.reward_risk.toFixed(2)}` : "—";
    $("sbScore").textContent = `${Math.round(s.score)}/100`;

    const opp = s.opportunity || {};
    const badge = $("sbOpportunity");
    badge.textContent = opp.classification || "—";
    badge.className = "sb-opportunity"
      + (opp.classification === "Major" || opp.classification === "Strong" ? " major" : "")
      + (opp.qualifies === false ? " short" : "");
    badge.title = opp.headline || "";
  }

  // ------------------------------------------------------------ signal card

  // Lifecycle stages in the order the specification defines them, so the
  // progress rail reads left to right as the setup matures.
  const SIGNAL_STAGES = ["DEVELOPING", "WATCH", "IN ZONE", "WAITING FOR CONFIRMATION",
                         "CONFIRMED", "ACTIVE", "TARGET HIT"];

  function renderSignal(suggestion) {
    renderBanner(suggestion);
    const host = $("signalBody");
    if (!suggestion) {
      host.innerHTML = `<p class="muted">No setup is being tracked yet. One appears once
        the evidence is strong enough to be worth watching.</p>`;
      $("signalCard").className = "card signal-card";
      return;
    }

    const s = suggestion;
    const dir = s.direction === "bullish" ? "BUY" : "SELL";
    const zone = s.entry_zone;
    $("signalCard").className = `card signal-card ${s.direction} status-${s.status.replace(/ /g, "-").toLowerCase()}`;

    const stageIndex = SIGNAL_STAGES.indexOf(s.status);
    const rail = SIGNAL_STAGES.map((stage, i) => {
      const state = s.status === stage ? "now"
        : (stageIndex >= 0 && i < stageIndex ? "done" : "todo");
      return `<span class="stage ${state}" title="${escapeHtml(stage)}"></span>`;
    }).join("");

    const terminal = TERMINAL_STATES.includes(s.status);

    // When this signal fired, and into what. A setup that looks identical on
    // paper is a different proposition at the London open than it is in thin
    // Asian hours, so the session sits next to the time rather than being
    // buried in the Trader tab.
    const firedStrip = firedLine(s);

    // A decided setup leads with its result. Everything below it -- zone,
    // trigger, targets -- is the record of how it got there.
    const resultBlock = terminal && s.entered ? `
      <div class="signal-result ${WON_STATES.includes(s.status) ? "win" : "loss"}">
        <span class="res-figure">${s.result_pips >= 0 ? "+" : ""}${Math.round(s.result_pips)} pips</span>
        <span class="res-r">${s.r_multiple >= 0 ? "+" : ""}${Number(s.r_multiple).toFixed(2)}R</span>
        <span class="res-detail">in ${fmt(s.fill_price)} → out ${fmt(s.exit_price)}
          on ${escapeHtml(s.exit_reason || "")}</span>
      </div>` : terminal ? `
      <div class="signal-result untaken">
        <span class="res-figure">never filled</span>
        <span class="res-detail">${escapeHtml(s.exit_note
            || "the setup was decided before an entry existed — nothing was risked")}</span>
      </div>` : "";

    const targets = (s.targets || []).map((t) => `
      <div class="kv target ${t.hit ? "hit" : ""} ${t.below_minimum ? "short" : ""}">
        <span>${escapeHtml(t.level)}${t.hit ? " ✓" : ""}</span>
        <b>${fmt(t.price)}
           <em class="pips">${t.pips ? `${Math.round(t.pips)} pips` : ""}</em>
           ${t.reward_risk ? `<em>1:${t.reward_risk.toFixed(2)}</em>` : ""}</b>
      </div>
      <div class="target-basis">${escapeHtml(t.basis)}</div>`).join("");

    // The day-trading opportunity grade sits above the targets: it is the
    // first thing that decides whether a setup is worth reading at all.
    const opp = s.opportunity || {};
    const oppBadge = opp.classification ? `
      <div class="opportunity ${opp.qualifies ? "qualifies" : "short"}">
        <span class="opp-grade">${escapeHtml(opp.classification)}</span>
        <span class="opp-detail">${escapeHtml(opp.headline || "")}</span>
      </div>` : "";

    const outstanding = (s.trigger.outstanding || []);

    host.innerHTML = `
      ${firedStrip}
      <div class="signal-head">
        <span class="signal-dir ${s.direction}">${dir}</span>
        <span class="signal-setup">${escapeHtml(s.setup_label)}</span>
        <span class="signal-quality q-${s.quality.replace(/ /g, "-").toLowerCase()}">
          ${Math.round(s.score)}/100 · ${escapeHtml(s.quality)}</span>
      </div>

      <div class="signal-strategy">
        <span class="strategy-name">${escapeHtml(s.strategy_name || "")}</span>
        <span class="strategy-family">${escapeHtml(s.family || "")}</span>
        <span class="strategy-scores" title="the strategy's own read, and the confluence score for this direction">
          pattern ${Math.round(s.conviction || 0)} · context ${Math.round(s.context_score || 0)}</span>
      </div>
      ${(s.confirmations || []).length ? `
        <div class="confirmations">also seen by
          ${s.confirmations.map((c) => `<b>${escapeHtml(c.name)}</b>`).join(", ")}</div>` : ""}

      <div class="signal-status ${terminal ? "terminal" : ""}">
        <span class="status-name">${escapeHtml(s.status)}</span>
        <span class="stage-rail">${rail}</span>
      </div>
      ${resultBlock}

      ${zone ? `
        <div class="kv"><span>Entry zone</span><b>${fmt(zone.low)} – ${fmt(zone.high)}</b></div>
        <div class="kv"><span>Preferred</span><b>${fmt(zone.preferred)}</b></div>
        <div class="zone-why">${escapeHtml(s.entry_rationale || "")}
          ${zone.price_inside ? `<span class="in-zone">price is in the zone</span>`
            : `<span class="muted">${zone.distance_atr.toFixed(1)} ATR away</span>`}</div>`
        : `<div class="muted small">No entry zone could be derived yet.</div>`}

      <div class="kv"><span>Trigger</span><b class="${s.trigger.met ? "met" : "unmet"}">
        ${s.trigger.met ? "met" : "waiting"}</b></div>
      <div class="trigger-detail">${escapeHtml(s.trigger.description)}</div>
      ${outstanding.length ? `<ul class="waiting">${outstanding
        .map((o) => `<li>${escapeHtml(o)}</li>`).join("")}</ul>` : ""}

      <div class="kv"><span>Invalidation</span><b>${fmt((s.invalidation || {}).price)}</b></div>
      <div class="target-basis">${escapeHtml((s.invalidation || {}).basis || "")}</div>

      ${oppBadge}
      ${targets}

      <div class="kv signal-rr"><span>Reward / risk</span>
        <b>${s.reward_risk ? `1:${s.reward_risk.toFixed(2)}` : "—"}</b></div>

      ${s.risks && s.risks.length
        ? `<ul class="warnings">${s.risks.slice(0, 2)
            .map((r) => `<li class="warn-medium">${escapeHtml(r)}</li>`).join("")}</ul>`
        : ""}

      <details class="signal-history">
        <summary>Setup history (${(s.history || []).length})</summary>
        <ul>${(s.history || []).slice().reverse().map((h) => `
          <li><span class="time" title="${escapeHtml(fmtFull(h.time))}">${fmtClock(h.time)}</span>
              ${escapeHtml(h.status)} · ${Math.round(h.score)} — ${escapeHtml(h.note)}</li>`)
          .join("")}</ul>
      </details>`;
  }

  // ------------------------------------------------------------ dashboard

  /*
   * The tape.
   *
   * Rendered from analysis.micro, which the server aggregates out of the raw
   * tick stream. When the broker serves no tick history the card says so
   * plainly rather than showing zeroes: a flat tape and a blind one look
   * identical in numbers and mean opposite things.
   */
  function renderTape(micro) {
    const host = $("tapeBody");
    if (!host) return;
    const card = $("tapeCard");
    if (!micro || !micro.available) {
      if (card) card.className = "card tape-card blind";
      host.innerHTML = `<p class="muted">${escapeHtml(
        (micro && (micro.note || micro.reason)) || "no tick data")}</p>`;
      return;
    }

    const thrust = Number(micro.thrust || 0);
    const side = thrust > 0.12 ? "bull" : thrust < -0.12 ? "bear" : "";
    if (card) card.className = "card tape-card " + (side || "flat");

    // The speed word is a second opinion on the same thing as the state, and
    // on a quiet tape both come out as the literal word "quiet". Drop it when
    // it is already contained in what the state says.
    const state = String(micro.state || "");
    const speed = String(micro.speed || "");
    const showSpeed = speed && !state.toLowerCase().includes(speed.toLowerCase());

    const bars = (micro.timeframes || {});
    const rungs = ["S5", "S15", "S30"].filter((tf) => bars[tf] && bars[tf].last);

    host.innerHTML = `
      <div class="tape-head">
        <span class="tape-state ${side}">${escapeHtml(micro.state || "")}</span>
        ${showSpeed ? `<span class="tape-speed">${escapeHtml(speed)}</span>` : ""}
      </div>
      <div class="tape-meter" title="Push over the last 30 seconds">
        <div class="tape-zero"></div>
        <div class="tape-fill ${side}" style="width:${Math.min(50, Math.abs(thrust) * 50)}%;
          ${thrust >= 0 ? "left:50%" : "right:50%"}"></div>
      </div>
      <div class="kv"><span>Velocity</span><b>${micro.velocity_pips_min >= 0 ? "+" : ""}${
        fmt(micro.velocity_pips_min, 1)} pips/min</b></div>
      <div class="kv"><span>Range on the tape</span><b>${fmt(micro.range_pips, 1)} pips</b></div>
      <div class="kv"><span>Where in that range</span><b>${pct(micro.pressure)}</b></div>
      <div class="kv"><span>Directional efficiency</span><b>${pct(micro.efficiency)}</b></div>
      <div class="kv"><span>Spread</span><b class="${micro.spread_wide ? "unmet" : "met"}">${
        fmt(micro.spread_now, 2)} <em>${fmt(micro.spread_ratio, 1)}x normal</em></b></div>
      <div class="kv"><span>Ticks a minute</span><b>${fmt(micro.tick_rate, 0)}</b></div>
      ${rungs.length ? `<div class="tape-rungs">` + rungs.map((tf) => `
        <div class="rung"><span>${tf}</span>
          <b class="${bars[tf].last.close >= bars[tf].last.open ? "bull" : "bear"}">${
            fmt(bars[tf].last.close)}</b>
          <em>${fmt(bars[tf].last.range_pips, 1)}p · ${bars[tf].last.ticks} ticks</em>
        </div>`).join("") + `</div>` : ""}
      <p class="tape-note muted">${escapeHtml(micro.note || "")}</p>`;
  }

  function renderDecision(snapshot) {
    const d = snapshot.decision;
    if (!d) return;

    $("price").textContent = fmt(d.price);
    $("verdictAction").textContent = d.decision;
    $("verdictAction").className = "verdict-action " + d.decision.toLowerCase();
    $("scoreFill").style.width = Math.max(2, d.score) + "%";
    $("scoreFill").className = "score-fill " + d.direction;
    $("scoreText").textContent = `${Math.round(d.score)}/100`;
    $("headline").textContent = d.headline;

    $("regimeChip").textContent = "regime · " + d.regime.regime;
    $("biasChip").textContent = "bias · " + d.direction;
    $("rrChip").textContent = "R:R · " + (d.risk.reward_risk ? d.risk.reward_risk.toFixed(2) : "—");

    const tick = snapshot.analysis && snapshot.analysis.tick;
    $("spread").textContent = tick ? `spread ${fmt(tick.spread)}` : "spread —";
    $("atrLabel").textContent = `ATR ${fmt(d.risk.atr)}`;

    fillList($("reasons"), d.reasons, (r) => `<li>${escapeHtml(r)}</li>`);
    fillList($("waitingFor"), d.waiting_for, (w) => `<li>${escapeHtml(w)}</li>`,
             "nothing outstanding");

    $("checklist").innerHTML = (d.confirmation_checklist || [])
      .map((c) => `<li class="${c.met ? "met" : "unmet"}">
          <span class="tick">${c.met ? "✓" : "○"}</span>
          <span>${escapeHtml(c.condition)}${c.required ? "" : " <em>(optional)</em>"}</span>
        </li>`).join("");

    const inv = d.invalidation || {};
    $("invalidation").textContent = inv.price
      ? `${fmt(inv.price)} — ${inv.basis || ""}` : "—";
    $("targets").textContent = (d.targets || []).length
      ? d.targets.map((t) => fmt(t.price)).join("  ·  ") : "—";

    $("warnings").innerHTML = (d.risk.warnings || [])
      .map((w) => `<li class="warn-${w.severity}">${escapeHtml(w.message)}</li>`).join("");

    renderConfluence(d);
  }

  function renderConfluence(d) {
    const block = d.confluence[d.direction];
    if (!block) return;
    const rows = [];
    for (const e of block.evidence) {
      const pct = Math.min(100, (e.points / e.max_points) * 100);
      rows.push(`<div class="conf-row">
        <div class="conf-label">${escapeHtml(e.label)}</div>
        <div class="conf-bar"><div class="conf-fill pos" style="width:${pct}%"></div></div>
        <div class="conf-points">+${e.points.toFixed(1)}</div>
        <div class="conf-detail">${escapeHtml(e.detail)}</div>
      </div>`);
    }
    for (const p of block.penalties) {
      rows.push(`<div class="conf-row">
        <div class="conf-label">${escapeHtml(p.label)}</div>
        <div class="conf-bar"><div class="conf-fill neg" style="width:${Math.min(100, p.points * 6)}%"></div></div>
        <div class="conf-points neg">−${p.points.toFixed(1)}</div>
        <div class="conf-detail">${escapeHtml(p.detail)}</div>
      </div>`);
    }
    rows.push(`<div class="conf-total">
      Raw ${block.raw_score.toFixed(1)} − deductions ${block.deductions.toFixed(1)}
      = <b>${block.score.toFixed(1)}/100</b> · opposing case ${d.opposing_score.toFixed(1)}</div>`);
    $("confluence").innerHTML = rows.join("");
  }

  async function renderDashboardTable() {
    let data;
    try {
      data = await api("/api/dashboard");
    } catch (err) {
      // A refresh can race a server restart; the next tick will retry.
      return;
    }
    if (!data.ready) return;
    $("dashboardTable").querySelector("tbody").innerHTML = data.rows.map((r) => `
      <tr><td>${escapeHtml(r.label)}</td>
          <td class="${biasClass(r.value)}">${escapeHtml(String(r.value))}</td>
          <td class="muted">${escapeHtml(r.detail || "")}</td></tr>`).join("");
  }

  function biasClass(value) {
    const v = String(value).toLowerCase();
    if (v.startsWith("bullish") || v === "buy") return "bull";
    if (v.startsWith("bearish") || v === "sell") return "bear";
    return "";
  }

  function renderUpdates(updates) {
    fillList($("updates"), updates, (u) => {
      const time = fmtClock(u.time);
      return `<li><span class="time">${time}</span> ${escapeHtml(u.message)}</li>`;
    }, "no changes yet");
  }

  function renderStatus(status) {
    const ok = status.connected && !status.error;
    $("status").className = "status " + (ok ? "ok" : "bad");
    $("statusText").textContent = status.error
      ? status.error.slice(0, 70)
      : status.connected ? "live" : "disconnected";
    $("symbolLabel").textContent = status.symbol
      ? `${status.symbol} · ${status.ai_available ? "AI analyst online" : "offline explanations"}`
      : "connecting…";
  }

  // ----------------------------------------------------------------- data

  function applySnapshot(snapshot) {
    if (!snapshot || !snapshot.ready) {
      if (snapshot && snapshot.status) renderStatus(snapshot.status);
      return;
    }
    state.snapshot = snapshot;
    renderStatus(snapshot.status);
    renderDecision(snapshot);
    renderSignal(snapshot.suggestion);
    renderTape((snapshot.analysis || {}).micro);
    renderBook(snapshot.book);
    renderResolved(snapshot.book);
    announceAll(snapshot.suggestion, snapshot.book);
    renderTrader(snapshot.trader);
    renderUpdates(snapshot.updates);
    if (snapshot.explanation && snapshot.explanation.text) {
      setExplanation(snapshot.explanation);
    }
  }

  function setExplanation(explanation) {
    const tag = explanation.source === "claude"
      ? `<span class="src">AI analyst</span>`
      : `<span class="src offline">offline explanation</span>`;
    $("explanation").innerHTML = tag + paragraphs(explanation.text);
  }

  function connectSocket() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.ready !== undefined) {
        applySnapshot(message);
      } else if (message.type === "analysis") {
        if (state.snapshot) {
          state.snapshot.decision = message.decision;
          state.snapshot.status = message.status;
          state.snapshot.suggestion = message.suggestion;
          state.snapshot.book = message.book;
          state.snapshot.trader = message.trader;
          if (message.micro && state.snapshot.analysis) {
            state.snapshot.analysis.micro = message.micro;
          }
          renderStatus(message.status);
          renderDecision(state.snapshot);
          renderSignal(message.suggestion);
          renderTape(message.micro);
          renderBook(message.book);
          renderResolved(message.book);
          announceAll(message.suggestion, message.book);
          renderTrader(message.trader);
        }
        // Refresh the chart at most every 20s; candles change far more slowly
        // than the tick-driven decision panel.
        if (Date.now() - state.lastChartFetch > 20000) loadChart();
      } else if (message.type === "signal") {
        if (state.snapshot) state.snapshot.suggestion = message.suggestion;
        renderSignal(message.suggestion);
        announce(message.suggestion);
        const list = (state.snapshot && state.snapshot.updates) || [];
        list.unshift(message.update);
        renderUpdates(list.slice(0, 20));
      } else if (message.type === "desk") {
        if (message.trader) renderTrader(message.trader);
        const deskList = (state.snapshot && state.snapshot.updates) || [];
        deskList.unshift(message.update);
        renderUpdates(deskList.slice(0, 20));
      } else if (message.type === "update") {
        const list = (state.snapshot && state.snapshot.updates) || [];
        list.unshift(message.update);
        renderUpdates(list.slice(0, 20));
        renderDashboardTable();
      }
    };

    ws.onclose = () => {
      $("statusText").textContent = "reconnecting…";
      setTimeout(connectSocket, 3000);
    };
  }


  // ------------------------------------------------- trader behaviour layer

  /*
   * Everything the desk works out around the analysis: which session it is,
   * whether the day is behaving, whether the setup obeys the top-down method,
   * the standing view, the dollar, today's running record, and what to do with
   * a position that is already open.
   *
   * None of it withholds a setup. Conviction only ever suggests a size.
   */

  function renderTrader(trader) {
    if (!trader || !trader.verdict) return;
    state.trader = trader;

    const v = trader.verdict;
    $("deskHeadline").textContent = v.headline || "—";
    $("deskSession").textContent = "session " + ((trader.session || {}).label || "—");
    $("deskCharacter").textContent = "day " + ((trader.character || {}).verdict || "—");
    $("deskConviction").textContent = "conviction " + (v.conviction ?? "—");
    $("deskSize").textContent = v.risk_label || "—";

    fillList($("deskFactors"), v.factors || [], (f) =>
      "<li><b>" + escapeHtml(f.factor) + "</b>"
      + "<span class='mult " + (f.multiplier < 1 ? "down" : f.multiplier > 1 ? "up" : "") + "'>x"
      + escapeHtml(f.multiplier) + "</span>"
      + "<span class='muted'>" + escapeHtml(f.note || "") + "</span></li>",
      "nothing is modifying conviction");

    renderTopdown(trader.topdown);
    renderThesis(trader.thesis);
    renderCharacter(trader.character, trader.session);
    renderMacro(trader.macro);
    renderRecord(trader.record);
    renderMemory(trader.memory);
    renderManagement(trader.management);
    renderDeskChips(trader);
  }

  // The banner carries the two things worth seeing without changing tab.
  function renderDeskChips(trader) {
    const v = (trader && trader.verdict) || {};
    const conviction = $("sbConviction");
    if (conviction) {
      conviction.textContent = v.conviction !== undefined
        ? (v.conviction + " · " + (v.risk_label || "")).trim() : "—";
      conviction.className = "sb-conviction"
        + (v.risk_fraction === 1 ? " full" : v.risk_fraction === 0.5 ? " half" : " light");
    }
    const method = $("sbMethod");
    const td = (trader && trader.topdown) || {};
    if (method) {
      method.textContent = td.compliance || "—";
      method.className = "sb-method"
        + (td.compliance === "with bias" ? " ok"
           : td.compliance === "counter-bias" ? " warn" : "");
      method.title = td.headline || "";
    }
  }

  function renderTopdown(td) {
    const host = $("topdownBody");
    if (!host) return;
    if (!td) { host.innerHTML = "<p class='muted'>—</p>"; return; }

    const reads = (obj) => Object.entries(obj || {}).map(([tf, read]) =>
      "<div class='kv'><span>" + escapeHtml(tf) + "</span><b class='"
      + biasClass(read) + "'>" + escapeHtml(read) + "</b></div>").join("");

    let html =
      "<p class='headline " + (td.compliance === "counter-bias" ? "warn" : "") + "'>"
      + escapeHtml(td.headline || "") + "</p>"
      + "<div class='kv'><span>D1/H4 bias</span><b class='" + biasClass(td.bias) + "'>"
      + escapeHtml(td.bias) + " (" + escapeHtml(td.bias_agreement) + ")</b></div>"
      + "<h4>Step 1 — direction (" + escapeHtml((td.bias_timeframes || []).join("/")) + ")</h4>"
      + reads(td.bias_reads)
      + "<h4>Step 2 — execution (" + escapeHtml((td.execution_timeframes || []).join("/")) + ")</h4>"
      + reads(td.execution_reads)
      + "<div class='kv'><span>Timing on</span><b>"
      + escapeHtml(td.execution_timeframe || "none yet") + "</b></div>";

    if ((td.zones || []).length) {
      html += "<h4>Major D1/H4 zones</h4>" + td.zones.map((z) =>
        "<div class='kv'><span>" + escapeHtml(z.timeframe) + " " + escapeHtml(z.kind)
        + " (" + escapeHtml(z.side) + ")</span><b>" + escapeHtml(z.low) + " – "
        + escapeHtml(z.high) + "</b></div>").join("");
    }
    if ((td.notes || []).length) {
      html += "<ul class='warnings'>" + td.notes.map((n) =>
        "<li>" + escapeHtml(n) + "</li>").join("") + "</ul>";
    }
    host.innerHTML = html;
  }

  function renderThesis(t) {
    const host = $("thesisBody");
    if (!host) return;
    if (!t) { host.innerHTML = "<p class='muted'>No view has been written down yet.</p>"; return; }

    let html =
      "<p class='headline'>" + escapeHtml(t.statement || "") + "</p>"
      + "<div class='regime-row'>"
      + "<span class='chip " + biasClass(t.direction) + "'>" + escapeHtml(t.direction) + "</span>"
      + "<span class='chip'>" + escapeHtml(t.conviction) + "</span>"
      + "<span class='chip " + (t.status === "challenged" ? "warn" : "") + "'>"
      + escapeHtml(t.status) + "</span>"
      + "<span class='chip'>held " + escapeHtml(t.age_minutes) + " min</span></div>";

    if (t.status === "challenged") {
      html += "<p class='warn'>Under challenge from a "
        + escapeHtml(t.challenge_direction || "") + " read for "
        + escapeHtml(t.challenged_minutes)
        + " min. The view is not abandoned until structure confirms it.</p>";
    }
    html += "<div class='kv'><span>Wrong if</span><b>"
      + escapeHtml(t.invalidated_if || "—") + "</b></div>";
    if (t.macro_note) html += "<p class='muted'>" + escapeHtml(t.macro_note) + "</p>";
    html += "<h4>Why</h4><ul class='reasons'>"
      + (t.rationale || []).map((r) => "<li>" + escapeHtml(r) + "</li>").join("") + "</ul>";
    if ((t.revisions || []).length) {
      html += "<h4>Revisions</h4>" + t.revisions.map((r) =>
        "<div class='kv'><span>" + escapeHtml(r.from) + " → " + escapeHtml(r.to)
        + "</span><b class='muted'>" + escapeHtml(r.why) + "</b></div>").join("");
    }
    host.innerHTML = html;
  }

  function renderCharacter(c, session) {
    const host = $("characterBody");
    if (!host) return;
    if (!c) { host.innerHTML = "<p class='muted'>—</p>"; return; }
    const m = c.measures || {};
    const verdictClass = c.verdict === "TRADE" ? "bull"
      : c.verdict === "STAND ASIDE" ? "bear" : "warn";

    let html =
      "<p class='headline'>" + escapeHtml(c.headline || "") + "</p>"
      + "<div class='regime-row'>"
      + "<span class='chip " + verdictClass + "'>" + escapeHtml(c.verdict) + "</span>"
      + "<span class='chip'>" + escapeHtml(c.score) + "/100</span>"
      + "<span class='chip'>" + escapeHtml((session || {}).label || "") + "</span></div>"
      + "<div class='kv'><span>Directional efficiency</span><b>" + pct(m.efficiency) + "</b></div>"
      + "<div class='kv'><span>Levels respected</span><b>"
      + (m.level_respect === null || m.level_respect === undefined
          ? "too few (" + escapeHtml(m.level_interactions ?? 0) + ")"
          : pct(m.level_respect) + " of " + escapeHtml(m.level_interactions))
      + "</b></div>"
      + "<div class='kv'><span>Structure reversals</span><b>" + escapeHtml(m.structure_flips) + "</b></div>"
      + "<div class='kv'><span>Volatility</span><b>" + escapeHtml(m.volatility_ratio) + "x median</b></div>"
      + "<div class='kv'><span>Session quality</span><b>" + pct(m.session_quality) + "</b></div>";

    if ((c.reasons || []).length) {
      html += "<ul class='reasons'>" + c.reasons.map((r) =>
        "<li>" + escapeHtml(r) + "</li>").join("") + "</ul>";
    }
    const cautions = (c.cautions || []).concat(((session || {}).notes) || []);
    if (cautions.length) {
      html += "<ul class='warnings'>" + cautions.map((r) =>
        "<li>" + escapeHtml(r) + "</li>").join("") + "</ul>";
    }
    host.innerHTML = html;
  }

  function renderMacro(m) {
    const host = $("macroBody");
    if (!host) return;
    if (!m) { host.innerHTML = "<p class='muted'>Dollar context is not running.</p>"; return; }
    if (!m.available) {
      host.innerHTML = "<p class='muted'>" + escapeHtml(m.headline || "unavailable") + "</p>"
        + (m.note ? "<p class='muted'>" + escapeHtml(m.note) + "</p>" : "");
      return;
    }
    let html =
      "<p class='headline'>" + escapeHtml(m.headline) + "</p>"
      + "<div class='regime-row'><span class='chip "
      + (m.supports === "conflicted" ? "warn" : biasClass(m.supports)) + "'>"
      + escapeHtml(m.relationship) + "</span>"
      + "<span class='chip'>" + escapeHtml(m.symbol)
      + (m.inverted ? " (inverted)" : "") + "</span></div>"
      + "<div class='kv'><span>Dollar today</span><b>" + signed(m.dollar_change_pct) + "%</b></div>"
      + "<div class='kv'><span>Dollar 5-day</span><b>" + signed(m.dollar_5d_pct) + "%</b></div>"
      + "<div class='kv'><span>Gold today</span><b>" + signed(m.gold_change_pct) + "%</b></div>"
      + "<div class='kv'><span>20-day correlation</span><b>"
      + escapeHtml(m.correlation ?? "—") + "</b></div>";
    if ((m.cautions || []).length) {
      html += "<ul class='warnings'>" + m.cautions.map((c) =>
        "<li>" + escapeHtml(c) + "</li>").join("") + "</ul>";
    }
    host.innerHTML = html;
  }

  function renderRecord(r) {
    const host = $("recordBody");
    if (!host) return;
    if (!r) { host.innerHTML = "<p class='muted'>—</p>"; return; }
    const c = r.counters || {};
    host.innerHTML =
      "<p class='headline'>" + escapeHtml(r.headline || "") + "</p>"
      + "<div class='kv'><span>Setups tracked</span><b>" + escapeHtml(c.setups_identified ?? 0) + "</b></div>"
      + "<div class='kv'><span>Entries filled</span><b>" + escapeHtml(c.setups_filled ?? 0) + "</b></div>"
      + "<div class='kv'><span>Result</span><b>" + escapeHtml(c.wins ?? 0) + "W / "
      + escapeHtml(c.losses ?? 0) + "L</b></div>"
      + "<div class='kv'><span>Realised</span><b>" + signed(c.realised_r) + "R</b></div>"
      + (r.observations || []).map((o) =>
          "<div class='observation'><b>" + escapeHtml(o.headline) + "</b>"
          + "<span class='muted'>" + escapeHtml(o.detail) + "</span></div>").join("");
  }

  function renderMemory(mem) {
    const host = $("memoryBody");
    if (!host) return;
    if (!mem) { host.innerHTML = "<p class='muted'>—</p>"; return; }
    const report = mem.report || {};

    const table = (dim, rows) => !rows || !rows.length ? "" :
      "<h4>" + escapeHtml(dim) + "</h4><table class='mini'>"
      + "<thead><tr><th>value</th><th>n</th><th>W/L</th><th>win %</th>"
      + "<th>avg R</th><th></th></tr></thead><tbody>"
      + rows.map((r) =>
          "<tr class='" + (r.reliable ? (r.avg_r >= 0 ? "good" : "bad") : "muted") + "'>"
          + "<td>" + escapeHtml(r.key) + "</td>"
          + "<td>" + escapeHtml(r.n) + "</td>"
          + "<td>" + escapeHtml(r.wins) + "/" + escapeHtml(r.losses) + "</td>"
          + "<td>" + (r.win_rate === null ? "—" : escapeHtml(r.win_rate) + "%") + "</td>"
          + "<td>" + signed(r.avg_r) + "</td>"
          + "<td class='confidence'>" + escapeHtml(r.confidence) + "</td></tr>").join("")
      + "</tbody></table>";

    const dims = report.by_dimension || {};
    host.innerHTML =
      "<p class='headline'>" + escapeHtml(report.headline || mem.headline || "") + "</p>"
      + (mem.headline && mem.headline !== report.headline
          ? "<p class='" + ((mem.warnings || []).length ? "warn" : "muted") + "'>"
            + escapeHtml(mem.headline) + "</p>" : "")
      + "<p class='muted'>A group needs at least " + escapeHtml(report.min_sample ?? 8)
      + " closed setups before any conclusion is drawn from it. Everything below "
      + "states its own sample size.</p>"
      + table("Setup type", dims.setup_type)
      + table("Session", dims.session)
      + table("Market regime", dims.regime)
      + table("Score band", dims.score_band)
      + table("Day character", dims.day_verdict)
      + table("Direction", dims.direction);
  }

  function renderManagement(mgmt) {
    const host = $("manageBody");
    if (!host) return;
    const open = (mgmt && mgmt.open) || [];
    if (!open.length) {
      host.innerHTML = "<p class='muted'>No entry has filled yet. Management "
        + "starts the moment price trades into an entry zone.</p>";
      return;
    }

    host.innerHTML = open.map((t) =>
      "<div class='trade " + escapeHtml(t.direction) + "'>"
      + "<div class='trade-head'><b>" + (t.direction === "bullish" ? "LONG" : "SHORT")
      + " from " + escapeHtml(t.entry) + "</b>"
      + "<span class='r " + (t.r_now >= 0 ? "up" : "down") + "'>" + signed(t.r_now) + "R</span></div>"
      + "<div class='kv'><span>Stop now</span><b>" + escapeHtml(t.current_stop)
      + (t.breakeven_set ? " (breakeven)" : "") + "</b></div>"
      + "<div class='kv'><span>Best / worst</span><b>" + signed(t.mfe_r) + "R / "
      + signed(t.mae_r) + "R</b></div>"
      + "<div class='kv'><span>Position left</span><b>" + pct(t.position_open) + "</b></div>"
      + (t.actions || []).slice().reverse().map((a) =>
          "<div class='action " + escapeHtml(a.urgency) + "'><b>" + escapeHtml(a.headline)
          + "</b><span>" + escapeHtml(a.detail) + "</span></div>").join("")
      + "</div>").join("");
  }

  // ------------------------------------------------------------ setup book

  function renderBook(book) {
    const list = $("setupsList");
    const count = $("setupsCount");
    if (!list) return;
    const live = (book && book.live) || [];
    if (count) count.textContent = String(live.length);

    fillList(list, live, (s) =>
      "<li class='setup " + escapeHtml(s.direction)
      + (s.status === "CONFIRMED" ? " ready" : "")
      + (s.entered ? " filled" : "") + "'>"
      + "<div class='setup-top'>"
      + "<span class='dir'>" + (s.direction === "bullish" ? "BUY" : "SELL") + "</span>"
      + "<span class='setup-name'>" + escapeHtml(s.setup_label) + "</span>"
      + "<span class='score'>" + escapeHtml(Math.round(s.score)) + "</span></div>"
      + "<div class='setup-strategy'>" + escapeHtml(s.strategy_name || "")
      + ((s.confirmations || []).length
          ? " <em>+ " + s.confirmations.length + " agreeing</em>" : "")
      + "</div>"
      + "<div class='setup-meta'>"
      + "<span class='fired' title='fired " + escapeHtml(fmtFull(s.created_at)) + "'>"
      + escapeHtml(s.fired_clock || fmtClock(s.created_at)) + "</span>"
      + "<span class='session-chip small "
      + sessionClass(s.fired_session_quality === undefined ? 0.5 : s.fired_session_quality)
      + "'>" + escapeHtml(s.fired_session_label || "") + "</span>"
      + "<span class='status'>" + escapeHtml(s.status) + "</span>"
      + "<span>" + (s.entry_zone ? escapeHtml(s.entry_zone.label) : "—") + "</span>"
      + "<span>" + ((s.targets || [])[0]
          ? Math.round(s.targets[0].pips) + "p" : "—") + "</span>"
      + "<span>" + (s.reward_risk ? "1:" + escapeHtml(s.reward_risk.toFixed(2)) : "—")
      + "</span></div></li>", "nothing being tracked");
  }

  /*
   * The resolved panel.
   *
   * This is the half of the board that did not exist before: a signal used to
   * stay on the dashboard after it had already hit its target or its stop,
   * with nothing anywhere recording which of the two had happened. A setup now
   * leaves the live list the moment price decides it and appears here with the
   * result attached, which is also what frees the slot for the next one.
   */
  function renderResolved(book) {
    const list = $("resolvedList");
    if (!list) return;
    const retired = (book && book.retired) || [];
    const results = (book && book.results) || {};
    const count = $("resolvedCount");
    if (count) count.textContent = String(results.resolved || retired.length);

    const tape = $("sessionResults");
    if (tape) {
      tape.innerHTML = !results.taken
        ? `<span class="muted">no setup has filled yet this session</span>`
        : `<span class="tape-item win">${results.wins}W</span>`
          + `<span class="tape-item loss">${results.losses}L</span>`
          + (results.scratches
              ? `<span class="tape-item">${results.scratches} scratch</span>` : "")
          + `<span class="tape-item ${results.pips >= 0 ? "win" : "loss"}">`
          + `${results.pips >= 0 ? "+" : ""}${Math.round(results.pips)} pips</span>`
          + `<span class="tape-item ${results.r >= 0 ? "win" : "loss"}">`
          + `${results.r >= 0 ? "+" : ""}${Number(results.r).toFixed(2)}R</span>`
          + (results.win_rate !== null && results.win_rate !== undefined
              ? `<span class="tape-item">${results.win_rate}% won</span>` : "")
          + (results.untaken
              ? `<span class="tape-item muted">${results.untaken} never filled</span>` : "");
    }

    fillList(list, retired.slice(0, 12), (s) => {
      const won = WON_STATES.includes(s.status);
      const klass = !s.entered ? "untaken" : won ? "win" : "loss";
      return "<li class='resolved " + klass + "'>"
        + "<div class='resolved-top'>"
        + "<span class='dir'>" + (s.direction === "bullish" ? "BUY" : "SELL") + "</span>"
        + "<span class='setup-name'>" + escapeHtml(s.setup_label) + "</span>"
        + "<span class='verdict'>" + (!s.entered ? "no fill"
            : (s.result_pips >= 0 ? "+" : "") + Math.round(s.result_pips) + "p") + "</span>"
        + "</div><div class='setup-meta'>"
        + "<span class='fired'>" + escapeHtml(s.fired_clock || fmtClock(s.created_at))
        + (s.exit_clock && s.exit_clock !== "\u2014" ? "\u2192" + escapeHtml(s.exit_clock) : "")
        + "</span>"
        + "<span>" + escapeHtml(s.strategy_name || "") + "</span>"
        + "<span>" + escapeHtml(s.status) + "</span>"
        + "<span>" + (s.entered
            ? (s.r_multiple >= 0 ? "+" : "") + Number(s.r_multiple).toFixed(2) + "R" : "—")
        + "</span></div></li>";
    }, "nothing has resolved yet");
  }

  /*
   * The "fired at" strip.
   *
   * Rendered from the fields the server froze onto the signal when it was
   * identified, falling back to formatting `created_at` locally for anything
   * recorded before those fields existed. The session is deliberately the one
   * that was running *then*, not the one running now.
   */
  function firedLine(s) {
    if (!s) return "";
    const clock = s.fired_clock || fmtClock(s.created_at);
    const date = s.fired_date || fmtDate(s.created_at);
    const zone = s.clock_offset || state.clock.label;
    const session = s.fired_session_label || sessionAt(asDate(s.created_at)).label;
    const quality = s.fired_session_quality !== undefined && s.fired_session_quality !== null
      ? s.fired_session_quality : 0.5;
    const today = date === fmtDate(new Date());
    return `<div class="fired-strip" title="Identified ${escapeHtml(fmtFull(s.created_at))}">
      <span class="fired-clock">${escapeHtml(clock)}</span>
      <span class="fired-zone">${escapeHtml(zone)}</span>
      ${today ? "" : `<span class="fired-date">${escapeHtml(date)}</span>`}
      <span class="session-chip ${sessionClass(quality)}">${escapeHtml(session)}</span>
      ${s.filled_clock && s.filled_clock !== "—"
        ? `<span class="fired-leg">filled ${escapeHtml(s.filled_clock)}</span>` : ""}
      ${s.exit_clock && s.exit_clock !== "—"
        ? `<span class="fired-leg">closed ${escapeHtml(s.exit_clock)}</span>` : ""}
    </div>`;
  }

  /* ------------------------------------------------------------ signals tab
   *
   * The log of what this system has actually called. It reads its own endpoint
   * rather than the live book because the book only holds what is still
   * running: a signal that fired at 09:14 and stopped out at 10:02 has left
   * the board entirely, and it is precisely that signal a trader reviews.
   */
  async function loadFired() {
    let data;
    try {
      data = await api("/api/fired" + firedQuery());
    } catch (err) {
      console.error(err);
      return;
    }

    renderFiredFilters(data);
    renderFiredStats(data.summary || {});
    renderFiredTable(data.signals || []);
    renderFiredBreakdown(data.summary || {});
  }

  function firedQuery() {
    const f = state.fired;
    const parts = [];
    if (f.date) parts.push("date=" + encodeURIComponent(f.date));
    if (f.side) parts.push("direction=" + encodeURIComponent(f.side));
    if (f.outcome) parts.push("outcome=" + encodeURIComponent(f.outcome));
    if (f.session) parts.push("session=" + encodeURIComponent(f.session));
    return parts.length ? "?" + parts.join("&") : "";
  }

  function renderFiredFilters(data) {
    const dates = data.dates || [];
    const host = $("firedDateFilters");
    if (host) {
      const today = fmtDate(new Date());
      host.innerHTML = [`<button class="filter ${state.fired.date ? "" : "active"}"
          data-date="">All days</button>`]
        .concat(dates.map((d) => `<button
          class="filter ${state.fired.date === d.date ? "active" : ""}"
          data-date="${escapeHtml(d.date)}">${d.date === today ? "Today"
            : escapeHtml(fmtDayShort(d.date + "T12:00:00Z"))}
          <em>${d.n}</em></button>`)).join("");
    }

    const sessions = $("firedSessionFilters");
    if (sessions && !sessions.dataset.built) {
      sessions.innerHTML = [`<button class="filter active" data-session="">Any session</button>`]
        .concat((data.sessions || []).map((w) => `<button class="filter"
          data-session="${escapeHtml(w.name)}"
          title="${escapeHtml(w.description)} · ${escapeHtml(w.window)} ${escapeHtml(state.clock.label)}"
          >${escapeHtml(w.label)}</button>`)).join("");
      sessions.dataset.built = "1";
    }
  }

  function renderFiredStats(s) {
    const host = $("firedStats");
    if (!host) return;
    host.innerHTML =
      stat("Signals fired", s.fired ?? 0)
      + stat("BUY / SELL", `${s.buys ?? 0} / ${s.sells ?? 0}`)
      + stat("Still running", s.open ?? 0)
      + stat("Filled", s.filled ?? 0)
      + stat("Won / lost", `${s.wins ?? 0} / ${s.losses ?? 0}`)
      + stat("Win rate", s.win_rate === null || s.win_rate === undefined
          ? "—" : s.win_rate + "%")
      + stat("Net pips", (s.pips ?? 0) >= 0
          ? "+" + Math.round(s.pips ?? 0) : Math.round(s.pips))
      + stat("Avg pips offered", s.avg_target_pips
          ? Math.round(s.avg_target_pips) : "—");
  }

  function renderFiredTable(rows) {
    const body = $("firedTable").querySelector("tbody");
    body.innerHTML = rows.map((r) => {
      const open = !r.closed_at;
      const klass = open ? "" : r.outcome === "win" ? "bull"
        : r.outcome === "loss" ? "bear" : "muted";
      const zone = r.entry_low && r.entry_high
        ? `${fmt(r.entry_low)}–${fmt(r.entry_high)}` : "—";
      const result = open
        ? `<span class="pill running">running</span>`
        : r.outcome === "untaken" || !r.filled_at
          ? `<span class="pill muted">never filled</span>`
          : `<span class="pill ${r.outcome === "win" ? "win" : "loss"}">`
            + `${(r.result_pips ?? 0) >= 0 ? "+" : ""}${Math.round(r.result_pips ?? 0)}p`
            + `${r.r_multiple !== null && r.r_multiple !== undefined
                ? ` · ${(r.r_multiple >= 0 ? "+" : "")}${Number(r.r_multiple).toFixed(2)}R` : ""}</span>`;
      return `<tr>
        <td class="muted">${escapeHtml(r.fired_date || fmtDate(r.fired_at))}</td>
        <td class="fired-cell" title="${escapeHtml(fmtFull(r.fired_at))}">
          <b>${escapeHtml(r.fired_clock || fmtClock(r.fired_at))}</b>
          <span class="zone-tag">${escapeHtml(r.clock_offset || state.clock.label)}</span></td>
        <td><span class="session-chip ${sessionClass(r.session_quality ?? 0.5)}"
          >${escapeHtml(r.session_label || "—")}</span></td>
        <td>${escapeHtml(r.strategy_name || r.strategy || "—")}
          <span class="family-tag">${escapeHtml(r.family || "")}</span></td>
        <td class="${biasClass(r.direction)}">${r.direction === "bullish" ? "BUY" : "SELL"}</td>
        <td>${zone}</td>
        <td>${fmt(r.stop)}</td>
        <td>${fmt(r.tp1)}</td>
        <td title="${escapeHtml(r.opportunity || "")}">${r.first_target_pips
            ? Math.round(r.first_target_pips) + "p" : "—"}${r.max_pips
            && r.max_pips > (r.first_target_pips || 0) * 1.2
            ? ` <span class="muted">→ ${Math.round(r.max_pips)}p</span>` : ""}</td>
        <td>${r.reward_risk ? "1:" + Number(r.reward_risk).toFixed(2) : "—"}</td>
        <td>${Math.round(r.score ?? 0)}</td>
        <td class="muted">${escapeHtml(r.last_status || r.status || "")}</td>
        <td>${result}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="13" class="muted">
      No signal has fired yet for this filter. Every setup any strategy
      identifies is logged here the moment it appears.</td></tr>`;
  }

  function renderFiredBreakdown(summary) {
    const rows = (items, key) => (items || []).length
      ? `<div class="kv-list">` + items.map((i) => `<div class="kv">
          <span>${escapeHtml(i[key] || "—")}</span><b>${i.n}</b></div>`).join("") + `</div>`
      : `<p class="muted">—</p>`;
    $("firedBySession").innerHTML = rows(summary.by_session, "session");
    $("firedByStrategy").innerHTML = rows(summary.by_strategy, "strategy");
  }

  function initFiredFilters() {
    const bind = (hostId, attr, key) => {
      const host = $(hostId);
      if (!host) return;
      host.addEventListener("click", (event) => {
        const button = event.target.closest("button.filter");
        if (!button) return;
        host.querySelectorAll("button.filter").forEach((b) =>
          b.classList.toggle("active", b === button));
        state.fired[key] = button.dataset[attr] || "";
        loadFired();
      });
    };
    bind("firedDateFilters", "date", "date");
    bind("firedSideFilters", "side", "side");
    bind("firedOutcomeFilters", "outcome", "outcome");
    bind("firedSessionFilters", "session", "session");
  }

  function pct(value) {
    return value === null || value === undefined ? "—" : Math.round(value * 100) + "%";
  }

  function signed(value) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    return (n >= 0 ? "+" : "") + n.toFixed(2);
  }

  // ------------------------------------------------------------- profiles

  async function loadProfiles() {
    const data = await api("/api/profiles");
    const options = data.profiles
      .map((p) => `<option value="${escapeHtml(p.name)}" ${p.name === data.active ? "selected" : ""}>${escapeHtml(p.name)}</option>`)
      .join("");
    $("profileSelect").innerHTML = options;
    $("backtestProfile").innerHTML = options;
  }

  // ----------------------------------------------------------------- tabs

  function initTabs() {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
        const view = tab.dataset.view;
        document.querySelectorAll(".view").forEach((v) => {
          v.classList.toggle("active", v.id === "view-" + view);
        });
        if (view === "terminal") state.chart.resize();
        if (view === "analysis") renderDashboardTable();
        if (view === "trader") loadTrader();
        if (view === "signals") loadFired();
        if (view === "history") { loadOutcomes(); loadSignals(); }
        if (view === "journal") loadJournal();
        if (view === "backtest") loadBacktests();
      });
    });
  }

  async function loadTrader() {
    try {
      const data = await api("/api/trader");
      if (data && data.ready) renderTrader(data);
    } catch (_) { /* the desk is optional; the terminal still works */ }
  }

  // -------------------------------------------------------------- history

  /*
   * Setup results.
   *
   * The old history tab listed decisions -- what the engine said. This lists
   * outcomes: what each signal was worth once price had finished with it. The
   * two are different questions, and only the second one can tell the trader
   * whether the application is any good.
   */
  const EXIT_LABELS = {
    target: "target hit",
    stop: "stop hit",
    breakeven: "closed at breakeven",
    expiry: "time exit",
    cancelled: "premise gone",
    structure: "invalidated before entry",
  };

  async function loadOutcomes() {
    let data;
    try {
      data = await api("/api/outcomes");
    } catch (_) {
      return;
    }
    const s = data.summary || {};
    $("outcomeStats").innerHTML =
      stat("Setups decided", s.recorded ?? 0)
      + stat("Filled", s.taken ?? 0)
      + stat("Never filled", s.untaken ?? 0)
      + stat("Wins", s.wins ?? 0)
      + stat("Losses", s.losses ?? 0)
      + stat("Win rate", s.win_rate === null || s.win_rate === undefined ? "—" : s.win_rate + "%")
      + stat("Net pips", (s.pips ?? 0) >= 0 ? "+" + Math.round(s.pips ?? 0) : Math.round(s.pips))
      + stat("Net R", Number(s.r ?? 0).toFixed(2));

    $("outcomesTable").querySelector("tbody").innerHTML =
      (data.outcomes || []).map((o) => {
        const filled = !!o.entered;
        const won = o.outcome === "win";
        const klass = !filled ? "muted" : won ? "bull" : o.outcome === "loss" ? "bear" : "";
        return `<tr>
          <td class="muted" title="${escapeHtml(fmtFull(o.closed_at))}">${fmtStamp(o.closed_at)}</td>
          <td>${escapeHtml(o.strategy_name || o.strategy || o.setup_type || "—")}</td>
          <td class="${biasClass(o.direction)}">${escapeHtml(o.direction || "")}</td>
          <td>${filled ? fmt(o.fill_price ?? o.entry) : "—"}</td>
          <td>${filled ? fmt(o.exit_price) : "—"}</td>
          <td class="muted">${escapeHtml(EXIT_LABELS[o.exit_reason] || o.exit_reason || o.reason || "")}</td>
          <td class="${klass}">${filled && o.result_pips !== null && o.result_pips !== undefined
              ? (o.result_pips >= 0 ? "+" : "") + Math.round(o.result_pips) : "—"}</td>
          <td class="${klass}">${filled && o.r_multiple !== null && o.r_multiple !== undefined
              ? (o.r_multiple >= 0 ? "+" : "") + Number(o.r_multiple).toFixed(2) : "—"}</td>
          <td class="${klass}">${escapeHtml(filled ? (o.outcome || "") : "untaken")}</td>
        </tr>`;
      }).join("") || `<tr><td colspan="9" class="muted">
        No setup has been decided yet. Results appear here the moment a signal
        reaches its target or its stop.</td></tr>`;

    const live = ((state.snapshot || {}).book || {}).counts || {};
    const liveByStrategy = live.by_strategy || {};
    $("strategyTable").querySelector("tbody").innerHTML =
      (data.strategies || []).map((r) => `<tr>
        <td>${escapeHtml(r.strategy_name || r.strategy)}</td>
        <td class="muted">${escapeHtml(r.family || "")}</td>
        <td>${r.n}</td>
        <td>${r.wins || 0}/${r.losses || 0}</td>
        <td>${r.n ? Math.round(((r.wins || 0) / Math.max(1, (r.wins || 0) + (r.losses || 0))) * 100) + "%" : "—"}</td>
        <td class="${(r.avg_r || 0) >= 0 ? "bull" : "bear"}">${Number(r.avg_r || 0).toFixed(2)}</td>
        <td class="${(r.total_pips || 0) >= 0 ? "bull" : "bear"}">${Math.round(r.total_pips || 0)}</td>
        <td class="muted">${liveByStrategy[r.strategy] || 0}</td>
      </tr>`).join("") || `<tr><td colspan="8" class="muted">
        No strategy has a filled setup on record yet.</td></tr>`;
  }

  async function loadSignals() {
    const query = state.signalFilter ? `?decision=${state.signalFilter}` : "";
    const data = await api("/api/signals" + query);
    $("signalsTable").querySelector("tbody").innerHTML = data.signals.map((s) => `
      <tr>
        <td class="muted" title="${escapeHtml(fmtFull(s.created_at))}">${fmtStamp(s.created_at)}</td>
        <td>${fmt(s.price)}</td>
        <td class="${s.decision.toLowerCase()}">${s.decision}</td>
        <td class="${biasClass(s.direction)}">${s.direction}</td>
        <td>${fmt(s.score, 0)}</td>
        <td class="muted">${escapeHtml(s.regime || "")}</td>
        <td>${s.invalidation ? fmt(s.invalidation) : "—"}</td>
        <td>${s.reward_risk ? fmt(s.reward_risk) : "—"}</td>
        <td class="muted">${escapeHtml(s.result || "")}</td>
      </tr>`).join("") || `<tr><td colspan="9" class="muted">No signals recorded yet.</td></tr>`;
  }

  // -------------------------------------------------------------- journal

  async function loadJournal() {
    const data = await api("/api/journal");
    const s = data.stats;
    $("journalStats").innerHTML = `
      ${stat("Entries", s.entries)}
      ${stat("Trades taken", s.taken)}
      ${stat("Closed", s.closed)}
      ${stat("Win rate", s.win_rate === null ? "—" : s.win_rate + "%")}
      ${stat("Total P/L", s.total_profit)}
      ${stat("Followed engine", s.followed_engine)}
      ${stat("Traded against WAIT", s.traded_against_wait)}
      ${stat("Skipped engine setups", s.skipped_engine_setups)}
      ${stat("Avg P/L following", s.avg_profit_when_following ?? "—")}
      ${stat("Avg P/L overriding", s.avg_profit_when_overriding ?? "—")}`;

    $("journalTable").querySelector("tbody").innerHTML = data.entries.map((e) => `
      <tr>
        <td class="muted">${e.created_at.slice(0, 16).replace("T", " ")}</td>
        <td class="${(e.signal_decision || "").toLowerCase()}">${escapeHtml(e.signal_decision || "—")}</td>
        <td>${e.took_trade ? "took it" : "passed"}</td>
        <td>${e.entry ? fmt(e.entry) : "—"}</td>
        <td>${e.stop_loss ? fmt(e.stop_loss) : "—"}</td>
        <td>${e.take_profit ? fmt(e.take_profit) : "—"}</td>
        <td>
          <select data-entry="${e.id}" class="result-select">
            ${["open", "win", "loss", "breakeven"].map((r) =>
              `<option ${e.result === r ? "selected" : ""}>${r}</option>`).join("")}
          </select>
        </td>
        <td><input class="profit-input" type="number" step="0.01" data-entry="${e.id}" value="${e.profit ?? ""}"></td>
        <td class="muted small">${escapeHtml((e.reason || "").slice(0, 60))}</td>
      </tr>`).join("") || `<tr><td colspan="9" class="muted">No journal entries yet.</td></tr>`;

    document.querySelectorAll(".result-select").forEach((el) => {
      el.addEventListener("change", () => patchJournal(el.dataset.entry, { result: el.value }));
    });
    document.querySelectorAll(".profit-input").forEach((el) => {
      el.addEventListener("change", () =>
        patchJournal(el.dataset.entry, { profit: parseFloat(el.value) || 0 }));
    });
  }

  async function patchJournal(id, fields) {
    await api(`/api/journal/${id}`, { method: "PATCH", body: JSON.stringify(fields) });
    loadJournal();
  }

  // ------------------------------------------------------------- backtest

  async function loadBacktests() {
    const data = await api("/api/backtests");
    $("backtestHistory").querySelector("tbody").innerHTML = data.backtests.map((b) => `
      <tr>
        <td class="muted">${b.created_at.slice(0, 16).replace("T", " ")}</td>
        <td>${escapeHtml(b.profile)}</td>
        <td>${b.stats.total_setups}</td>
        <td>${b.stats.win_rate === null ? "—" : b.stats.win_rate + "%"}</td>
        <td>${b.stats.profit_factor ?? "—"}</td>
        <td>${b.stats.total_r ?? "—"}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted">No backtests run yet.</td></tr>`;
  }

  function renderBacktestStats(stats) {
    $("backtestStats").innerHTML = `
      ${stat("Evaluations", stats.evaluations)}
      ${stat("Setups", stats.total_setups)}
      ${stat("Wins", stats.wins)}
      ${stat("Losses", stats.losses)}
      ${stat("Timeouts", stats.timeouts)}
      ${stat("Win rate", stats.win_rate === null ? "—" : stats.win_rate + "%")}
      ${stat("Profit factor", stats.profit_factor ?? "—")}
      ${stat("Total R", stats.total_r)}
      ${stat("Average R", stats.average_r ?? "—")}
      ${stat("Avg R:R", stats.average_reward_risk ?? "—")}
      ${stat("Max drawdown R", stats.max_drawdown_r)}
      ${stat("Max win streak", stats.max_consecutive_wins)}
      ${stat("Max loss streak", stats.max_consecutive_losses)}
      ${stat("Best session", stats.best_session ?? "—")}
      ${stat("Worst session", stats.worst_session ?? "—")}
      ${stat("Best regime", stats.best_regime ?? "—")}`;

    const rows = [];
    const section = (title, groups) => {
      for (const [name, g] of Object.entries(groups || {})) {
        rows.push(`<tr><td>${title}: ${escapeHtml(name)}</td><td>${g.setups}</td>
          <td>${g.wins}</td><td>${g.win_rate}%</td><td>${g.total_r}</td></tr>`);
      }
    };
    section("Session", stats.by_session);
    section("Regime", stats.by_regime);
    section("Direction", stats.by_direction);
    section("Score", stats.by_score_band);

    // When nothing triggered, the score distribution is the useful output.
    if (!stats.total_setups && stats.score_distribution) {
      const sd = stats.score_distribution;
      rows.push(`<tr><td colspan="5" class="muted">
        No setup met the profile's threshold. Score distribution across
        ${sd.evaluations} evaluations — median ${sd.median}, 90th percentile ${sd.p90},
        max ${sd.max}; ${sd.at_or_above_70} reached 70+, ${sd.at_or_above_60} reached 60+.
        </td></tr>`);
      for (const [reason, count] of Object.entries(stats.wait_reasons || {})) {
        rows.push(`<tr><td colspan="5" class="muted small">waited ${count}× — ${escapeHtml(reason)}</td></tr>`);
      }
    }
    $("backtestGroups").querySelector("tbody").innerHTML = rows.join("");
  }

  // ------------------------------------------------------------------- AI

  function appendChat(role, text, source) {
    const div = document.createElement("div");
    div.className = "chat-msg " + role;
    const tag = source && source !== "claude"
      ? `<span class="src offline">${escapeHtml(source)}</span>` : "";
    div.innerHTML = tag + paragraphs(text);
    $("chat").appendChild(div);
    $("chat").scrollTop = $("chat").scrollHeight;
    return div;
  }

  async function askQuestion(question) {
    appendChat("user", question);
    state.chatHistory.push({ role: "user", content: question });
    const pending = appendChat("assistant", "…thinking");
    try {
      const res = await api("/api/ask", {
        method: "POST",
        body: JSON.stringify({ question, history: state.chatHistory.slice(-8) }),
      });
      pending.innerHTML = (res.source && res.source !== "claude"
        ? `<span class="src offline">${escapeHtml(res.source)}</span>` : "") + paragraphs(res.text);
      state.chatHistory.push({ role: "assistant", content: res.text });
    } catch (err) {
      pending.innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`;
    }
  }

  // ---------------------------------------------------------------- utils

  function stat(label, value) {
    return `<div class="stat"><span>${escapeHtml(label)}</span><b>${escapeHtml(String(value ?? "—"))}</b></div>`;
  }

  function fillList(el, items, render, emptyText = "—") {
    el.innerHTML = items && items.length
      ? items.map(render).join("")
      : `<li class="muted">${escapeHtml(emptyText)}</li>`;
  }

  function paragraphs(text) {
    return String(text || "").split(/\n\n+/)
      .map((p) => `<p>${escapeHtml(p).replace(/\n/g, "<br>")}</p>`).join("");
  }

  // Bring the full signal card into view -- used by the banner's "details"
  // button and by clicking an alert toast.
  function showSignalCard() {
    const tab = document.querySelector('.tab[data-view="terminal"]');
    if (tab) tab.click();
    const card = $("signalCard");
    if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = String(value === null || value === undefined ? "" : value);
    return div.innerHTML;
  }

  // ----------------------------------------------------------------- init

  function initForms() {
    updateSoundToggle();
    $("soundToggle").addEventListener("click", () => sound.toggle());

    // Any first click also satisfies the browser's autoplay gate.
    document.addEventListener("click", () => sound.ensure(), { once: true });

    $("sbJump").addEventListener("click", showSignalCard);

    $("profileSelect").addEventListener("change", async (e) => {
      await api("/api/profiles/active", {
        method: "POST",
        body: JSON.stringify({ name: e.target.value }),
      });
      applySnapshot(await api("/api/snapshot"));
    });

    $("chatForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("chatInput").value.trim();
      if (!q) return;
      $("chatInput").value = "";
      askQuestion(q);
    });

    document.querySelectorAll(".quick-questions button").forEach((btn) => {
      btn.addEventListener("click", () => askQuestion(btn.dataset.q));
    });

    $("regenerateBtn").addEventListener("click", async () => {
      $("explanation").textContent = "Generating…";
      try {
        setExplanation(await api("/api/explain", { method: "POST" }));
      } catch (err) {
        $("explanation").innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`;
      }
    });

    $("chartImageInput").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      $("chartImageResult").textContent = "Analysing image…";
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/chart-image", { method: "POST", body: form });
      const data = await res.json();
      $("chartImageResult").innerHTML = paragraphs(data.text || data.error || "No result.");
    });

    document.querySelectorAll(".filter").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".filter").forEach((b) => b.classList.toggle("active", b === btn));
        state.signalFilter = btn.dataset.filter;
        loadSignals();
      });
    });

    $("journalForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = new FormData(e.target);
      const body = {
        signal_id: state.snapshot ? state.snapshot.signal_id : null,
        took_trade: form.get("took_trade") === "1",
        reason: form.get("reason"),
        entry: parseFloat(form.get("entry")) || null,
        stop_loss: parseFloat(form.get("stop_loss")) || null,
        take_profit: parseFloat(form.get("take_profit")) || null,
        lot_size: parseFloat(form.get("lot_size")) || null,
        notes: form.get("notes"),
      };
      await api("/api/journal", { method: "POST", body: JSON.stringify(body) });
      e.target.reset();
      loadJournal();
    });

    $("backtestForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = new FormData(e.target);
      $("backtestStatus").textContent =
        "Replaying history through the live engines — this takes a few minutes.";
      $("backtestStats").innerHTML = "";
      try {
        const res = await api("/api/backtests", {
          method: "POST",
          body: JSON.stringify({
            profile: form.get("profile"),
            timeframe: form.get("timeframe"),
            step: parseInt(form.get("step"), 10) || 8,
          }),
        });
        const w = res.stats.window || {};
        $("backtestStatus").textContent =
          `Window ${(w.from || "").slice(0, 10)} → ${(w.to || "").slice(0, 10)}, ` +
          `every ${res.stats.step} ${res.stats.step_timeframe} candles.`;
        renderBacktestStats(res.stats);
        loadBacktests();
      } catch (err) {
        $("backtestStatus").innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`;
      }
    });

    const concepts = ["BOS", "CHoCH", "MSS", "liquidity", "liquidity sweep", "FVG",
                      "order block", "supply and demand", "regime", "confluence",
                      "invalidation"];
    $("conceptButtons").innerHTML = concepts
      .map((c) => `<button data-concept="${escapeHtml(c)}">${escapeHtml(c)}</button>`).join("");
    $("conceptButtons").addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      $("learnOutput").textContent = "Explaining…";
      const res = await api("/api/teach", {
        method: "POST",
        body: JSON.stringify({ concept: btn.dataset.concept }),
      });
      $("learnOutput").innerHTML = paragraphs(res.text);
    });

    $("learnForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const concept = $("learnInput").value.trim();
      if (!concept) return;
      $("learnOutput").textContent = "Explaining…";
      const res = await api("/api/teach", { method: "POST", body: JSON.stringify({ concept }) });
      $("learnOutput").innerHTML = paragraphs(res.text);
    });
  }

  async function boot() {
    initChart();
    initTabs();
    initForms();
    initFiredFilters();
    // The clock comes first: everything rendered below it carries a timestamp.
    await loadClock();
    startClock();
    await loadProfiles();
    try {
      applySnapshot(await api("/api/snapshot"));
    } catch (err) {
      console.error(err);
    }
    await loadChart();
    renderDashboardTable();
    connectSocket();
    // The chart follows closed candles; a slow poll covers a dropped socket.
    setInterval(loadChart, 60000);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
