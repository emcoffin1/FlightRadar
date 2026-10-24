/* One text-only request at a time; no map, details, pictures or animation loop. */
(() => {
    "use strict";
    const config = window.basicConfig;
    let timer = null;
    let expiryTimer = null;
    let controller = null;
    let generation = 0;
    let active = false;
    const set = (id, value) => {
        const node = document.getElementById(id);
        const text = value === null || value === undefined || value === "" ? "—" : String(value);
        if (node.textContent !== text) node.textContent = text;
    };
    const number = (value, decimals = 0) => {
        if (value === null || value === undefined || value === "") return null;
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed.toFixed(decimals) : null;
    };
    function clear(status) {
        window.clearTimeout(expiryTimer);
        expiryTimer = null;
        for (const id of ["callsign", "type", "registration", "distance", "altitude", "speed", "track", "hex"]) set(id, null);
        set("callsign", "No aircraft");
        set("status", status);
        set("source", "Waiting for fresh positions");
        document.body.dataset.live = "false";
    }
    function render(payload) {
        window.clearTimeout(expiryTimer);
        const aircraft = payload.aircraft;
        // Use the server's clock for ages; phone/Pi clocks need not agree.
        const generated = Number(payload.generated_at) * 1000;
        const heartbeatAge = generated - Number(payload.heartbeat_at) * 1000;
        const age = aircraft ? generated - Number(aircraft.position_observed_at) * 1000 : NaN;
        const remaining = Math.min(config.positionFreshMs - age, config.heartbeatFreshMs - heartbeatAge);
        if (!payload.collector_fresh || !Number.isFinite(heartbeatAge) || heartbeatAge < -config.clockSkewMs) {
            clear("Receiver stale");
            return;
        }
        if (!aircraft || !Number.isFinite(remaining) || remaining <= 0 || age < -config.clockSkewMs) {
            clear("No fresh aircraft");
            return;
        }
        set("callsign", aircraft.callsign || aircraft.r || aircraft.hex);
        set("type", aircraft.t);
        set("registration", aircraft.r);
        set("distance", number(aircraft.range, 1));
        set("altitude", aircraft.alt_baro === "ground" ? "Ground" : number(aircraft.alt_baro));
        set("speed", number(aircraft.gs));
        set("track", number(aircraft.track));
        set("hex", aircraft.hex.toUpperCase());
        set("source", aircraft.source === "local" ? "Local receiver" : "Remote source");
        set("status", "Live");
        document.body.dataset.live = "true";
        expiryTimer = window.setTimeout(() => clear("Data stale"), remaining);
    }
    async function poll() {
        if (!active || document.hidden || controller) return;
        const token = generation;
        const requestController = new AbortController();
        controller = requestController;
        const timeout = window.setTimeout(() => requestController.abort(), 4000);
        const started = performance.now();
        try {
            const response = await fetch(config.url, { signal: requestController.signal, cache: "no-store" });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            if (active && token === generation && !document.hidden) {
                // Subtract time spent receiving a response from its freshness budget.
                payload.generated_at += (performance.now() - started) / 1000;
                render(payload);
            }
        } catch (error) {
            if (active && token === generation && !document.hidden) clear("Connection lost");
        } finally {
            window.clearTimeout(timeout);
            if (controller === requestController) controller = null;
            if (active && token === generation && !document.hidden) timer = window.setTimeout(poll, 2000);
        }
    }
    function stop() {
        active = false;
        generation += 1;
        window.clearTimeout(timer);
        window.clearTimeout(expiryTimer);
        if (controller) controller.abort();
        controller = null;
    }
    function start() {
        if (active || document.hidden) return;
        active = true;
        clear("Connecting");
        poll();
    }
    document.addEventListener("visibilitychange", () => document.hidden ? stop() : start());
    window.addEventListener("pagehide", stop);
    window.addEventListener("pageshow", start);
    start();
})();
