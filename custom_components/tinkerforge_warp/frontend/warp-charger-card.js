/* Tinkerforge WARP charger card. Loaded automatically by the integration. */

const DOMAIN = "tinkerforge_warp";
const CARD = "warp-charger-card";

const STRINGS = {
  en: {
    not_connected: "Not connected",
    waiting_for_release: "Waiting",
    ready: "Ready",
    charging: "Charging",
    error: "Error",
    power: "Power",
    phases: "Phases",
    session: "Session",
    duration: "Duration",
    user: "User",
    energy_limit: "Energy limit",
    duration_limit: "Time limit",
    limit: "Current limit",
    allowed: "allowed",
    paused: "Paused",
    mode: "Charge mode",
    start: "Start charging",
    stop: "Stop charging",
    boost: "Boost",
    no_device: "Select a WARP charger in the card editor.",
    not_found: "No WARP charger entities found for this device.",
    device: "Charger",
    name: "Name",
    show_controls: "Show controls",
  },
  de: {
    not_connected: "Nicht verbunden",
    waiting_for_release: "Warte auf Freigabe",
    ready: "Bereit",
    charging: "Lädt",
    error: "Fehler",
    power: "Leistung",
    phases: "Phasen",
    session: "Ladevorgang",
    duration: "Dauer",
    user: "Benutzer",
    energy_limit: "Energielimit",
    duration_limit: "Zeitlimit",
    limit: "Ladestromgrenze",
    allowed: "erlaubt",
    paused: "Pausiert",
    mode: "Lademodus",
    start: "Laden starten",
    stop: "Laden stoppen",
    boost: "Boost",
    no_device: "Wähle im Karteneditor eine WARP-Wallbox aus.",
    not_found: "Keine WARP-Entitäten für dieses Gerät gefunden.",
    device: "Wallbox",
    name: "Name",
    show_controls: "Steuerung anzeigen",
  },
};

// Charger state steps, as in the WARP web interface status page.
const STEPS = ["not_connected", "waiting_for_release", "ready", "charging"];
const STATE_COLOR = {
  not_connected: "var(--secondary-text-color)",
  waiting_for_release: "var(--warning-color, #ffa600)",
  ready: "var(--info-color, #039be5)",
  charging: "var(--success-color, #43a047)",
  error: "var(--error-color, #db4437)",
};
const PHASE_KEYS = ["l1", "l2", "l3"].map((l) => [`current_${l}_im_ex_sum`, `current_${l}_im_ex_diff`]);

const t = (hass, key) => (STRINGS[hass?.locale?.language] || STRINGS.en)[key] ?? STRINGS.en[key];

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

const usable = (stateObj) =>
  stateObj && stateObj.state !== "unavailable" && stateObj.state !== "unknown";

const clamp = (v) => Math.max(0, Math.min(100, v));

/** "15m" / "2h" -> minutes; anything else (e.g. "off") -> 0. */
const durationMinutes = (option) => {
  const m = /^(\d+)([mh])$/.exec(option || "");
  return m ? Number(m[1]) * (m[2] === "h" ? 60 : 1) : 0;
};

const hhmm = (minutes) => `${Math.floor(minutes / 60)}:${String(Math.floor(minutes % 60)).padStart(2, "0")}`;

// The WARP mark from the brand icon: open box with two posts.
const MARK = `<svg viewBox="24 55 208 145" aria-hidden="true">
  <rect x="24" y="55" width="25" height="145"/><rect x="24" y="175" width="208" height="25"/>
  <rect x="120" y="55" width="25" height="97"/><rect x="208" y="55" width="24" height="97"/></svg>`;

/** Map translation_key -> entity_id for all WARP entities of a device. */
function deviceEntities(hass, deviceId) {
  const result = {};
  for (const entry of Object.values(hass.entities || {})) {
    if (entry.device_id === deviceId && entry.platform === DOMAIN && entry.translation_key) {
      result[entry.translation_key] ??= entry.entity_id;
    }
  }
  return result;
}

