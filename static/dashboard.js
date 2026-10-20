const config = window.ADSB_DISPLAY_CONFIG;
let currentCenter = [...config.mapCenter];
let currentSearchRangeNm = config.searchRangeNm;
const aircraftRefreshMs = config.aircraftRefreshMs;
const allAircraftUrl = config.allAircraftUrl;
const airportsUrl = config.airportsUrl;
const aircraftDetailUrlTemplate = config.aircraftDetailUrlTemplate;
const modeSettingsUrl = config.modeSettingsUrl;
const coordinateSettingsUrl = config.coordinateSettingsUrl;

const httpErrorIndicator = document.getElementById("http-error-indicator");
const httpErrorMessage = document.getElementById("http-error-message");
const failedRequestGroups = new Map();
const browserFetch = window.fetch.bind(window);

function requestGroup(input, init) {
    const method = String(
        (init && init.method) ||
        (input instanceof Request && input.method) ||
        "GET"
    ).toUpperCase();
    const rawUrl = input instanceof Request ? input.url : String(input);
    const url = new URL(rawUrl, window.location.href);
    let path = `${url.pathname}${url.search}`;

    if (/^\/api\/aircraft\/[^/]+$/.test(url.pathname)) {
        path = "/api/aircraft/:hex";
    }

    return `${method} ${path}`;
}

function updateHttpErrorIndicator() {
    if (!httpErrorIndicator || !httpErrorMessage) {
        return;
    }

    const failures = Array.from(failedRequestGroups.values());
    const latestFailure = failures.at(-1);

    if (!latestFailure) {
        httpErrorIndicator.hidden = true;
        httpErrorIndicator.removeAttribute("title");
        httpErrorMessage.textContent = "";
        return;
    }

    const additionalFailures = failures.length > 1
        ? `; ${failures.length - 1} other endpoint failure${
            failures.length === 2 ? "" : "s"
        }`
        : "";
    const message = (
        `HTTP ${latestFailure.status} ${latestFailure.statusText || "error"}` +
        ` from ${latestFailure.group}${additionalFailures}`
    );

    httpErrorIndicator.hidden = false;
    httpErrorIndicator.title = message;
    httpErrorMessage.textContent = message;
}

window.fetch = async function monitoredFetch(input, init) {
    const group = requestGroup(input, init);
    const response = await browserFetch(input, init);

    if (response.ok) {
        failedRequestGroups.delete(group);
    } else {
        // Reinsert the group so the most recently failed request is reported.
        failedRequestGroups.delete(group);
        failedRequestGroups.set(group, {
            group,
            status: response.status,
            statusText: response.statusText
        });
    }

    updateHttpErrorIndicator();
    return response;
};

const map = L.map("map", {
    minZoom: 7,
    maxZoom: 12,
    zoomSnap: 0.1,
    zoomDelta: 0.25,
    zoomControl: true
}).setView(currentCenter, 11);

// Leaflet listens for window resizes, but the map can also change size when
// responsive layout rules reflow the sidebar. Observe its panel directly so
// tiles and controls always fill the newly available space.
const mapPanel = document.querySelector(".right-panel");
let mapResizeFrame;
let mapResizeObserver;
if (mapPanel && "ResizeObserver" in window) {
    mapResizeObserver = new ResizeObserver(() => {
        window.cancelAnimationFrame(mapResizeFrame);
        mapResizeFrame = window.requestAnimationFrame(() => {
            map.invalidateSize({ pan: false });
        });
    });
    mapResizeObserver.observe(mapPanel);
}

L.tileLayer("/tiles/{z}/{x}/{y}.png", {
    minZoom: 7,
    maxZoom: 12,
    updateWhenIdle: true,
    updateWhenZooming: false,
    keepBuffer: 1,
    noWrap: true,
    attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">' +
        'OpenStreetMap contributors</a>'
}).addTo(map);

map.createPane("airportPane");
map.getPane("airportPane").style.zIndex = "450";

map.createPane("centerIndicatorPane");
map.getPane("centerIndicatorPane").style.zIndex = "625";
map.getPane("centerIndicatorPane").style.pointerEvents = "none";

const centerMarker = L.marker(currentCenter, {
    pane: "centerIndicatorPane",
    interactive: false,
    keyboard: false,
    icon: L.divIcon({
        className: "receiver-center-marker",
        html: "<span></span>",
        iconSize: [42, 42],
        iconAnchor: [21, 21]
    })
}).addTo(map);

const searchRadiusCircle = L.circle(currentCenter, {
    radius: currentSearchRangeNm * 1852,
    interactive: false,
    color: "#d6a04b",
    weight: 1.5,
    opacity: 0.82,
    dashArray: "8 7",
    fillColor: "#d6a04b",
    fillOpacity: 0.035
}).addTo(map);

const primaryPathOutline = L.polyline([], {
    interactive: false,
    color: "#080706",
    weight: 5,
    opacity: 0.78,
    lineCap: "round",
    lineJoin: "round"
}).addTo(map);

const primaryPathLine = L.polyline([], {
    interactive: false,
    color: "#d6a04b",
    weight: 2.25,
    opacity: 0.95,
    lineCap: "round",
    lineJoin: "round"
}).addTo(map);

function fitSearchRadius(animate = true) {
    map.invalidateSize();
    map.fitBounds(searchRadiusCircle.getBounds(), {
        padding: [26, 26],
        maxZoom: 11,
        animate
    });
}

fitSearchRadius(false);

