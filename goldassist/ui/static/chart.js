/*
 * Candlestick chart with analytical overlays.
 *
 * Written directly against canvas rather than a charting library because the
 * layers this application needs are mostly *regions* — supply/demand zones,
 * Fair Value Gaps, order blocks — which general charting libraries do not draw
 * natively. Doing it here keeps every layer toggleable and consistent.
 *
 * Coordinate model: candle index -> x, price -> y. `view` holds the visible
 * index range and is the only thing pan/zoom mutate.
 */

(function (global) {
  "use strict";

  /*
   * The axis is drawn on the trader's clock, not UTC.
   *
   * The candle index the server sends is UTC epoch seconds, which is correct
   * and stays that way. But an axis reading 12:00 beside a signal card reading
   * 14:00 for the same instant is a chart nobody can trade from, so the offset
   * app.js has already resolved is read from the window and applied here too.
   * The fallback matches the application default, so the axis is right even if
   * this file is loaded before the clock has been fetched.
   */
  function axisLabel(epochSeconds) {
    const offset = Number(global.GA_CLOCK_OFFSET_HOURS ?? 2);
    const d = new Date(epochSeconds * 1000 + offset * 3600000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
      + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }

  const COLORS = {
    up: "#26a17b",
    down: "#e0524a",
    upWick: "#26a17b",
    downWick: "#e0524a",
    grid: "rgba(255,255,255,0.05)",
    axis: "rgba(255,255,255,0.45)",
    text: "#8b93a7",
    crosshair: "rgba(255,255,255,0.28)",
    demand: "rgba(38,161,123,0.13)",
    demandEdge: "rgba(38,161,123,0.55)",
    supply: "rgba(224,82,74,0.13)",
    supplyEdge: "rgba(224,82,74,0.55)",
    fvgBull: "rgba(90,160,255,0.14)",
    fvgBear: "rgba(200,120,255,0.14)",
    obBull: "rgba(38,161,123,0.20)",
    obBear: "rgba(224,82,74,0.20)",
    resistance: "#e0524a",
    support: "#26a17b",
    liquidity: "#d9a441",
    bos: "#5aa0ff",
    choch: "#e08c3a",
    mss: "#ff5ea8",
    ema: ["#f2c14e", "#6fa8dc", "#b48ead", "#8fbcbb"],
    vwap: "#d9a441",
    bollinger: "rgba(140,150,180,0.45)",
    price: "#e8ecf4",
  };

  const PADDING = { top: 14, right: 74, bottom: 26, left: 8 };
  const PANE_GAP = 8;

  class GoldChart {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.data = null;
      this.layers = opts.layers || {};
      this.onHover = opts.onHover || null;
      this.view = { start: 0, end: 0 };      // visible candle index range
      this.hover = null;                     // {x, y} in CSS pixels
      this.dpr = global.devicePixelRatio || 1;

      this._bindEvents();
      this._resizeObserver = new ResizeObserver(() => this.resize());
      this._resizeObserver.observe(canvas.parentElement);
    }

    // -- data ---------------------------------------------------------------

    setData(data) {
      const first = !this.data;
      const previousCount = this.data ? this.data.candles.length : 0;
      this.data = data;
      const count = data.candles.length;

      if (first || this.view.end === 0) {
        // Default view: the most recent 160 candles.
        this.view.start = Math.max(0, count - 160);
        this.view.end = count;
      } else {
        // Keep the user's zoom, but follow the tape if they were at the edge.
        const wasAtEdge = this.view.end >= previousCount - 1;
        const span = this.view.end - this.view.start;
        if (wasAtEdge) {
          this.view.end = count;
          this.view.start = Math.max(0, count - span);
        } else {
          this.view.end = Math.min(this.view.end, count);
          this.view.start = Math.max(0, Math.min(this.view.start, this.view.end - 10));
        }
      }
      this.resize();
    }

    setLayers(layers) {
      this.layers = layers;
      this.draw();
    }

    resetView() {
      if (!this.data) return;
      const count = this.data.candles.length;
      this.view.start = Math.max(0, count - 160);
      this.view.end = count;
      this.draw();
    }

    // -- geometry -----------------------------------------------------------

    resize() {
      const host = this.canvas.parentElement;
      const width = host.clientWidth;
      const height = host.clientHeight;
      this.dpr = global.devicePixelRatio || 1;
      this.canvas.width = Math.round(width * this.dpr);
      this.canvas.height = Math.round(height * this.dpr);
      this.canvas.style.width = width + "px";
      this.canvas.style.height = height + "px";
      this.width = width;
      this.height = height;
      this.draw();
    }

    /* Sub-panes: price always, plus optional RSI and MACD panes below it. */
    _panes() {
      const extras = [];
      if (this.layers.rsi) extras.push("rsi");
      if (this.layers.macd) extras.push("macd");

      const usable = this.height - PADDING.top - PADDING.bottom;
      const extraHeight = extras.length ? Math.min(190, usable * 0.34) : 0;
      const each = extras.length ? (extraHeight - PANE_GAP * extras.length) / extras.length : 0;

      const panes = { price: { top: PADDING.top, height: usable - extraHeight } };
      let y = PADDING.top + panes.price.height;
      for (const name of extras) {
        y += PANE_GAP;
        panes[name] = { top: y, height: each };
        y += each;
      }
      return panes;
    }

    _visible() {
      const c = this.data.candles;
      return c.slice(Math.max(0, Math.floor(this.view.start)), Math.ceil(this.view.end));
    }

    _priceRange(pane) {
      const visible = this._visible();
      if (!visible.length) return { min: 0, max: 1 };
      let min = Infinity;
      let max = -Infinity;
      for (const c of visible) {
        if (c.low < min) min = c.low;
        if (c.high > max) max = c.high;
      }
      // Include overlay geometry so a zone just off-screen still shows.
      for (const item of this._regionItems()) {
        if (item.bottom < min && item.bottom > min - (max - min)) min = item.bottom;
        if (item.top > max && item.top < max + (max - min)) max = item.top;
      }
      const pad = (max - min) * 0.08 || 1;
      return { min: min - pad, max: max + pad };
    }

    _regionItems() {
      if (!this.data) return [];
      const out = [];
      if (this.layers.zones) out.push(...(this.data.zones || []));
      if (this.layers.fvg) out.push(...(this.data.fvg || []));
      if (this.layers.orderBlocks) out.push(...(this.data.order_blocks || []));
      return out;
    }

    xOf(index) {
      const span = this.view.end - this.view.start;
      const plotWidth = this.width - PADDING.left - PADDING.right;
      return PADDING.left + ((index - this.view.start) / span) * plotWidth;
    }

    indexOfX(x) {
      const span = this.view.end - this.view.start;
      const plotWidth = this.width - PADDING.left - PADDING.right;
      return this.view.start + ((x - PADDING.left) / plotWidth) * span;
    }

    yOf(price, pane, range) {
      const ratio = (price - range.min) / (range.max - range.min || 1);
      return pane.top + pane.height - ratio * pane.height;
    }

    /* Candle index for a unix timestamp; -1 when it predates the loaded data. */
    indexOfTime(time) {
      const candles = this.data.candles;
      if (!candles.length) return -1;
      if (time <= candles[0].time) return 0;
      let lo = 0;
      let hi = candles.length - 1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (candles[mid].time === time) return mid;
        if (candles[mid].time < time) lo = mid + 1;
        else hi = mid - 1;
      }
      return Math.min(lo, candles.length - 1);
    }

    // -- drawing ------------------------------------------------------------

    draw() {
      if (!this.data || !this.width) return;
      const ctx = this.ctx;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.width, this.height);

      const panes = this._panes();
      const range = this._priceRange(panes.price);

      this._drawGrid(panes.price, range);
      this._drawRegions(panes.price, range);
      this._drawLevels(panes.price, range);
      this._drawLiquidity(panes.price, range);
      this._drawFib(panes.price, range);
      this._drawSeries(panes.price, range);
      this._drawCandles(panes.price, range);
      this._drawStructure(panes.price, range);
      this._drawSwings(panes.price, range);
      this._drawPriceAxis(panes.price, range);
      this._drawTimeAxis(panes.price);

      if (panes.rsi) this._drawRsi(panes.rsi);
      if (panes.macd) this._drawMacd(panes.macd);

      this._drawCrosshair(panes.price, range);
    }

    _drawGrid(pane, range) {
      const ctx = this.ctx;
      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i <= 5; i++) {
        const y = Math.round(pane.top + (pane.height / 5) * i) + 0.5;
        ctx.moveTo(PADDING.left, y);
        ctx.lineTo(this.width - PADDING.right, y);
      }
      ctx.stroke();
    }

    _drawCandles(pane, range) {
      const ctx = this.ctx;
      const candles = this.data.candles;
      const span = this.view.end - this.view.start;
      const plotWidth = this.width - PADDING.left - PADDING.right;
      const slot = plotWidth / span;
      const bodyWidth = Math.max(1, Math.min(14, slot * 0.68));

      for (let i = Math.max(0, Math.floor(this.view.start)); i < Math.min(candles.length, Math.ceil(this.view.end)); i++) {
        const c = candles[i];
        const x = this.xOf(i + 0.5);
        const up = c.close >= c.open;
        const openY = this.yOf(c.open, pane, range);
        const closeY = this.yOf(c.close, pane, range);

        ctx.strokeStyle = up ? COLORS.upWick : COLORS.downWick;
        ctx.lineWidth = Math.max(1, bodyWidth * 0.14);
        ctx.beginPath();
        ctx.moveTo(x, this.yOf(c.high, pane, range));
        ctx.lineTo(x, this.yOf(c.low, pane, range));
        ctx.stroke();

        ctx.fillStyle = up ? COLORS.up : COLORS.down;
        const top = Math.min(openY, closeY);
        const height = Math.max(1, Math.abs(closeY - openY));
        ctx.fillRect(x - bodyWidth / 2, top, bodyWidth, height);
      }
    }

    /* Supply/demand zones, FVGs and order blocks — drawn as bands that extend
       from where they formed to the right edge, the way a trader marks them. */
    _drawRegions(pane, range) {
      const ctx = this.ctx;
      const right = this.width - PADDING.right;

      const band = (item, fill, edge, label) => {
        const startIndex = this.indexOfTime(item.created);
        const x = Math.max(PADDING.left, this.xOf(startIndex));
        if (x > right) return;
        const yTop = this.yOf(item.top, pane, range);
        const yBottom = this.yOf(item.bottom, pane, range);
        if (yBottom < pane.top - 40 || yTop > pane.top + pane.height + 40) return;

        ctx.fillStyle = fill;
        ctx.fillRect(x, yTop, right - x, Math.max(1, yBottom - yTop));
        ctx.strokeStyle = edge;
        ctx.lineWidth = 1;
        ctx.strokeRect(x + 0.5, yTop + 0.5, right - x - 1, Math.max(1, yBottom - yTop) - 1);

        if (label && yBottom - yTop > 13) {
          ctx.fillStyle = edge;
          ctx.font = "10px ui-monospace, monospace";
          ctx.textAlign = "left";
          ctx.fillText(label, x + 4, yTop + 11);
        }
      };

      if (this.layers.zones) {
        for (const z of this.data.zones || []) {
          const demand = z.kind === "demand";
          band(z, demand ? COLORS.demand : COLORS.supply,
               demand ? COLORS.demandEdge : COLORS.supplyEdge,
               `${z.timeframe} ${z.kind}${z.fresh ? " · fresh" : ""} ${z.quality.toFixed(0)}`);
        }
      }
      if (this.layers.fvg) {
        for (const g of this.data.fvg || []) {
          band(g, g.direction === "bullish" ? COLORS.fvgBull : COLORS.fvgBear,
               g.direction === "bullish" ? "rgba(90,160,255,0.5)" : "rgba(200,120,255,0.5)",
               `FVG ${g.timeframe}`);
        }
      }
      if (this.layers.orderBlocks) {
        for (const b of this.data.order_blocks || []) {
          band(b, b.direction === "bullish" ? COLORS.obBull : COLORS.obBear,
               b.direction === "bullish" ? "rgba(38,161,123,0.7)" : "rgba(224,82,74,0.7)",
               `OB ${b.timeframe} ${b.quality.toFixed(0)}`);
        }
      }
    }

    _drawLevels(pane, range) {
      if (!this.layers.levels) return;
      const ctx = this.ctx;
      const right = this.width - PADDING.right;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "right";
      const usedLabelRows = [];

      for (const lvl of this.data.levels || []) {
        const y = this.yOf(lvl.price, pane, range);
        if (y < pane.top || y > pane.top + pane.height) continue;
        const strong = lvl.strength >= 65;
        ctx.strokeStyle = lvl.kind === "resistance" ? COLORS.resistance : COLORS.support;
        ctx.globalAlpha = 0.25 + (lvl.strength / 100) * 0.55;
        ctx.lineWidth = strong ? 1.6 : 1;
        ctx.setLineDash(strong ? [] : [4, 4]);
        ctx.beginPath();
        ctx.moveTo(PADDING.left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
        ctx.setLineDash([]);

        // Skip a label that would land on top of one already drawn.
        if (!usedLabelRows.some((used) => Math.abs(used - y) < 11)) {
          usedLabelRows.push(y);
          ctx.fillStyle = lvl.kind === "resistance" ? COLORS.resistance : COLORS.support;
          ctx.fillText(`${lvl.source} ${lvl.strength.toFixed(0)}`, right - 4, y - 3);
        }
        ctx.globalAlpha = 1;
      }
    }

    _drawLiquidity(pane, range) {
      if (!this.layers.liquidity) return;
      const ctx = this.ctx;
      const right = this.width - PADDING.right;
      const liq = this.data.liquidity || {};

      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "left";
      for (const pool of liq.pools || []) {
        const y = this.yOf(pool.price, pane, range);
        if (y < pane.top || y > pane.top + pane.height) continue;
        ctx.strokeStyle = COLORS.liquidity;
        ctx.globalAlpha = 0.3 + (pool.strength / 100) * 0.5;
        ctx.setLineDash([2, 5]);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(PADDING.left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = COLORS.liquidity;
        ctx.fillText(pool.source.replace(/_/g, " "), PADDING.left + 4, y - 3);
        ctx.globalAlpha = 1;
      }

      // Sweeps get an explicit marker at the candle that took the liquidity.
      for (const sweep of liq.sweeps || []) {
        const index = this.indexOfTime(sweep.time);
        const x = this.xOf(index + 0.5);
        if (x < PADDING.left || x > right) continue;
        const y = this.yOf(sweep.level, pane, range);
        ctx.fillStyle = sweep.direction === "bullish" ? COLORS.up : COLORS.down;
        ctx.beginPath();
        ctx.arc(x, y, 3.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = "9px ui-monospace, monospace";
        ctx.textAlign = "center";
        ctx.fillText("sweep", x, sweep.direction === "bullish" ? y + 14 : y - 8);
      }
    }

    _drawFib(pane, range) {
      if (!this.layers.fib) return;
      const fib = this.data.fibonacci || {};
      const levels = fib.retracements;
      if (!levels) return;
      const ctx = this.ctx;
      const right = this.width - PADDING.right;
      ctx.font = "9px ui-monospace, monospace";
      ctx.textAlign = "left";
      for (const [ratio, price] of Object.entries(levels)) {
        const y = this.yOf(price, pane, range);
        if (y < pane.top || y > pane.top + pane.height) continue;
        const golden = ratio === "0.618" || ratio === "0.786";
        ctx.strokeStyle = golden ? "rgba(217,164,65,0.75)" : "rgba(217,164,65,0.3)";
        ctx.lineWidth = golden ? 1.3 : 1;
        ctx.beginPath();
        ctx.moveTo(PADDING.left, y);
        ctx.lineTo(right, y);
        ctx.stroke();
        ctx.fillStyle = "rgba(217,164,65,0.9)";
        ctx.fillText(`fib ${ratio}`, PADDING.left + 4, y - 2);
      }
    }

    _drawSeries(pane, range) {
      const series = this.data.series || {};
      if (this.layers.ema) {
        ["ema20", "ema50", "ema100", "ema200"].forEach((key, i) => {
          this._line(series[key], pane, range, COLORS.ema[i], 1.2);
        });
      }
      if (this.layers.vwap) this._line(series.vwap, pane, range, COLORS.vwap, 1.4, [5, 3]);
      if (this.layers.bollinger) {
        this._line(series.bb_upper, pane, range, COLORS.bollinger, 1);
        this._line(series.bb_lower, pane, range, COLORS.bollinger, 1);
      }
    }

    _line(points, pane, range, color, width = 1, dash = null) {
      if (!points || !points.length) return;
      const ctx = this.ctx;
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      if (dash) ctx.setLineDash(dash);
      ctx.beginPath();
      let started = false;
      // Points are aligned to candles by timestamp, so an index lookup keeps
      // the line correct even when the series is shorter than the candle set.
      for (const p of points) {
        const index = this.indexOfTime(p.time);
        if (index < this.view.start - 2 || index > this.view.end + 2) continue;
        const x = this.xOf(index + 0.5);
        const y = this.yOf(p.value, pane, range);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }

    _drawStructure(pane, range) {
      if (!this.layers.structure) return;
      const events = (this.data.structure || {}).events || [];
      const ctx = this.ctx;
      ctx.font = "9px ui-monospace, monospace";

      for (const ev of events) {
        const index = this.indexOfTime(ev.time);
        const x = this.xOf(index + 0.5);
        if (x < PADDING.left || x > this.width - PADDING.right) continue;
        const y = this.yOf(ev.price, pane, range);
        const color = ev.kind === "BOS" ? COLORS.bos : ev.kind === "MSS" ? COLORS.mss : COLORS.choch;
        const fromIndex = this.indexOfTime(ev.broken_swing_time);

        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(Math.max(PADDING.left, this.xOf(fromIndex + 0.5)), y);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = color;
        ctx.textAlign = "left";
        ctx.fillText(`${ev.kind} ${ev.direction === "bullish" ? "▲" : "▼"}`, x + 3, y - 3);
      }
    }

    _drawSwings(pane, range) {
      if (!this.layers.swings) return;
      const swings = (this.data.structure || {}).swings || [];
      const ctx = this.ctx;
      ctx.font = "9px ui-monospace, monospace";
      ctx.textAlign = "center";
      for (const s of swings) {
        if (!s.label) continue;
        const index = this.indexOfTime(s.time);
        const x = this.xOf(index + 0.5);
        if (x < PADDING.left || x > this.width - PADDING.right) continue;
        const y = this.yOf(s.price, pane, range);
        const bullish = s.label === "HH" || s.label === "HL";
        ctx.fillStyle = bullish ? "rgba(38,161,123,0.95)" : "rgba(224,82,74,0.95)";
        ctx.fillText(s.label, x, s.kind === "high" ? y - 6 : y + 12);
      }
    }

    _drawPriceAxis(pane, range) {
      const ctx = this.ctx;
      const x = this.width - PADDING.right;
      ctx.fillStyle = COLORS.text;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "left";
      for (let i = 0; i <= 5; i++) {
        const price = range.max - ((range.max - range.min) / 5) * i;
        const y = this.yOf(price, pane, range);
        ctx.fillText(price.toFixed(2), x + 6, y + 3);
      }

      // Last price tag.
      const candles = this.data.candles;
      if (candles.length) {
        const last = candles[candles.length - 1];
        const y = this.yOf(last.close, pane, range);
        if (y >= pane.top && y <= pane.top + pane.height) {
          ctx.fillStyle = last.close >= last.open ? COLORS.up : COLORS.down;
          ctx.fillRect(x + 2, y - 8, PADDING.right - 6, 16);
          ctx.fillStyle = "#0b0e14";
          ctx.fillText(last.close.toFixed(2), x + 6, y + 3);
        }
      }
    }

    _drawTimeAxis(pane) {
      const ctx = this.ctx;
      const candles = this.data.candles;
      const y = pane.top + pane.height + 14;
      ctx.fillStyle = COLORS.text;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "center";
      const span = this.view.end - this.view.start;
      const stepCount = Math.min(8, Math.max(2, Math.floor(this.width / 110)));
      for (let i = 0; i <= stepCount; i++) {
        const index = Math.floor(this.view.start + (span / stepCount) * i);
        if (index < 0 || index >= candles.length) continue;
        ctx.fillText(axisLabel(candles[index].time), this.xOf(index + 0.5), y);
      }
    }

    _drawRsi(pane) {
      const ctx = this.ctx;
      const series = (this.data.series || {}).rsi;
      if (!series) return;
      const range = { min: 0, max: 100 };

      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      for (const level of [30, 50, 70]) {
        const y = this.yOf(level, pane, range);
        ctx.beginPath();
        ctx.moveTo(PADDING.left, y);
        ctx.lineTo(this.width - PADDING.right, y);
        ctx.stroke();
      }
      this._line(series, pane, range, "#b48ead", 1.3);
      ctx.fillStyle = COLORS.text;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "left";
      ctx.fillText("RSI", PADDING.left + 4, pane.top + 11);
    }

    _drawMacd(pane) {
      const ctx = this.ctx;
      const series = this.data.series || {};
      const hist = series.macd_hist || [];
      if (!hist.length) return;

      let max = 0;
      for (const p of hist) max = Math.max(max, Math.abs(p.value));
      const range = { min: -max * 1.2 || -1, max: max * 1.2 || 1 };

      const span = this.view.end - this.view.start;
      const barWidth = Math.max(1, ((this.width - PADDING.left - PADDING.right) / span) * 0.6);
      const zeroY = this.yOf(0, pane, range);
      for (const p of hist) {
        const index = this.indexOfTime(p.time);
        if (index < this.view.start || index > this.view.end) continue;
        const x = this.xOf(index + 0.5);
        const y = this.yOf(p.value, pane, range);
        ctx.fillStyle = p.value >= 0 ? "rgba(38,161,123,0.75)" : "rgba(224,82,74,0.75)";
        ctx.fillRect(x - barWidth / 2, Math.min(y, zeroY), barWidth, Math.abs(y - zeroY) || 1);
      }
      this._line(series.macd, pane, range, "#6fa8dc", 1.2);
      this._line(series.macd_signal, pane, range, "#f2c14e", 1.2);
      ctx.fillStyle = COLORS.text;
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "left";
      ctx.fillText("MACD", PADDING.left + 4, pane.top + 11);
    }

    _drawCrosshair(pane, range) {
      if (!this.hover) return;
      const ctx = this.ctx;
      const { x, y } = this.hover;
      if (x < PADDING.left || x > this.width - PADDING.right) return;

      ctx.strokeStyle = COLORS.crosshair;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, PADDING.top);
      ctx.lineTo(x, this.height - PADDING.bottom);
      ctx.moveTo(PADDING.left, y);
      ctx.lineTo(this.width - PADDING.right, y);
      ctx.stroke();
      ctx.setLineDash([]);

      if (y >= pane.top && y <= pane.top + pane.height) {
        const price = range.max - ((y - pane.top) / pane.height) * (range.max - range.min);
        ctx.fillStyle = "#1b2130";
        ctx.fillRect(this.width - PADDING.right + 2, y - 8, PADDING.right - 6, 16);
        ctx.fillStyle = COLORS.price;
        ctx.font = "10px ui-monospace, monospace";
        ctx.textAlign = "left";
        ctx.fillText(price.toFixed(2), this.width - PADDING.right + 6, y + 3);
      }
    }

    // -- interaction --------------------------------------------------------

    _bindEvents() {
      const canvas = this.canvas;
      let dragging = false;
      let dragStartX = 0;
      let viewStartAtDrag = 0;

      canvas.addEventListener("mousedown", (e) => {
        dragging = true;
        dragStartX = e.offsetX;
        viewStartAtDrag = this.view.start;
        canvas.style.cursor = "grabbing";
      });

      global.addEventListener("mouseup", () => {
        dragging = false;
        canvas.style.cursor = "crosshair";
      });

      canvas.addEventListener("mousemove", (e) => {
        this.hover = { x: e.offsetX, y: e.offsetY };
        if (dragging && this.data) {
          const span = this.view.end - this.view.start;
          const plotWidth = this.width - PADDING.left - PADDING.right;
          const shift = ((dragStartX - e.offsetX) / plotWidth) * span;
          const count = this.data.candles.length;
          let start = viewStartAtDrag + shift;
          start = Math.max(-span * 0.1, Math.min(count - span * 0.35, start));
          this.view.start = start;
          this.view.end = start + span;
        }
        this.draw();
        if (this.onHover && this.data) {
          const index = Math.floor(this.indexOfX(e.offsetX));
          const candle = this.data.candles[index];
          this.onHover(candle || null, e.offsetX, e.offsetY);
        }
      });

      canvas.addEventListener("mouseleave", () => {
        this.hover = null;
        this.draw();
        if (this.onHover) this.onHover(null);
      });

      canvas.addEventListener("wheel", (e) => {
        if (!this.data) return;
        e.preventDefault();
        const span = this.view.end - this.view.start;
        const factor = e.deltaY > 0 ? 1.12 : 0.89;
        const newSpan = Math.max(25, Math.min(this.data.candles.length * 1.5, span * factor));
        // Zoom around the cursor so the candle under it stays put.
        const anchor = this.indexOfX(e.offsetX);
        const ratio = (anchor - this.view.start) / span;
        this.view.start = anchor - newSpan * ratio;
        this.view.end = this.view.start + newSpan;
        this.draw();
      }, { passive: false });

      canvas.addEventListener("dblclick", () => this.resetView());
      canvas.style.cursor = "crosshair";
    }
  }

  global.GoldChart = GoldChart;
})(window);