function findChargers(hass) {
  return Object.values(hass.entities || {})
    .filter((e) => e.platform === DOMAIN && e.translation_key === "charger_state" && e.device_id)
    .map((e) => e.device_id);
}

class WarpChargerCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(`${CARD}-editor`);
  }

  static getStubConfig(hass) {
    return { device: findChargers(hass)[0] ?? "" };
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this._config = { show_controls: true, ...config };
    this._signature = undefined;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return this._config?.show_controls === false ? 4 : 8;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  connectedCallback() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
      this.shadowRoot.addEventListener("input", (ev) => this._onInput(ev));
      this.shadowRoot.addEventListener("change", (ev) => this._onChange(ev));
    }
    // Session duration ticks without a state change.
    this._clock = setInterval(() => this._render(), 30000);
    this._render();
  }

  disconnectedCallback() {
    clearInterval(this._clock);
  }

  // ---- view model ----

  _entity(key) {
    const id = this._ids?.[key];
    return id ? this._hass.states[id] : undefined;
  }

  _format(key) {
    const stateObj = this._entity(key);
    return usable(stateObj) ? this._hass.formatEntityState(stateObj) : "—";
  }

  /** Formatted number and unit separately, for the large readouts. */
  _parts(key) {
    const stateObj = this._entity(key);
    const unit = stateObj?.attributes.unit_of_measurement ?? "";
    if (!usable(stateObj)) return { value: "—", unit };
    const text = this._hass.formatEntityState(stateObj);
    return { value: unit && text.endsWith(unit) ? text.slice(0, -unit.length).trim() : text, unit };
  }

  _number(key) {
    const stateObj = this._entity(key);
    return usable(stateObj) ? Number(stateObj.state) : NaN;
  }

  _model() {
    const hass = this._hass;
    const deviceId = this._config.device;
    if (!deviceId) return { message: t(hass, "no_device") };
    this._ids = deviceEntities(hass, deviceId);
    if (!this._ids.charger_state) return { message: t(hass, "not_found") };

    const device = hass.devices?.[deviceId];
    const stateObj = this._entity("charger_state");
    const state = usable(stateObj) ? stateObj.state : "unavailable";
    const charging = state === "charging";
    const plugged = ["waiting_for_release", "ready", "charging"].includes(state);
    const error = state === "error" || this._entity("problem")?.state === "on";

    const model = {
      model: device?.model || "WARP",
      name: this._config.name || device?.name_by_user || device?.name || "WARP",
      state,
      color: error ? STATE_COLOR.error : STATE_COLOR[state] || "var(--disabled-color, #9e9e9e)",
      charging,
      steps: STEPS.map((step) => ({ key: step, label: t(hass, step), active: step === state })),
      error: error ? (this._ids.error_state ? this._format("error_state") : t(hass, "error")) : null,
      unavailable: !usable(stateObj) && state !== "unknown",
      power: this._parts("power_active_l_sum_im_ex_diff"),
      hasPower: Boolean(this._ids.power_active_l_sum_im_ex_diff),
    };

    // Phases: lit per measured phase current if those sensors are enabled, else by active count.
    const activeCount = this._number("phases_active");
    model.phases = PHASE_KEYS.map(([sum, diff], i) => {
      const key = this._ids[sum] ? sum : this._ids[diff] ? diff : null;
      const amps = key ? this._number(key) : NaN;
      const lit = Number.isNaN(amps) ? charging && i < activeCount : Math.abs(amps) >= 0.5;
      return { label: `L${i + 1}`, lit, amps: Number.isNaN(amps) ? null : `${amps.toFixed(1)} A`, key };
    });

    // Session: energy, duration, NFC user, charge limit progress.
    const energy = this._number("session_energy");
    const start = Date.parse(this._entity("session_start")?.state);
    const elapsed = Number.isNaN(start) ? NaN : Math.max(0, (Date.now() - start) / 60000);
    model.session = plugged || !Number.isNaN(energy)
      ? {
          energy: this._parts("session_energy"),
          duration: Number.isNaN(elapsed) ? "—" : hhmm(elapsed),
          user: usable(this._entity("session_user")) ? this._entity("session_user").state : null,
        }
      : null;

    model.limits = [];
    const energyLimit = this._number("charge_energy_limit");
    if (energyLimit > 0) {
      model.limits.push({
        key: "charge_energy_limit",
        label: t(hass, "energy_limit"),
        text: `${Number.isNaN(energy) ? "—" : energy.toFixed(1)} / ${this._format("charge_energy_limit")}`,
        pct: Number.isNaN(energy) ? 0 : clamp((energy / energyLimit) * 100),
      });
    }
    const durationLimit = durationMinutes(this._entity("charge_duration_limit")?.state);
    if (durationLimit > 0) {
      model.limits.push({
        key: "charge_duration_limit",
        label: t(hass, "duration_limit"),
        text: `${Number.isNaN(elapsed) ? "—" : hhmm(elapsed)} / ${hhmm(durationLimit)}`,
        pct: Number.isNaN(elapsed) ? 0 : clamp((elapsed / durationLimit) * 100),
      });
    }

    const soc = this._entity("state_of_charge") ?? this._entity("vehicle_soc");
    if (usable(soc)) {
      model.limits.unshift({
        key: this._ids.state_of_charge ? "state_of_charge" : "vehicle_soc",
        label: usable(this._entity("vehicle_name")) ? this._entity("vehicle_name").state : "SoC",
        text: hass.formatEntityState(soc),
        pct: clamp(Number(soc.state)),
      });
    }

    if (this._config.show_controls === false) return model;

    const limit = this._entity("charging_current_limit");
    if (usable(limit)) {
      const value = Number(limit.state);
      model.limit = {
        value,
        min: Number(limit.attributes.min ?? 0),
        max: Number(limit.attributes.max ?? 32),
        step: Number(limit.attributes.step ?? 1),
        text: value === 0 ? t(hass, "paused") : hass.formatEntityState(limit),
        allowed: this._ids.allowed_charging_current ? this._format("allowed_charging_current") : null,
      };
    }

    const options = (key) => {
      const sel = this._entity(key);
      if (!usable(sel)) return null;
      return {
        current: sel.state,
        options: (sel.attributes.options || []).map((o) => ({ value: o, label: hass.formatEntityState(sel, o) })),
      };
    };
    model.mode = options("charge_mode");
    model.phaseSwitch = options("phases");

    const boost = this._entity("boost_mode");
    if (usable(boost)) model.boost = boost.state === "on";

    // Buttons stay "unknown" until first pressed.
    const action = charging ? "stop_charging" : "start_charging";
    if (plugged && this._entity(action) && this._entity(action).state !== "unavailable") {
      model.action = { key: action, label: t(hass, charging ? "stop" : "start"), icon: charging ? "mdi:stop" : "mdi:play" };
    }
    return model;
  }

  // ---- rendering ----

  _render() {
    if (!this.shadowRoot || !this._hass || !this._config || this._dragging) return;
    const model = this._model();
    const signature = JSON.stringify(model);
    if (signature === this._signature) return;
    this._signature = signature;

    if (model.message) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="message">${MARK}${esc(model.message)}</div></ha-card>`;
      return;
    }
    const hass = this._hass;

    const group = (attr, data, label) => `
      <div class="field">
        <div class="caption">${esc(label)}</div>
        <div class="group" role="radiogroup" aria-label="${esc(label)}">
          ${data.options
            .map(
              (o) => `<button data-${attr}="${esc(o.value)}" role="radio" aria-checked="${o.value === data.current}"
                class="${o.value === data.current ? "on" : ""}">${esc(o.label)}</button>`,
            )
            .join("")}
        </div>
      </div>`;

    const stat = (label, value, unit = "", more = "") => `
      <div class="stat" ${more ? `data-more="${more}"` : ""}>
        <div class="caption">${esc(label)}</div>
        <div class="value">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ""}</div>
      </div>`;

    this.shadowRoot.innerHTML = `${STYLE}
      <ha-card style="--state: ${model.color}" class="${model.unavailable ? "unavailable" : ""}">
        <div class="header">
          <div class="mark ${model.charging ? "live" : ""}" data-more="charger_state">${MARK}</div>
          <div class="title">
            <div class="caption">${esc(model.model)}</div>
            <div class="name">${esc(model.name)}</div>
          </div>
          ${
            model.action
              ? `<button class="action" data-press="${model.action.key}">
                  <ha-icon icon="${model.action.icon}"></ha-icon><span>${esc(model.action.label)}</span></button>`
              : ""
          }
        </div>

        ${
          model.error
            ? `<div class="error" data-more="${this._ids.error_state ? "error_state" : "charger_state"}">
                <span class="caption">${esc(t(hass, "error"))}</span><span>${esc(model.error)}</span></div>`
            : `<div class="steps" data-more="charger_state">
                ${model.steps
                  .map((s) => `<div class="step ${s.active ? "on" : ""}"><i></i><span>${esc(s.label)}</span></div>`)
                  .join("")}
              </div>`
        }

        <div class="readout">
          ${
            model.hasPower
              ? `<div class="power" data-more="power_active_l_sum_im_ex_diff">
                  <div class="caption">${esc(t(hass, "power"))}</div>
                  <div class="big">${esc(model.power.value)}<small>${esc(model.power.unit)}</small></div>
                </div>`
              : "<div></div>"
          }
          <div class="phases" data-more="${model.phases[0].key || "phases_active"}" aria-label="${esc(t(hass, "phases"))}">
            ${model.phases
              .map(
                (p) => `<div class="phase ${p.lit ? "on" : ""}"><i></i><span>${p.label}</span>${
                  p.amps ? `<small>${esc(p.amps)}</small>` : ""
                }</div>`,
              )
              .join("")}
          </div>
        </div>

        ${
          model.session
            ? `<div class="stats">
                ${stat(t(hass, "session"), model.session.energy.value, model.session.energy.unit, "session_energy")}
                ${stat(t(hass, "duration"), model.session.duration, "h", "session_start")}
                ${model.session.user ? stat(t(hass, "user"), model.session.user, "", "session_user") : ""}
              </div>`
            : ""
        }

        ${model.limits
          .map(
            (l) => `<div class="meter" data-more="${l.key}">
              <div class="row"><span class="caption">${esc(l.label)}</span><span>${esc(l.text)}</span></div>
              <div class="track"><div style="width:${l.pct}%"></div></div>
            </div>`,
          )
          .join("")}

        ${
          model.limit
            ? `<div class="field">
                <div class="row">
                  <span class="caption">${esc(t(hass, "limit"))}</span>
                  <span><b class="limit-value">${esc(model.limit.text)}</b>${
                    model.limit.allowed ? ` <small>· ${esc(model.limit.allowed)} ${esc(t(hass, "allowed"))}</small>` : ""
                  }</span>
                </div>
                <input type="range" min="${model.limit.min}" max="${model.limit.max}" step="${model.limit.step}"
                  value="${model.limit.value}" aria-label="${esc(t(hass, "limit"))}">
                <div class="scale"><span>${model.limit.min}</span><span>${model.limit.max} A</span></div>
              </div>`
            : ""
        }
        ${model.mode ? group("mode", model.mode, t(hass, "mode")) : ""}
        ${
          model.phaseSwitch || model.boost !== undefined
            ? `<div class="footer">
                ${model.phaseSwitch ? group("phase", model.phaseSwitch, t(hass, "phases")) : "<span></span>"}
                ${
                  model.boost !== undefined
                    ? `<button class="toggle ${model.boost ? "on" : ""}" data-toggle="boost_mode" aria-pressed="${model.boost}">
                        <ha-icon icon="mdi:rocket-launch-outline"></ha-icon>${esc(t(hass, "boost"))}</button>`
                    : ""
                }
              </div>`
            : ""
        }
      </ha-card>`;
  }

  // ---- interaction ----

  _call(domain, service, key, data = {}) {
    // On failure, drop the cached view so controls snap back to the real state.
    return this._hass.callService(domain, service, { entity_id: this._ids[key], ...data }).catch(() => {
      this._signature = undefined;
      this._render();
    });
  }

  _onClick(ev) {
    const el = ev.target.closest("[data-press],[data-toggle],[data-mode],[data-phase],[data-more]");
    if (!el) return;
    const { press, toggle, mode, phase, more } = el.dataset;
    if (press) this._call("button", "press", press);
    else if (toggle) this._call("switch", "toggle", toggle);
    else if (mode !== undefined) this._call("select", "select_option", "charge_mode", { option: mode });
    else if (phase !== undefined) this._call("select", "select_option", "phases", { option: phase });
    else if (more && this._ids[more]) {
      this.dispatchEvent(
        new CustomEvent("hass-more-info", { bubbles: true, composed: true, detail: { entityId: this._ids[more] } }),
      );
    }
  }

  _onInput(ev) {
    if (ev.target.type !== "range") return;
    // Hold re-renders while dragging; the timer covers a drag that ends without `change`.
    this._dragging = true;
    clearTimeout(this._dragTimer);
    this._dragTimer = setTimeout(() => this._endDrag(), 3000);
    const value = Number(ev.target.value);
    const label = this.shadowRoot.querySelector(".limit-value");
    if (label) label.textContent = value === 0 ? t(this._hass, "paused") : `${value} A`;
  }

  _endDrag() {
    this._dragging = false;
    this._signature = undefined;
    this._render();
  }

  _onChange(ev) {
    if (ev.target.type !== "range") return;
    clearTimeout(this._dragTimer);
    this._dragging = false;
    this._call("number", "set_value", "charging_current_limit", { value: Number(ev.target.value) });
  }
}

// Visual language of the WARP brand: monochrome, thin bars, square corners; color only for state.
const STYLE = `<style>
  :host { --line: var(--divider-color, rgba(127, 127, 127, 0.25)); --ink: var(--primary-text-color); }
  ha-card {
    padding: 20px; display: flex; flex-direction: column; gap: 18px;
    border-top: 3px solid var(--state); container-type: inline-size;
  }
  ha-card.unavailable > :not(.header) { opacity: 0.4; pointer-events: none; }
  .message { display: flex; align-items: center; gap: 12px; color: var(--secondary-text-color); }
  .message svg { width: 28px; flex: none; fill: currentColor; }
  [data-more] { cursor: pointer; }
  .caption {
    font-size: 0.68rem; font-weight: 500; letter-spacing: 0.14em; text-transform: uppercase;
    color: var(--secondary-text-color);
  }
  small { font-size: 0.75em; font-weight: 400; color: var(--secondary-text-color); margin-left: 3px; }

  .header { display: flex; align-items: center; gap: 14px; }
  .mark { width: 40px; flex: none; line-height: 0; }
  .mark svg { width: 100%; fill: var(--state); }
  .mark.live rect:nth-child(3), .mark.live rect:nth-child(4) { animation: post 1.6s ease-in-out infinite; }
  .mark.live rect:nth-child(4) { animation-delay: 0.3s; }
  @keyframes post { 50% { opacity: 0.35; } }
  @media (prefers-reduced-motion: reduce) { .mark.live rect { animation: none !important; } }
  .title { flex: 1; min-width: 0; }
  .title .caption { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .name { font-size: 1.25rem; font-weight: 400; letter-spacing: 0.02em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  button {
    font: inherit; color: var(--ink); cursor: pointer; background: transparent;
    border: 1px solid var(--line); border-radius: 0; padding: 6px 12px;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  }
  button ha-icon { --mdc-icon-size: 18px; }
  button:focus-visible { outline: 2px solid var(--state); outline-offset: 2px; }
  .action {
    border: 2px solid var(--state); color: var(--state); font-weight: 500;
    font-size: 0.75rem; letter-spacing: 0.1em; text-transform: uppercase; padding: 8px 12px;
  }
  .action:hover { background: var(--state); color: var(--card-background-color, #fff); }
  @container (max-width: 360px) { .action span { display: none; } }

  .steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
  .step { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .step i { display: block; height: 4px; background: var(--line); }
  .step span { font-size: 0.72rem; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .step.on i { background: var(--state); }
  .step.on span { color: var(--ink); font-weight: 500; }
  .error {
    display: flex; gap: 10px; align-items: baseline; padding: 8px 12px;
    border-left: 4px solid var(--state); background: color-mix(in srgb, var(--state) 10%, transparent);
  }
  .error .caption { color: var(--state); }

  .readout { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; }
  .big { font-size: 2.6rem; font-weight: 300; line-height: 1.05; font-variant-numeric: tabular-nums; }
  .phases { display: flex; gap: 10px; }
  .phase { display: flex; flex-direction: column; align-items: center; gap: 4px; min-width: 34px; }
  .phase i { display: block; width: 6px; height: 34px; background: var(--line); }
  .phase.on i { background: var(--state); }
  .phase span { font-size: 0.72rem; font-weight: 500; letter-spacing: 0.08em; color: var(--secondary-text-color); }
  .phase.on span { color: var(--ink); }
  .phase small { margin: 0; font-size: 0.68rem; white-space: nowrap; }

  .stats { display: flex; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
  .stat { flex: 1; min-width: 0; padding: 10px 12px; display: flex; flex-direction: column; gap: 2px; }
  .stat + .stat { border-left: 1px solid var(--line); }
  .stat:first-child { padding-left: 0; }
  .stat .value { font-size: 1.1rem; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-size: 0.9rem; }
  .meter, .field { display: flex; flex-direction: column; gap: 6px; }
  .track { height: 4px; background: var(--line); }
  .track div { height: 100%; background: var(--state); transition: width 0.4s; }

  input[type="range"] { -webkit-appearance: none; appearance: none; width: 100%; height: 22px; margin: 0; background: transparent; cursor: pointer; }
  input[type="range"]::-webkit-slider-runnable-track { height: 4px; background: var(--line); }
  input[type="range"]::-moz-range-track { height: 4px; background: var(--line); }
  input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none; width: 10px; height: 22px; margin-top: -9px; border: 0; border-radius: 0; background: var(--ink);
  }
  input[type="range"]::-moz-range-thumb { width: 10px; height: 22px; border: 0; border-radius: 0; background: var(--ink); }
  input[type="range"]:focus-visible { outline: 2px solid var(--state); outline-offset: 2px; }
  .scale { display: flex; justify-content: space-between; font-size: 0.68rem; color: var(--secondary-text-color); margin-top: -4px; }

  .group { display: flex; flex-wrap: wrap; }
  .group button { flex: 1 1 auto; margin-left: -1px; font-size: 0.85rem; white-space: nowrap; }
  .group button:first-child { margin-left: 0; }
  .group button.on { background: var(--ink); color: var(--card-background-color, #fff); border-color: var(--ink); position: relative; }
  .footer { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; }
  .toggle.on { border-color: var(--warning-color, #ffa600); color: var(--warning-color, #ffa600); }
</style>`;

class WarpChargerCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  _update() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => t(this._hass, schema.name);
      this._form.addEventListener("value-changed", (ev) => {
        this.dispatchEvent(
          new CustomEvent("config-changed", { bubbles: true, composed: true, detail: { config: ev.detail.value } }),
        );
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = { show_controls: true, ...this._config };
    this._form.schema = [
      { name: "device", required: true, selector: { device: { filter: { integration: DOMAIN } } } },
      { name: "name", selector: { text: {} } },
      { name: "show_controls", selector: { boolean: {} } },
    ];
  }
}

if (!customElements.get(CARD)) {
  customElements.define(CARD, WarpChargerCard);
  customElements.define(`${CARD}-editor`, WarpChargerCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD,
    name: "WARP Charger",
    description: "Status and controls for a Tinkerforge WARP charger.",
    preview: true,
    documentationURL: "https://github.com/schliflo/ha-warp-charger",
  });
}