const RecenterControl = L.Control.extend({
    options: { position: "topleft" },

    onAdd() {
        const container = L.DomUtil.create(
            "div",
            "leaflet-bar recenter-control"
        );
        const button = L.DomUtil.create("a", "", container);
        button.href = "#";
        button.title = "Recenter map";
        button.setAttribute("role", "button");
        button.setAttribute(
            "aria-label",
            "Recenter map on receiver location"
        );
        button.innerHTML = '<span class="recenter-icon" aria-hidden="true"></span>';

        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.on(button, "click", L.DomEvent.preventDefault);
        L.DomEvent.on(button, "click", () => {
            fitSearchRadius();
        });

        return container;
    }
});

new RecenterControl().addTo(map);

const aircraftMarkers = new Map();
const aircraftMotionStates = new Map();
const airportMarkers = new Map();
const expectedAircraftUpdateMs = aircraftRefreshMs;
let nearbyAirports = [];
let routeAirportCodes = new Set();

function displayModeSettings(settings) {
    for (const button of document.querySelectorAll("[data-mode]")) {
        const enabled = Boolean(settings[button.dataset.mode]);
        button.classList.toggle("is-active", enabled);
        button.setAttribute("aria-pressed", String(enabled));
    }

    const range = Number(settings.range_nm);

    if ([15, 25, 50].includes(range)) {
        currentSearchRangeNm = range;
        searchRadiusCircle.setRadius(range * 1852);

        for (const button of document.querySelectorAll("[data-range]")) {
            const selected = Number(button.dataset.range) === range;
            button.classList.toggle("is-active", selected);
            button.setAttribute("aria-pressed", String(selected));
        }
    }
}

async function loadModeSettings() {
    try {
        const response = await fetch(modeSettingsUrl);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        displayModeSettings(await response.json());
    } catch (error) {
        console.error("Could not retrieve feed settings:", error);
    }
}

function setButtonsPending(buttons, isPending) {
    for (const button of buttons) {
        button.classList.toggle("is-pending", isPending);
        button.disabled = isPending;
    }
}

async function toggleMode(event) {
    const button = event.currentTarget;
    const buttons = document.querySelectorAll("[data-mode]");
    const mode = button.dataset.mode;
    const enabled = button.getAttribute("aria-pressed") !== "true";
    setButtonsPending(buttons, true);

    try {
        const response = await fetch(modeSettingsUrl, {
            method: "PATCH",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ [mode]: enabled })
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        displayModeSettings(await response.json());
    } catch (error) {
        console.error(`Could not update ${mode} feed:`, error);
        await loadModeSettings();
    } finally {
        setButtonsPending(buttons, false);
    }
}

for (const button of document.querySelectorAll("[data-mode]")) {
    button.addEventListener("click", toggleMode);
}

async function selectSearchRange(event) {
    const selectedRange = Number(event.currentTarget.dataset.range);
    const buttons = document.querySelectorAll("[data-range]");

    if (selectedRange === currentSearchRangeNm) {
        return;
    }

    setButtonsPending(buttons, true);

    try {
        const response = await fetch(modeSettingsUrl, {
            method: "PATCH",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ range_nm: selectedRange })
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        displayModeSettings(await response.json());
        fitSearchRadius();
        await Promise.all([
            loadAirports(),
            updateAircraftDisplay()
        ]);
    } catch (error) {
        console.error("Could not update aircraft search range:", error);
        await loadModeSettings();
    } finally {
        setButtonsPending(buttons, false);
    }
}

for (const button of document.querySelectorAll("[data-range]")) {
    button.addEventListener("click", selectSearchRange);
}

const coordinateDialog = document.getElementById("coordinate-dialog");
const coordinateForm = document.getElementById("coordinate-form");
const coordinateButton = document.getElementById("coords-button");
const coordinateClose = document.getElementById("coordinate-close");
const latitudeInput = document.getElementById("coordinate-lat");
const longitudeInput = document.getElementById("coordinate-lon");
const coordinateKeypad = document.getElementById("coordinate-keypad");
const coordinateKeypadLabel = document.getElementById(
    "coordinate-keypad-label"
);
const localReceiverInput = document.getElementById("local-receiver-enabled");
const receiverAddressInput = document.getElementById("receiver-address");
const receiverBeastPortInput = document.getElementById("receiver-beast-port");
const adsblolInput = document.getElementById("adsblol-enabled");
const deviceLocationButton = document.getElementById("device-location-button");
const saveCoordinatesButton = document.getElementById("save-coordinates-button");
const coordinateStatus = document.getElementById("coordinate-status");
let activeCoordinateInput = null;

function setCoordinateStatus(message, isError = false) {
    coordinateStatus.textContent = message;
    coordinateStatus.classList.toggle("is-error", isError);
}

function readCoordinateInput(input, minimum, maximum, label) {
    const text = input.value.trim();
    const value = Number(text);

    if (
        text === "" ||
        !Number.isFinite(value) ||
        value < minimum ||
        value > maximum
    ) {
        throw new Error(`${label} must be between ${minimum} and ${maximum}.`);
    }

    return value;
}

function showCoordinateKeypad(input) {
    activeCoordinateInput = input;
    coordinateKeypadLabel.textContent = input === latitudeInput
        ? "Latitude"
        : "Longitude";
    coordinateKeypad.hidden = false;
}

function replaceCoordinateSelection(input, text) {
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? start;
    input.setRangeText(text, start, end, "end");
    input.dispatchEvent(new Event("input", { bubbles: true }));
}

