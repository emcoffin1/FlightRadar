(function (root) {
    "use strict";

    class AircraftSnapshotGuard {
        constructor(options) {
            this.positionFreshMs = options.positionFreshMs;
            this.failureGraceMs = options.failureGraceMs;
            this.now = options.now || Date.now;
            this.lastSuccessAt = 0;
            this.networkConnected = true;
            this.dataFresh = false;
        }

        accept(items, collectorFresh) {
            const receivedAt = this.now();
            this.lastSuccessAt = receivedAt;
            this.networkConnected = true;
            this.dataFresh = Boolean(collectorFresh);
            if (!Array.isArray(items) || !this.dataFresh) {
                return [];
            }
            return items.filter((item) => {
                const observedAt = Number(item.position_observed_at) * 1000;
                return item.position_fresh === true &&
                    Number.isFinite(observedAt) &&
                    observedAt <= receivedAt + 5000 &&
                    receivedAt - observedAt <= this.positionFreshMs;
            });
        }

        failed() {
            this.networkConnected = false;
            return this.lastSuccessAt === 0 ||
                this.now() - this.lastSuccessAt >= this.failureGraceMs;
        }
    }

    function aircraftTrailSignature(aircraft) {
        const positions = Array.isArray(aircraft && aircraft.path)
            ? aircraft.path
            : [];
        return JSON.stringify({
            hex: aircraft && aircraft.hex,
            segment: aircraft && aircraft.trail_segment_id,
            revision: aircraft && aircraft.trail_revision,
            path: positions.map((position) => [
                position.recorded_at,
                position.lat,
                position.lon,
                position.segment_id
            ])
        });
    }

    root.AircraftSnapshotGuard = AircraftSnapshotGuard;
    root.aircraftTrailSignature = aircraftTrailSignature;
}(typeof globalThis !== "undefined" ? globalThis : this));
