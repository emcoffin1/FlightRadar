/* Run with node tests/basic_display_test.js. No browser packages required. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../static/basic.js"), "utf8");
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
function harness() {
    let now = 0, next = 0, writes = 0;
    const timers = new Map(), events = {}, nodes = {}, requests = [];
    const document = {
        hidden: false, body: {dataset: {}},
        getElementById(id) {
            return nodes[id] ||= {
                text: "", hidden: false, alt: "", attributes: {},
                get textContent() { return this.text; },
                set textContent(v) { writes++; this.text = v; },
                get src() { return this.attributes.src || ""; },
                set src(v) { this.attributes.src = v; },
                getAttribute(name) { return this.attributes[name] || null; },
                removeAttribute(name) { delete this.attributes[name]; }
            };
        },
        addEventListener(name, fn) { events[name] = fn; }
    };
    const window = {
        basicConfig: {url: "/api/aircraft/nearest", positionFreshMs: 20000, heartbeatFreshMs: 10000, clockSkewMs: 5000},
        setTimeout(fn, delay) { const id = ++next; timers.set(id, {at: now + delay, fn}); return id; },
        clearTimeout(id) { timers.delete(id); },
        addEventListener(name, fn) { events[name] = fn; }
    };
    vm.runInNewContext(source, {
        window, document, navigator: {onLine: true}, AbortController, performance: {now: () => now},
        fetch(url, options) {
            return new Promise((resolve, reject) => {
                const call = {url, signal: options.signal,
                    reply(payload) { resolve({ok: true, json: async () => payload}); },
                    reject};
                options.signal.addEventListener("abort", () => reject(new Error("aborted")));
                requests.push(call);
            });
        }
    });
    return {
        nodes, document, requests, events, timers,
        get writes() {return writes;},
        async tick(ms) {
            const end = now + ms;
            while (true) {
                const entry = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a,b) => a[1].at-b[1].at)[0];
                if (!entry) break;
                now = entry[1].at; timers.delete(entry[0]); entry[1].fn(); await flush();
            }
            now = end; await flush();
        }
    };
}
function payload(hex = "abc123", updates = {}) {
    return {generated_at: 1000, heartbeat_at: 1000, collector_fresh: true,
        aircraft: {hex, callsign: "TEST1", t: "C172", r: "N123AB", range: 2.14,
            alt_baro: 0, gs: null, track: 0, source: "local", position_observed_at: 1000,
            aircraft_image: "/static/aircraft_photos/c172.webp", route_status: "ready",
            departure: "LAX", departure_name: "Los Angeles International Airport",
            destination: "SFO", destination_name: "San Francisco International Airport",
            ...updates}};
}
(async () => {
    const view = harness();
    assert.equal(view.requests.length, 1);
    view.requests[0].reply(payload()); await flush();
    assert.equal(view.nodes.callsign.textContent, "TEST1");
    assert.equal(view.nodes.altitude.textContent, "0");
    assert.equal(view.nodes.speed.textContent, "—");
    assert.equal(view.nodes["aircraft-image"].src, "/static/aircraft_photos/c172.webp");
    assert.equal(view.nodes["aircraft-image"].hidden, false);
    assert.equal(view.nodes.departure.textContent, "LAX");
    assert.equal(view.nodes["departure-name"].textContent, "Los Angeles International Airport");
    assert.equal(view.nodes.destination.textContent, "SFO");
    assert.equal(view.document.body.dataset.liveSource, "local");
    assert.equal(view.requests[0].url, "/api/aircraft/nearest");
    const writes = view.writes;
    await view.tick(2000); view.requests[1].reply(payload()); await flush();
    assert.equal(view.writes, writes, "unchanged snapshots must not rewrite text");
    await view.tick(2000); view.requests[2].reply(payload("def456", {callsign: "NEXT2"})); await flush();
    assert.equal(view.nodes.callsign.textContent, "NEXT2");
    await view.tick(2000); view.requests[3].reply({...payload(), aircraft: null}); await flush();
    assert.equal(view.nodes.callsign.textContent, "No aircraft");
    assert.equal(view.nodes["aircraft-image"].hidden, true);
    assert.equal(view.nodes.departure.textContent, "—");
    await view.tick(2000); view.requests[4].reply(payload("abc123", {position_observed_at: 900})); await flush();
    assert.equal(view.nodes.callsign.textContent, "No aircraft", "stale positions are rejected");
    view.events.pagehide(); assert.equal(view.timers.size, 0);

    const pending = harness();
    await pending.tick(3000);
    assert.equal(pending.requests.length, 1, "requests must not overlap");
    await pending.tick(1000);
    assert.equal(pending.requests[0].signal.aborted, true);
    assert.equal(pending.nodes.status.textContent, "Connection lost");
    await pending.tick(2000); assert.equal(pending.requests.length, 2);
    pending.document.hidden = true; pending.events.visibilitychange(); await flush();
    assert.equal(pending.requests[1].signal.aborted, true);
    await pending.tick(100000); assert.equal(pending.requests.length, 2);
    pending.document.hidden = false; pending.events.visibilitychange();
    pending.requests[2].reply(payload()); await flush();
    assert.equal(pending.nodes.status.textContent, "Live");
    pending.events.pagehide(); assert.equal(pending.timers.size, 0);

    const expiry = harness();
    expiry.requests[0].reply({...payload(), heartbeat_at: 990.5}); await flush();
    await expiry.tick(501);
    assert.equal(expiry.nodes.status.textContent, "Data stale", "expiry must work independently of poll completion");
    expiry.events.pagehide();

    const remote = harness();
    remote.requests[0].reply({...payload(), collector_fresh: true,
        heartbeat_at: 1000, live_source: "remote",
        aircraft: {...payload().aircraft, source: "remote_closest"}}); await flush();
    assert.equal(remote.nodes.status.textContent, "Live");
    assert.equal(remote.nodes.source.textContent, "Remote source");
    assert.equal(remote.document.body.dataset.liveSource, "remote");
    remote.events.pagehide();
    console.log("Basic display: rendering, freshness, timeout, single-flight and lifecycle checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
