const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/dashboard.js"), "utf8");
const context = {
    constrainedProfile: false, document: {hidden: false},
    aircraftMotionStates: new Map(), aircraftMarkers: new Map(),
    positionFreshMs: 20000, expectedAircraftUpdateMs: 1000,
};
const frames = new Map();
let next = 0;
context.window = {
    requestAnimationFrame(callback) { const id = ++next; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); }
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf("let lastMotionFrame = 0;"), source.indexOf("function aircraftMapIcon(")), context);
context.ensureAnimationLoop();
assert.equal(frames.size, 0, "empty map must not animate");
context.aircraftMotionStates.set("abc123", {});
context.ensureAnimationLoop(); context.ensureAnimationLoop();
assert.equal(frames.size, 1, "only one animation loop may be scheduled");
context.document.hidden = true;
const [id, callback] = [...frames][0]; frames.delete(id); callback(100);
assert.equal(frames.size, 0, "hidden pages must stop scheduling frames");
context.document.hidden = false;
context.ensureAnimationLoop(); assert.equal(frames.size, 1);
context.stopAnimationLoop(); assert.equal(frames.size, 0);
context.constrainedProfile = true;
context.ensureAnimationLoop(); assert.equal(frames.size, 0, "constrained pages must not animate");
console.log("Animation loop: hidden, empty, constrained and single-loop checks passed");