function handleCoordinateKeypad(event) {
    const button = event.target.closest("button");
    if (!button || !activeCoordinateInput) {
        return;
    }

    const key = button.dataset.coordinateKey;
    const action = button.dataset.coordinateAction;

    if (key !== undefined) {
        if (key !== "." || !activeCoordinateInput.value.includes(".")) {
            replaceCoordinateSelection(activeCoordinateInput, key);
        }
    } else if (action === "clear") {
        activeCoordinateInput.value = "";
        activeCoordinateInput.dispatchEvent(
            new Event("input", { bubbles: true })
        );
    } else if (action === "sign") {
        activeCoordinateInput.value = activeCoordinateInput.value.startsWith("-")
            ? activeCoordinateInput.value.slice(1)
            : `-${activeCoordinateInput.value}`;
        activeCoordinateInput.dispatchEvent(
            new Event("input", { bubbles: true })
        );
    } else if (action === "backspace") {
        const start = activeCoordinateInput.selectionStart ?? 0;
        const end = activeCoordinateInput.selectionEnd ?? start;
        if (start !== end) {
            activeCoordinateInput.setRangeText("", start, end, "end");
        } else if (start > 0) {
            activeCoordinateInput.setRangeText(
                "",
                start - 1,
                start,
                "end"
            );
        }
        activeCoordinateInput.dispatchEvent(
            new Event("input", { bubbles: true })
        );
    } else if (action === "done") {
        coordinateKeypad.hidden = true;
        activeCoordinateInput = null;
    }
}

function displayCoordinates(coordinates) {
    // Device geolocation only supplies lat/lon. Do not erase receiver and
    // adsb.lol settings when applying that partial update to the form.
    if (coordinates.lat !== undefined) {
        latitudeInput.value = Number(coordinates.lat).toFixed(6);
    }
    if (coordinates.lon !== undefined) {
        longitudeInput.value = Number(coordinates.lon).toFixed(6);
    }
    if (coordinates.local_receiver_enabled !== undefined) {
        localReceiverInput.checked = Boolean(
            coordinates.local_receiver_enabled
        );
    }
    if (coordinates.receiver_address !== undefined) {
        receiverAddressInput.value = coordinates.receiver_address;
    }
    if (coordinates.receiver_beast_port !== undefined) {
        receiverBeastPortInput.value = coordinates.receiver_beast_port;
    }
    if (coordinates.adsblol_enabled !== undefined) {
        adsblolInput.checked = Boolean(coordinates.adsblol_enabled);
    }
}

async function loadCoordinates() {
    const response = await fetch(coordinateSettingsUrl);

    if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
    }

    const coordinates = await response.json();
    displayCoordinates(coordinates);
    return coordinates;
}

async function openCoordinateDialog() {
    coordinateDialog.showModal();
    coordinateButton.classList.add("is-active");
    setCoordinateStatus("Loading saved center...");

    try {
        await loadCoordinates();
        setCoordinateStatus(
            "Source toggles save immediately. Apply other settings below."
        );
        latitudeInput.focus();
        latitudeInput.select();
    } catch (error) {
        console.error("Could not retrieve coordinates:", error);
        setCoordinateStatus("Could not load saved coordinates.", true);
    }
}

function closeCoordinateDialog() {
    coordinateDialog.close();
}

function useDeviceLocation() {
    if (!navigator.geolocation) {
        setCoordinateStatus(
            "This browser does not support device location.",
            true
        );
        return;
    }

    deviceLocationButton.disabled = true;
    setCoordinateStatus("Waiting for device location permission...");

    navigator.geolocation.getCurrentPosition(
        (position) => {
            displayCoordinates({
                lat: position.coords.latitude,
                lon: position.coords.longitude
            });
            setCoordinateStatus(
                "Location found. Review it, then apply coordinates."
            );
            deviceLocationButton.disabled = false;
        },
        (error) => {
            const messages = {
                1: "Location permission was denied.",
                2: "The device location is unavailable.",
                3: "The location request timed out."
            };
            setCoordinateStatus(
                messages[error.code] || "Could not get device location.",
                true
            );
            deviceLocationButton.disabled = false;
        },
        {
            enableHighAccuracy: true,
            timeout: 10000,
            maximumAge: 60000
        }
    );
}

async function saveCoordinates(event) {
    event.preventDefault();

    if (!coordinateForm.reportValidity()) {
        return;
    }

    let latitude;
    let longitude;
    try {
        latitude = readCoordinateInput(latitudeInput, -90, 90, "Latitude");
        longitude = readCoordinateInput(
            longitudeInput,
            -180,
            180,
            "Longitude"
        );
    } catch (error) {
        setCoordinateStatus(error.message, true);
        return;
    }

    saveCoordinatesButton.disabled = true;
    setCoordinateStatus("Saving center coordinates...");

    try {
        const response = await fetch(coordinateSettingsUrl, {
            method: "PATCH",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                lat: latitude,
                lon: longitude,
                local_receiver_enabled: localReceiverInput.checked,
                receiver_address: receiverAddressInput.value.trim(),
                receiver_beast_port: Number(receiverBeastPortInput.value),
                adsblol_enabled: adsblolInput.checked
            })
        });
        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.error || `HTTP ${response.status}`);
        }

        displayCoordinates(result);
        currentCenter = [result.lat, result.lon];
        centerMarker.setLatLng(currentCenter);
        searchRadiusCircle.setLatLng(currentCenter);
        fitSearchRadius();
        await loadAirports();
        setCoordinateStatus(
            "Center saved. The aircraft service will use it on its next cycle."
        );
    } catch (error) {
        console.error("Could not save coordinates:", error);
        setCoordinateStatus(error.message, true);
    } finally {
        saveCoordinatesButton.disabled = false;
    }
}

