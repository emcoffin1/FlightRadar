(function (root) {
    "use strict";

    class AircraftDetailScheduler {
        constructor(options) {
            this.intervalMs = options.intervalMs;
            this.requestTimeoutMs = options.requestTimeoutMs;
            this.poll = options.poll;
            this.setTimeout = options.setTimeoutFn || root.setTimeout.bind(root);
            this.clearTimeout = options.clearTimeoutFn || root.clearTimeout.bind(root);
            this.AbortController = options.AbortControllerClass || root.AbortController;
            this.hidden = Boolean(options.hidden);
            this.selection = null;
            this.generation = 0;
            this.timer = null;
            this.active = null;
        }

        select(hex, callsign) {
            if (!hex) {
                this.clear();
                return true;
            }

            const next = {
                hex: String(hex),
                callsign: String(callsign || "").trim().toUpperCase()
            };
            const changed = !this.selection ||
                this.selection.hex !== next.hex ||
                this.selection.callsign !== next.callsign;

            if (changed) {
                this.selection = next;
                this.generation += 1;
                this._cancelTimer();
                this._cancelActive();
                this._schedule(0);
            } else if (!this.hidden && !this.timer && !this.active) {
                this._schedule(0);
            }
            return changed;
        }

        setVisible(visible) {
            const hidden = !visible;
            if (hidden === this.hidden) {
                return;
            }
            this.hidden = hidden;
            this._cancelTimer();
            if (hidden) {
                this._cancelActive();
            } else if (this.selection) {
                this._schedule(0);
            }
        }

        clear() {
            this.selection = null;
            this.generation += 1;
            this._cancelTimer();
            this._cancelActive();
        }

        isCurrent(hex, generation) {
            return Boolean(this.selection) &&
                this.selection.hex === String(hex) &&
                this.generation === generation;
        }

        _cancelTimer() {
            if (this.timer !== null) {
                this.clearTimeout(this.timer);
                this.timer = null;
            }
        }

        _cancelActive() {
            if (this.active) {
                this.active.controller.abort();
                this.clearTimeout(this.active.timeout);
                this.active = null;
            }
        }

        _schedule(delay) {
            if (this.hidden || !this.selection || this.timer !== null) {
                return;
            }
            this.timer = this.setTimeout(() => {
                this.timer = null;
                this._run();
            }, delay);
        }

        _run() {
            if (this.hidden || !this.selection || this.active) {
                return;
            }

            const selection = { ...this.selection };
            const generation = this.generation;
            const controller = new this.AbortController();
            const active = {
                controller,
                generation,
                timeout: this.setTimeout(
                    () => {
                        controller.signal.detailTimeout = true;
                        controller.abort();
                    },
                    this.requestTimeoutMs
                )
            };
            this.active = active;

            let result;
            try {
                result = this.poll(
                    selection.hex,
                    selection.callsign,
                    generation,
                    controller.signal
                );
            } catch (error) {
                result = Promise.reject(error);
            }

            const finish = () => {
                this.clearTimeout(active.timeout);
                if (this.active !== active) {
                    return;
                }
                this.active = null;
                if (this.isCurrent(selection.hex, generation)) {
                    this._schedule(this.intervalMs);
                }
            };
            Promise.resolve(result).then(finish, finish);
        }
    }

    root.AircraftDetailScheduler = AircraftDetailScheduler;
}(typeof globalThis !== "undefined" ? globalThis : this));
