#!/usr/bin/gjs -m

import GLib from "gi://GLib";

const schedulerPath = GLib.build_filenamev([
    GLib.get_current_dir(), "static", "detail_scheduler.js"
]);
const [loaded, schedulerBytes] = GLib.file_get_contents(schedulerPath);
if (!loaded) {
    throw new Error(`Could not load ${schedulerPath}`);
}
(0, eval)(new TextDecoder().decode(schedulerBytes));
const freshnessPath = GLib.build_filenamev([
    GLib.get_current_dir(), "static", "freshness_state.js"
]);
const [freshnessLoaded, freshnessBytes] = GLib.file_get_contents(freshnessPath);
if (!freshnessLoaded) {
    throw new Error(`Could not load ${freshnessPath}`);
}
(0, eval)(new TextDecoder().decode(freshnessBytes));

function assert(condition, message) {
    if (!condition) {
        throw new Error(message);
    }
}

class FakeTimers {
    constructor() {
        this.nextId = 1;
        this.jobs = new Map();
    }

    setTimeout(callback, delay) {
        const id = this.nextId++;
        this.jobs.set(id, { callback, delay });
        return id;
    }

    clearTimeout(id) {
        this.jobs.delete(id);
    }

    count(delay) {
        return [...this.jobs.values()].filter((job) => job.delay === delay).length;
    }

    run(delay) {
        const entry = [...this.jobs.entries()].find(([, job]) => job.delay === delay);
        assert(entry, `No timer found for ${delay}ms`);
        this.jobs.delete(entry[0]);
        entry[1].callback();
    }
}

class FakeAbortController {
    constructor() {
        this.signal = { aborted: false, onabort: null };
    }

    abort() {
        if (this.signal.aborted) {
            return;
        }
        this.signal.aborted = true;
        if (this.signal.onabort) {
            this.signal.onabort();
        }
    }
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => {
        resolve = yes;
        reject = no;
    });
    return { promise, resolve, reject };
}

async function flushPromises() {
    await Promise.resolve();
    await Promise.resolve();
}

function makeScheduler(poll, timers = new FakeTimers()) {
    return {
        timers,
        scheduler: new AircraftDetailScheduler({
            intervalMs: 7000,
            requestTimeoutMs: 8000,
            poll,
            setTimeoutFn: timers.setTimeout.bind(timers),
            clearTimeoutFn: timers.clearTimeout.bind(timers),
            AbortControllerClass: FakeAbortController
        })
    };
}

async function testPeriodicStatusesAndNoOverlap() {
    const statuses = ["ready", "unavailable", "disabled"];
    const calls = [];
    const pending = [];
    const { scheduler, timers } = makeScheduler((hex) => {
        calls.push(hex);
        const request = deferred();
        pending.push(request);
        return request.promise;
    });

    scheduler.select("aaa111", "TEST1");
    timers.run(0);
    assert(calls.length === 1, "initial detail request did not start");
    scheduler.select("aaa111", "TEST1");
    assert(calls.length === 1 && timers.count(0) === 0,
        "a slow request overlapped");

    for (const status of statuses) {
        pending.at(-1).resolve({ route_status: status });
        await flushPromises();
        assert(timers.count(7000) === 1,
            `status ${status} did not schedule the detail cadence`);
        if (status !== statuses.at(-1)) {
            timers.run(7000);
            assert(calls.length === pending.length,
                `status ${status} did not poll again`);
        }
    }
}

async function testFailureAndTimeoutRetry() {
    let calls = 0;
    const { scheduler, timers } = makeScheduler((hex, callsign, generation, signal) => {
        calls += 1;
        if (calls === 1) {
            return Promise.reject(new Error("temporary"));
        }
        return new Promise((resolve, reject) => {
            signal.onabort = () => reject(Object.assign(new Error("timeout"), {
                name: "AbortError"
            }));
        });
    });

    scheduler.select("aaa111", "TEST1");
    timers.run(0);
    await flushPromises();
    assert(timers.count(7000) === 1, "failed request was not retried later");
    timers.run(7000);
    assert(calls === 2, "retry request did not start");
    timers.run(8000);
    await flushPromises();
    assert(timers.count(7000) === 1, "timed-out request was not retried later");
}