async function saveSourceToggle(input, settingName, label) {
    const enabled = input.checked;
    const payload = { [settingName]: enabled };
    const latitudeText = latitudeInput.value.trim();
    const longitudeText = longitudeInput.value.trim();
    const latitude = Number(latitudeText);
    const longitude = Number(longitudeText);

    // Include the displayed center for compatibility with a Flask process
    // that has not reloaded the partial-settings endpoint yet.
    if (
        latitudeText !== "" &&
        longitudeText !== "" &&
        Number.isFinite(latitude) &&
        Number.isFinite(longitude)
    ) {
        payload.lat = latitude;
        payload.lon = longitude;
    }

    input.disabled = true;
    setCoordinateStatus(`Saving ${label} setting...`);

    try {
        const response = await fetch(coordinateSettingsUrl, {
            method: "PATCH",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(payload)
        });
        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.error || `HTTP ${response.status}`);
        }

        displayCoordinates(result);
        setCoordinateStatus(
            `${label} ${enabled ? "enabled" : "disabled"}.`
        );
    } catch (error) {
        console.error(`Could not save ${label} setting:`, error);
        try {
            await loadCoordinates();
        } catch (loadError) {
            console.error("Could not restore saved settings:", loadError);
        }
        setCoordinateStatus(error.message, true);
    } finally {
        input.disabled = false;
    }
}

coordinateButton.addEventListener("click", openCoordinateDialog);
coordinateClose.addEventListener("click", closeCoordinateDialog);
deviceLocationButton.addEventListener("click", useDeviceLocation);
coordinateForm.addEventListener("submit", saveCoordinates);
latitudeInput.addEventListener("focus", () => {
    showCoordinateKeypad(latitudeInput);
});
longitudeInput.addEventListener("focus", () => {
    showCoordinateKeypad(longitudeInput);
});
coordinateKeypad.addEventListener("click", handleCoordinateKeypad);
localReceiverInput.addEventListener("change", () => {
    saveSourceToggle(
        localReceiverInput,
        "local_receiver_enabled",
        "Local receiver"
    );
});
adsblolInput.addEventListener("change", () => {
    saveSourceToggle(adsblolInput, "adsblol_enabled", "adsb.lol");
});
coordinateDialog.addEventListener("close", () => {
    coordinateButton.classList.remove("is-active");
    coordinateKeypad.hidden = true;
    activeCoordinateInput = null;
    setCoordinateStatus("");
});

function isMissing(value) {
    return value === null ||
        value === undefined ||
        value === "" ||
        value === "NA" ||
        value === "N/A";
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function displayValue(elementId, value, suffix = "") {
    const element = document.getElementById(elementId);
    element.textContent = isMissing(value) ? "N/A" : `${value}${suffix}`;
}

function displayNumber(elementId, value, decimals, suffix) {
    const number = Number(value);

    if (isMissing(value) || !Number.isFinite(number)) {
        displayValue(elementId, null);
        return;
    }

    displayValue(elementId, number.toFixed(decimals), suffix);
}

let visibleAircraft = [];
let primaryAircraftHex = null;
let primarySelectionIsManual = false;
let primaryRequestNumber = 0;
let aircraftRefreshInFlight = false;
let primaryPathHex = null;

function clearPrimaryAircraftPath() {
    primaryPathOutline.setLatLngs([]);
    primaryPathLine.setLatLngs([]);
    primaryPathHex = null;
}

function displayPrimaryAircraftPath(aircraft) {
    const positions = Array.isArray(aircraft && aircraft.path)
        ? aircraft.path
        : [];
    const path = positions
        .map((position) => [
            Number(position.lat),
            Number(position.lon)
        ])
        .filter(([latitude, longitude]) => (
            Number.isFinite(latitude) &&
            Number.isFinite(longitude) &&
            latitude !== 0 &&
            longitude !== 0
        ));

    primaryPathOutline.setLatLngs(path);
    primaryPathLine.setLatLngs(path);
    primaryPathHex = aircraft ? aircraft.hex : null;
}

function clearPrimaryAircraft() {
    displayValue("closest-callsign", null);
    displayValue("closest-t", null);
    displayValue("closest-range", null);
    displayValue("closest-alt_baro", null);
    displayValue("closest-gs", null);
    displayAircraftImage(null);
    displayRoute(null);
    clearPrimaryAircraftPath();
    setRouteAirportHighlights(null);
}

function displayAircraftImage(aircraft) {
    const image = document.getElementById("primary-aircraft-image");

    if (aircraft && aircraft.aircraft_image) {
        image.src = aircraft.aircraft_image;
        image.alt = isMissing(aircraft.t)
            ? "Generic aircraft reference photograph"
            : `${aircraft.t} aircraft reference photograph`;
        image.hidden = false;
    } else {
        image.hidden = true;
        image.removeAttribute("src");
        image.alt = "";
    }
}

function displayRoute(aircraft, isLoading = false) {
    const departure = document.getElementById("route-departure");
    const departureName = document.getElementById(
        "route-departure-name"
    );
    const destination = document.getElementById("route-destination");
    const destinationName = document.getElementById(
        "route-destination-name"
    );
    const logo = document.getElementById("route-airline-logo");
    const privateLabel = document.getElementById("route-private");
    const airlineText = document.getElementById("route-airline-text");

    if (isLoading) {
        departure.textContent = "...";
        departureName.textContent = "";
        destination.textContent = "...";
        destinationName.textContent = "";
    } else {
        departure.textContent = aircraft
            ? listValue(aircraft.departure)
            : "--";
        departureName.textContent = (
            aircraft && !isMissing(aircraft.departure_name)
        ) ? aircraft.departure_name : "";
        destination.textContent = aircraft
            ? listValue(aircraft.destination)
            : "--";
        destinationName.textContent = (
            aircraft && !isMissing(aircraft.destination_name)
        ) ? aircraft.destination_name : "";
    }

    if (aircraft && aircraft.airline_logo) {
        logo.src = aircraft.airline_logo;
        logo.alt = aircraft.airline && aircraft.airline !== "NA"
            ? `${aircraft.airline} logo`
            : "Airline logo";
        logo.hidden = false;
        privateLabel.hidden = true;
        airlineText.hidden = true;
        airlineText.textContent = "";
    } else {
        logo.hidden = true;
        logo.removeAttribute("src");
        logo.alt = "";
        const showPrivate = Boolean(
            aircraft && aircraft.is_private && !isLoading
        );
        const airlineName = aircraft && !isMissing(aircraft.airline)
            ? aircraft.airline
            : aircraft && !isMissing(aircraft.airline_icao)
                ? aircraft.airline_icao
                : "";

        privateLabel.hidden = !showPrivate;
        airlineText.textContent = airlineName;
        airlineText.hidden = Boolean(
            isLoading || showPrivate || !airlineName
        );
    }
}

function displayPrimaryAircraft(aircraft, routeIsLoading = false) {
    if (!aircraft) {
        clearPrimaryAircraft();
        return;
    }

    displayValue("closest-callsign", aircraft.callsign);
    displayValue("closest-t", aircraft.t);
    displayNumber("closest-range", aircraft.range, 1, " nm");
    displayNumber("closest-alt_baro", aircraft.alt_baro, 0, " ft");
    displayNumber("closest-gs", aircraft.gs, 1, " kt");
    displayAircraftImage(aircraft);
    displayRoute(aircraft, routeIsLoading);
}

function listValue(value) {
    return isMissing(value) ? "N/A" : String(value);
}

function formatAltitude(value) {
    const altitude = Number(value);

    if (isMissing(value) || !Number.isFinite(altitude)) {
        return "N/A";
    }

    return Math.round(altitude).toLocaleString("en-US");
}

function renderAircraftList(aircraft, excludedHex) {
    const list = document.getElementById("aircraft-list");
    const count = document.getElementById("aircraft-count");
    const tableAircraft = aircraft.filter(
        (item) => item.hex !== excludedHex
    );
    count.textContent = String(tableAircraft.length).padStart(2, "0");

    if (tableAircraft.length === 0) {
        const message = document.createElement("p");
        message.className = "list-message";
        message.textContent = aircraft.length
            ? "No other aircraft detected"
            : "No aircraft detected";
        list.replaceChildren(message);
        return;
    }

    const rows = document.createDocumentFragment();

    for (const item of tableAircraft) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "aircraft-row";
        row.dataset.aircraftHex = item.hex;
        row.setAttribute(
            "aria-label",
            `Select ${listValue(item.callsign)} as primary aircraft`
        );

        const callsign = document.createElement("span");
        callsign.className = "row-callsign";
        callsign.textContent = listValue(item.callsign);

        const type = document.createElement("span");
        type.className = "row-type";
        type.textContent = listValue(item.t);

        const altitude = document.createElement("span");
        altitude.className = "row-altitude";
        altitude.textContent = formatAltitude(item.alt_baro);

        row.append(callsign, type, altitude);
        row.addEventListener("click", () => {
            selectAircraft(item.hex);
        });
        rows.append(row);
    }

    list.replaceChildren(rows);
}

function airportCodes(airport) {
    return [
        airport.ident,
        airport.icao_code,
        airport.iata_code,
        airport.gps_code,
        airport.local_code
    ]
        .filter((code) => !isMissing(code))
        .map((code) => String(code).trim().toUpperCase());
}

function airportIsRouteAirport(airport) {
    return airportCodes(airport).some(
        (code) => routeAirportCodes.has(code)
    );
}

function airportIsVisible(airport) {
    const zoom = map.getZoom();

    if (airportIsRouteAirport(airport)) {
        return true;
    }

    if (["large_airport", "medium_airport"].includes(airport.type)) {
        return true;
    }

    if (airport.type === "small_airport") {
        return airport.scheduled_service ? zoom >= 8 : zoom >= 9;
    }

    return false;
}

function airportMapIcon(airport) {
    const routeClass = airportIsRouteAirport(airport)
        ? " is-route-airport"
        : "";
    const code = escapeHtml(airport.display_code || airport.ident);

    return L.divIcon({
        className: `airport-map-marker${routeClass}`,
        html:
            '<div class="airport-map-symbol">' +
            '<svg class="airport-map-glyph" viewBox="0 0 20 20" ' +
            'aria-hidden="true" focusable="false">' +
            '<g transform="rotate(-35 10 10)">' +
            '<rect class="airport-runway" x="6.2" y="1.5" width="7.6" height="17" rx="1.3"></rect>' +
            '<path class="airport-runway-marking" d="M7.7 3.6h4.6M7.7 16.4h4.6M10 5.6v1.6M10 9.2v1.6M10 12.8v1.6"></path>' +
            '</g>' +
            '</svg>' +
            `<span class="airport-map-label">${code}</span>` +
            '</div>',
        iconSize: [96, 18],
        iconAnchor: [9, 9]
    });
}

function formatAirportType(value) {
    return String(value || "airport")
        .replaceAll("_", " ")
        .replace(/\b\w/g, (character) => character.toUpperCase());
}

function airportPopup(airport) {
    const content = document.createElement("div");
    const code = document.createElement("h3");
    code.className = "airport-popup-code";
    code.textContent = airport.display_code || airport.ident;

    const name = document.createElement("p");
    name.className = "airport-popup-name";
    name.textContent = airport.name;
    content.append(code, name);

    const locationParts = [
        airport.municipality,
        airport.iso_region
    ].filter((part) => !isMissing(part));
    const details = [
        formatAirportType(airport.type),
        locationParts.join(", "),
        isMissing(airport.elevation_ft)
            ? ""
            : `${Number(airport.elevation_ft).toLocaleString("en-US")} ft elevation`,
        `${Number(airport.distance_nm).toFixed(1)} nm from center`
    ].filter(Boolean);

    for (const detail of details) {
        const line = document.createElement("p");
        line.className = "airport-popup-detail";
        line.textContent = detail;
        content.append(line);
    }

    return content;
}