async function testVisibilitySelectionAndStaleResults() {
    const pending = [];
    const applied = [];
    let scheduler;
    const setup = makeScheduler((hex, callsign, generation) => {
        const request = deferred();
        pending.push({ hex, generation, request });
        return request.promise.then(() => {
            if (scheduler.isCurrent(hex, generation)) {
                applied.push(hex);
            }
        });
    });
    scheduler = setup.scheduler;
    const timers = setup.timers;

    scheduler.select("aaa111", "OLD1");
    scheduler.select("aaa111", "OLD1");
    assert(timers.count(0) === 1, "reselection multiplied immediate timers");
    timers.run(0);
    scheduler.select("bbb222", "NEW2");
    timers.run(0);
    pending[0].request.resolve();
    pending[1].request.resolve();
    await flushPromises();
    assert(applied.join(",") === "bbb222", "stale selection response was applied");

    scheduler.setVisible(false);
    scheduler.setVisible(false);
    assert(timers.count(7000) === 0, "hidden page retained a poll timer");
    scheduler.setVisible(true);
    scheduler.setVisible(true);
    assert(timers.count(0) === 1, "visibility changes multiplied timers");

    scheduler.clear();
    assert(timers.jobs.size === 0, "aircraft disappearance left scheduled work");
}

async function testCallsignChangeRefreshesImmediately() {
    const calls = [];
    const { scheduler, timers } = makeScheduler((hex, callsign) => {
        calls.push(`${hex}:${callsign}`);
        return Promise.resolve();
    });
    scheduler.select("aaa111", "OLD1");
    timers.run(0);
    await flushPromises();
    scheduler.select("aaa111", "NEW2");
    assert(timers.count(0) === 1, "callsign change did not schedule immediately");
    timers.run(0);
    assert(calls.join(",") === "aaa111:OLD1,aaa111:NEW2",
        "callsign refresh used stale selection details");
}

function testSnapshotFreshnessAndTrailRevision() {
    let now = 100000;
    const guard = new AircraftSnapshotGuard({
        positionFreshMs: 20000,
        failureGraceMs: 15000,
        now: () => now
    });
    const fresh = {
        hex: "aaa111",
        position_observed_at: 99,
        position_fresh: true
    };
    assert(guard.accept([fresh], true).length === 1,
        "fresh aircraft was rejected");
    now += 14000;
    assert(!guard.failed(), "one transient failure expired markers too early");
    now += 1001;
    assert(guard.failed(), "repeated snapshot failures did not expire markers");

    now = 200000;
    const old = {
        hex: "old111",
        position_observed_at: 150,
        position_fresh: true
    };
    assert(guard.accept([old], true).length === 0,
        "successful response revived old position data");
    assert(guard.accept([fresh], false).length === 0,
        "stale collector heartbeat was treated as fresh data");

    const base = {
        hex: "aaa111",
        trail_segment_id: 2,
        trail_revision: 5,
        path: [
            { recorded_at: 1, lat: 1, lon: 1, segment_id: 2 },
            { recorded_at: 2, lat: 2, lon: 2, segment_id: 2 },
            { recorded_at: 3, lat: 3, lon: 3, segment_id: 2 }
        ]
    };
    const changedInterior = {
        ...base,
        path: [base.path[0], { ...base.path[1], lat: 2.5 }, base.path[2]]
    };
    assert(
        aircraftTrailSignature(base) !== aircraftTrailSignature(changedInterior),
        "interior trail changes were not detected"
    );
    assert(
        aircraftTrailSignature(base) !== aircraftTrailSignature({
            hex: "aaa111", trail_segment_id: 2, trail_revision: 6, path: []
        }),
        "an empty replacement path did not invalidate the old trail"
    );
}

await testPeriodicStatusesAndNoOverlap();
await testFailureAndTimeoutRetry();
await testVisibilitySelectionAndStaleResults();
await testCallsignChangeRefreshesImmediately();
testSnapshotFreshnessAndTrailRevision();
print("detail scheduler behavioral tests passed");