function updateAirportMarkers() {
    const visibleAirportIds = new Set();

    for (const airport of nearbyAirports) {
        if (!airportIsVisible(airport)) {
            continue;
        }

        const airportId = String(airport.source_id);
        visibleAirportIds.add(airportId);
        let marker = airportMarkers.get(airportId);

        if (marker) {
            marker.setIcon(airportMapIcon(airport));
            marker.setLatLng([airport.lat, airport.lon]);
        } else {
            marker = L.marker([airport.lat, airport.lon], {
                pane: "airportPane",
                icon: airportMapIcon(airport),
                keyboard: true,
                title: `${airport.display_code} — ${airport.name}`
            });
            marker.bindPopup(airportPopup(airport), {
                className: "airport-map-popup",
                maxWidth: 280
            });
            marker.addTo(map);
            airportMarkers.set(airportId, marker);
        }
    }

    for (const [airportId, marker] of airportMarkers) {
        if (!visibleAirportIds.has(airportId)) {
            marker.remove();
            airportMarkers.delete(airportId);
        }
    }
}

function setRouteAirportHighlights(aircraft) {
    const nextCodes = new Set();

    if (aircraft) {
        for (const code of [
            aircraft.departure,
            aircraft.destination
        ]) {
            if (!isMissing(code)) {
                nextCodes.add(String(code).trim().toUpperCase());
            }
        }
    }

    routeAirportCodes = nextCodes;
    updateAirportMarkers();
}

async function loadAirports() {
    try {
        const response = await fetch(airportsUrl);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        nearbyAirports = await response.json();
        updateAirportMarkers();
    } catch (error) {
        console.error("Could not retrieve local airports:", error);
    }
}

function aircraftMarkerSize() {
    const zoom = map.getZoom();

    return zoom <= 7
        ? 13
        : zoom === 8
            ? 16
            : zoom === 9
                ? 19
                : zoom === 10
                    ? 22
                    : zoom === 11
                        ? 26
                        : 30;
}

function degreesToRadians(value) {
    return value * Math.PI / 180;
}

function radiansToDegrees(value) {
    return value * 180 / Math.PI;
}

function distanceBetweenPositions(start, end) {
    const earthRadiusNm = 3440.065;
    const startLat = degreesToRadians(start.lat);
    const endLat = degreesToRadians(end.lat);
    const deltaLat = endLat - startLat;
    const deltaLon = degreesToRadians(end.lon - start.lon);
    const haversine =
        Math.sin(deltaLat / 2) ** 2 +
        Math.cos(startLat) * Math.cos(endLat) *
        Math.sin(deltaLon / 2) ** 2;
    const arc = 2 * Math.atan2(
        Math.sqrt(haversine),
        Math.sqrt(Math.max(0, 1 - haversine))
    );
    return earthRadiusNm * arc;
}

function bearingBetweenPositions(start, end) {
    const startLat = degreesToRadians(start.lat);
    const endLat = degreesToRadians(end.lat);
    const deltaLon = degreesToRadians(end.lon - start.lon);
    const y = Math.sin(deltaLon) * Math.cos(endLat);
    const x =
        Math.cos(startLat) * Math.sin(endLat) -
        Math.sin(startLat) * Math.cos(endLat) * Math.cos(deltaLon);
    return (radiansToDegrees(Math.atan2(y, x)) + 360) % 360;
}

function projectPosition(position, track, speedKt, elapsedSeconds) {
    if (
        !Number.isFinite(track) ||
        !Number.isFinite(speedKt) ||
        speedKt <= 0 ||
        elapsedSeconds <= 0
    ) {
        return { lat: position.lat, lon: position.lon };
    }

    const earthRadiusNm = 3440.065;
    const angularDistance =
        (speedKt * elapsedSeconds / 3600) / earthRadiusNm;
    const bearing = degreesToRadians(track);
    const startLat = degreesToRadians(position.lat);
    const startLon = degreesToRadians(position.lon);
    const endLat = Math.asin(
        Math.sin(startLat) * Math.cos(angularDistance) +
        Math.cos(startLat) * Math.sin(angularDistance) *
        Math.cos(bearing)
    );
    const endLon = startLon + Math.atan2(
        Math.sin(bearing) * Math.sin(angularDistance) *
        Math.cos(startLat),
        Math.cos(angularDistance) -
        Math.sin(startLat) * Math.sin(endLat)
    );

    return {
        lat: radiansToDegrees(endLat),
        lon: ((radiansToDegrees(endLon) + 540) % 360) - 180
    };
}

function interpolatePosition(start, end, amount) {
    const longitudeDelta = ((end.lon - start.lon + 540) % 360) - 180;
    return {
        lat: start.lat + (end.lat - start.lat) * amount,
        lon: start.lon + longitudeDelta * amount
    };
}

function updateAircraftMotion(item, marker, latitude, longitude) {
    const now = Date.now();
    const reportedSpeed = isMissing(item.gs) ? NaN : Number(item.gs);
    const reportedTrack = isMissing(item.track)
        ? NaN
        : Number(item.track);
    const position = { lat: latitude, lon: longitude, receivedAt: now };
    let state = aircraftMotionStates.get(item.hex);

    if (!state) {
        state = {
            previous: null,
            current: position,
            speedKt: Number.isFinite(reportedSpeed) ? reportedSpeed : 0,
            track: Number.isFinite(reportedTrack) ? reportedTrack : NaN,
            correctionFrom: null,
            correctionStartedAt: 0
        };
        aircraftMotionStates.set(item.hex, state);
        return;
    }

    const positionChanged =
        Math.abs(state.current.lat - latitude) > 0.000001 ||
        Math.abs(state.current.lon - longitude) > 0.000001;

    if (!positionChanged) {
        return;
    }

    const elapsedSeconds = (now - state.current.receivedAt) / 1000;
    const distanceNm = distanceBetweenPositions(state.current, position);
    const observedSpeed = elapsedSeconds > 0
        ? distanceNm / elapsedSeconds * 3600
        : NaN;
    const observedTrack = bearingBetweenPositions(state.current, position);
    const observedMotionIsUsable =
        elapsedSeconds >= 1 &&
        elapsedSeconds <= 90 &&
        Number.isFinite(observedSpeed) &&
        observedSpeed <= 1500;
    const displayedPosition = marker.getLatLng();

    state.previous = state.current;
    state.current = position;
    state.speedKt = observedMotionIsUsable
        ? observedSpeed
        : Number.isFinite(reportedSpeed)
            ? reportedSpeed
            : 0;
    state.track = observedMotionIsUsable
        ? observedTrack
        : Number.isFinite(reportedTrack)
            ? reportedTrack
            : NaN;
    state.correctionFrom = {
        lat: displayedPosition.lat,
        lon: displayedPosition.lng
    };
    state.correctionStartedAt = now;
}

let lastMotionFrame = 0;

function animateAircraftMarkers(frameTime) {
    if (frameTime - lastMotionFrame >= 50) {
        const now = Date.now();
        const projectionLimitMs = Math.min(
            Math.max(expectedAircraftUpdateMs * 2, 15000),
            30000
        );
        const correctionDurationMs = Math.min(
            expectedAircraftUpdateMs * 0.2,
            1800
        );

        for (const [hex, state] of aircraftMotionStates) {
            const marker = aircraftMarkers.get(hex);

            if (!marker) {
                continue;
            }

            const elapsedMs = Math.min(
                Math.max(0, now - state.current.receivedAt),
                projectionLimitMs
            );
            const projected = projectPosition(
                state.current,
                state.track,
                state.speedKt,
                elapsedMs / 1000
            );
            let displayed = projected;

            if (state.correctionFrom) {
                const correctionProgress = Math.min(
                    1,
                    (now - state.correctionStartedAt) /
                        correctionDurationMs
                );
                const easedProgress =
                    correctionProgress * correctionProgress *
                    (3 - 2 * correctionProgress);
                displayed = interpolatePosition(
                    state.correctionFrom,
                    projected,
                    easedProgress
                );

                if (correctionProgress === 1) {
                    state.correctionFrom = null;
                }
            }

            marker.setLatLng([displayed.lat, displayed.lon]);
        }

        lastMotionFrame = frameTime;
    }

    window.requestAnimationFrame(animateAircraftMarkers);
}

function aircraftMapIcon(aircraft) {
    const heading = Number(aircraft.track);
    const rotation = Number.isFinite(heading) ? heading : 0;
    const typeLabel = escapeHtml(listValue(aircraft.t));
    const markerSize = aircraftMarkerSize();
    const selectedClass = aircraft.hex === primaryAircraftHex
        ? " is-selected"
        : "";
    const isHelicopter = aircraft.category === "Rotorcraft/Helicopter";
    const aircraftGlyph = isHelicopter
        ? '<svg class="aircraft-map-icon helicopter-map-icon" ' +
          'viewBox="0 0 32 32" aria-hidden="true" focusable="false">' +
          '<path class="helicopter-main-rotor" d="M1 10.8h13.8V2h2.4v8.8H31v2.4H17.2v9.1h-2.4v-9.1H1Z"></path>' +
          '<path class="helicopter-body" d="M16 3.2c-2.7 0-4.4 3.1-4.9 7.4l-.5 5.2c-.2 2.5 1.3 4.5 3.6 5.4l.5 5.8h-3v2.2h8.6V27h-3l.5-5.8c2.3-.9 3.8-2.9 3.6-5.4l-.5-5.2c-.5-4.3-2.2-7.4-4.9-7.4Z"></path>' +
          '<path class="helicopter-tailplane" d="M10.7 24.9h10.6v2.2H10.7Z"></path>' +
          "</svg>"
        : '<svg class="aircraft-map-icon" viewBox="0 0 32 32" ' +
          'aria-hidden="true" focusable="false">' +
          '<path d="M16 1.4c-1.15 0-1.72 1.12-1.9 2.8l-.86 7.63-9.92 5.54c-.82.46-1.16 1.22-.92 2.13l.5 1.9 10.03-2.77-.68 7.05-3.84 2.85.31 1.62L16 28.71l7.28 1.44.31-1.62-3.84-2.85-.68-7.05L29.1 21.4l.5-1.9c.24-.91-.1-1.67-.92-2.13l-9.92-5.54-.86-7.63c-.18-1.68-.75-2.8-1.9-2.8Z"></path>' +
          "</svg>";

    return L.divIcon({
        className: `aircraft-map-marker${selectedClass}`,
        html:
            '<div class="aircraft-map-symbol" ' +
            `style="--marker-size:${markerSize}px;` +
            `--aircraft-rotation:${rotation}deg">` +
            '<span class="aircraft-map-shape">' +
            aircraftGlyph +
            "</span>" +
            `<span class="aircraft-map-type">${typeLabel}</span>` +
            "</div>",
        iconSize: [markerSize + 76, markerSize],
        iconAnchor: [markerSize / 2, markerSize / 2]
    });
}

function updateAircraftMarkers(aircraft) {
    const currentHexes = new Set();

    for (const item of aircraft) {
        const latitude = Number(item.lat);
        const longitude = Number(item.lon);

        if (
            !item.hex ||
            !Number.isFinite(latitude) ||
            !Number.isFinite(longitude) ||
            latitude === 0 ||
            longitude === 0
        ) {
            continue;
        }

        currentHexes.add(item.hex);
        let marker = aircraftMarkers.get(item.hex);

        if (marker) {
            marker.setIcon(aircraftMapIcon(item));
            marker.unbindTooltip();
        } else {
            marker = L.marker([latitude, longitude], {
                icon: aircraftMapIcon(item),
                keyboard: true,
                title: listValue(item.callsign)
            });
            marker.on("click", () => selectAircraft(item.hex, true));
            marker.addTo(map);
            aircraftMarkers.set(item.hex, marker);
        }

        updateAircraftMotion(item, marker, latitude, longitude);

        const tooltip = document.createElement("span");
        tooltip.textContent = listValue(item.callsign);
        marker.bindTooltip(tooltip, {
            className: "aircraft-map-tooltip",
            direction: "top",
            offset: [0, -aircraftMarkerSize() / 2]
        });
    }

    for (const [hex, marker] of aircraftMarkers) {
        if (!currentHexes.has(hex)) {
            marker.remove();
            aircraftMarkers.delete(hex);
            aircraftMotionStates.delete(hex);
        }
    }
}

map.on("zoomend", () => {
    updateAirportMarkers();
    updateAircraftMarkers(visibleAircraft);
});

window.requestAnimationFrame(animateAircraftMarkers);

function aircraftDetailUrl(aircraftHex) {
    return aircraftDetailUrlTemplate.replace(
        "__HEX__",
        encodeURIComponent(aircraftHex)
    );
}

function returnToNearestAfterMissing(missingHex) {
    visibleAircraft = visibleAircraft.filter(
        (item) => item.hex !== missingHex
    );
    primarySelectionIsManual = false;
    document.getElementById("primary-heading").textContent =
        "Nearest contact";

    if (visibleAircraft.length === 0) {
        primaryAircraftHex = null;
        clearPrimaryAircraft();
        renderAircraftList([], null);
        return;
    }

    primaryAircraftHex = visibleAircraft[0].hex;
    renderAircraftList(visibleAircraft, primaryAircraftHex);
    loadPrimaryAircraft(primaryAircraftHex);
}

async function loadPrimaryAircraft(aircraftHex) {
    const requestNumber = ++primaryRequestNumber;
    const basicAircraft = visibleAircraft.find(
        (item) => item.hex === aircraftHex
    );

    if (primaryPathHex !== aircraftHex) {
        clearPrimaryAircraftPath();
        setRouteAirportHighlights(null);
    }

    displayPrimaryAircraft(basicAircraft, true);

    try {
        const response = await fetch(aircraftDetailUrl(aircraftHex));

        if (response.status === 404) {
            const error = new Error("Aircraft is no longer available");
            error.aircraftMissing = true;
            throw error;
        }

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const aircraft = await response.json();

        if (
            requestNumber === primaryRequestNumber &&
            aircraft.hex === primaryAircraftHex
        ) {
            displayPrimaryAircraft(aircraft);
            displayPrimaryAircraftPath(aircraft);
            setRouteAirportHighlights(aircraft);
        }
    } catch (error) {
        console.error("Could not retrieve selected aircraft:", error);

        if (requestNumber === primaryRequestNumber) {
            if (error.aircraftMissing) {
                returnToNearestAfterMissing(aircraftHex);
            } else {
                displayPrimaryAircraft(basicAircraft);
            }
        }
    }
}

function selectAircraft(aircraftHex, manual = true) {
    const aircraftExists = visibleAircraft.some(
        (item) => item.hex === aircraftHex
    );

    if (!aircraftExists) {
        return;
    }

    primaryAircraftHex = aircraftHex;
    primarySelectionIsManual = manual;
    document.getElementById("primary-heading").textContent = manual
        ? "Selected contact"
        : "Nearest contact";
    renderAircraftList(visibleAircraft, primaryAircraftHex);

    for (const item of visibleAircraft) {
        const marker = aircraftMarkers.get(item.hex);

        if (marker) {
            marker.setIcon(aircraftMapIcon(item));
        }
    }

    loadPrimaryAircraft(primaryAircraftHex);
}

window.selectAircraft = selectAircraft;

async function updateAircraftDisplay() {
    if (aircraftRefreshInFlight) {
        return;
    }

    aircraftRefreshInFlight = true;

    try {
        const response = await fetch(allAircraftUrl);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        visibleAircraft = await response.json();
        updateAircraftMarkers(visibleAircraft);

        if (visibleAircraft.length === 0) {
            primaryAircraftHex = null;
            primarySelectionIsManual = false;
            document.getElementById("primary-heading").textContent =
                "Nearest contact";
            clearPrimaryAircraft();
            renderAircraftList([], null);
            return;
        }

        const selectedAircraftStillExists = visibleAircraft.some(
            (item) => item.hex === primaryAircraftHex
        );

        if (!primarySelectionIsManual || !selectedAircraftStillExists) {
            primaryAircraftHex = visibleAircraft[0].hex;
            primarySelectionIsManual = false;
        }

        document.getElementById("primary-heading").textContent =
            primarySelectionIsManual
                ? "Selected contact"
                : "Nearest contact";
        renderAircraftList(visibleAircraft, primaryAircraftHex);
        await loadPrimaryAircraft(primaryAircraftHex);
    } catch (error) {
        console.error("Could not retrieve aircraft list:", error);
    } finally {
        aircraftRefreshInFlight = false;
    }
}

loadModeSettings();
loadAirports();
updateAircraftDisplay();
setInterval(updateAircraftDisplay, aircraftRefreshMs);
