from collections import Counter
from math import atan2, cos, radians, sin, sqrt
from threading import Lock
import time

import requests

from config import (
    DEFAULT_LAT,
    DEFAULT_LON,
    DEFAULT_SEARCH_RANGE,
    MESSAGE_FRESH_SECONDS,
    POSITION_FRESH_SECONDS,
    REMOTE_REQUEST_TIMEOUT_SECONDS,
)
from freshness import finite_number, is_fresh, observation_time, valid_absolute_time


EARTH_RADIUS_NM = 3440.065
DB_FLAG_NAMES = {
    1: "Military",
    2: "Interesting",
    4: "PIA",
    8: "LADD",
}
CATEGORY_NAMES = {
    "A": {
        0: "No Category Information",
        1: "Light Aircraft",
        2: "Small Aircraft",
        3: "Large Aircraft",
        4: "High Vortex Large Aircraft",
        5: "Heavy Aircraft",
        6: "High Performance Aircraft",
        7: "Rotorcraft/Helicopter",
    },
    "B": {
        0: "No Category Information",
        1: "Glider",
        2: "Lighter-than-Air",
        3: "Parachutist",
        4: "Ultralight",
        5: "Reserved",
        6: "Unmanned Aerial Vehicle (UAV)",
        7: "Space/Transatmospheric Vehicle",
    },
    "C": {
        0: "No Category Information",
        1: "Emergency Surface Vehicle",
        2: "Service Surface Vehicle",
        3: "Point Obstacle",
        4: "Cluster Obstacle",
        5: "Line Obstacle",
        6: "Reserved",
        7: "Reserved",
    },
}


class ExternalLookupDisabled(Exception):
    """Raised when route-network permission is revoked between providers."""


def decode_flags(db_flags: int) -> list:
    try:
        db_flags = int(db_flags)
    except (TypeError, ValueError):
        return []
    return [
        name for bit, name in DB_FLAG_NAMES.items()
        if db_flags & bit
    ]


def decode_category(cat: str) -> str:
    try:
        return CATEGORY_NAMES[cat[0]].get(
            int(cat[1]),
            "No Category Information",
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return "No Category Information"


class ADSB_LOL:
    def __init__(
        self,
        lat: float = DEFAULT_LAT,
        lon: float = DEFAULT_LON,
        range: float = DEFAULT_SEARCH_RANGE,
    ):
        """
        Temecula is default: 33.512561467820056, -117.11927031211901
        :param lat: Latitude [deg]
        :param lon: Longitude [deg]
        :param range: Range [nautical miles]
        """
        
        self.LAT = lat
        self.LON = lon
        self.RANGE = range
        self._request_counts = Counter()
        self._request_counts_lock = Lock()

    def _external_get(self, provider, url, **kwargs):
        try:
            response = requests.get(url, **kwargs)
        except requests.exceptions.RequestException:
            with self._request_counts_lock:
                self._request_counts[f"{provider}:exception"] += 1
            raise
        outcome = str(response.status_code)
        with self._request_counts_lock:
            self._request_counts[f"{provider}:{outcome}"] += 1
        return response

    def request_diagnostics(self):
        with self._request_counts_lock:
            return dict(self._request_counts)


    def GET_all(self, range=25, military=True, normal=True):
        """
        Return aircraft inside the point radius according to the selected feeds.

        The point endpoint already contains military aircraft, so one request is
        enough. The dbFlags military bit is used to make the Normal and Military
        toggles mutually useful without returning duplicate aircraft.
        """
        if not normal and not military:
            return []

        aircraft = self.GET_point(range=range)

        if normal and military:
            return aircraft

        if military:
            return [
                ac for ac in aircraft
                if "Military" in ac.get('flag', [])
            ]

        return [
            ac for ac in aircraft
            if "Military" not in ac.get('flag', [])
        ]


    def _range_to_point(
        self,
        data: dict,
        single_point: bool = False,
        max_range: float | None = None,
        *,
        reference_lat=None,
        reference_lon=None,
    ):
        """
        Calculates the distance of each aircraft from the given point (self.LAT, self.LON) and adds it to the aircraft data.
        """
        if single_point:
            items = data
        else:
            items = data['ac']
            

        center_lat = self.LAT if reference_lat is None else float(reference_lat)
        center_lon = self.LON if reference_lon is None else float(reference_lon)

        for ac in items:
            if 'lat' in ac and 'lon' in ac:
                lat = ac['lat']
                lon = ac['lon']
            else: 
                lat = lon = 0

            if lat != 0 and lon != 0:
                # Calculation
                dlat = radians(center_lat - lat)
                dlon = radians(center_lon - lon)

                a = sin(dlat / 2)**2 + cos(radians(lat)) * cos(radians(center_lat)) * sin(dlon / 2)**2
                c = 2 * atan2(sqrt(a), sqrt(1 - a))
                distance = EARTH_RADIUS_NM * c
            else:
                distance = None

            # Add to dictionary for individual item
            ac['range'] = distance

        if not single_point:
            aircraft = data['ac']

            if max_range is not None:
                aircraft = [
                    ac for ac in aircraft
                    if ac['range'] is None or ac['range'] <= max_range
                ]

            return {'ac': aircraft}
            
        else:
            return distance


    def _add_dbFlags(self, data):
        """
        Adds a new key to each aircraft in the data dictionary, which contains a list of decoded flags.
        :param data: Dictionary containing aircraft data.
        :return: Modified dictionary with decoded flags added.
        """
        for ac in data['ac']:
            if 'dbFlags' in ac:
                ac['decodedFlags'] = decode_flags(ac['dbFlags'])
            else:
                ac['decodedFlags'] = []

            if 'category' in ac:
                ac['decodedCategory'] = decode_category(ac['category'])
            else:
                ac['decodedCategory'] = "No Category Information"
        return data


    def _filter_format(
        self,
        data,
        max_range=None,
        *,
        source=None,
        fetched_at=None,
        reference_lat=None,
        reference_lon=None,
    ):
        """
        Filters the data so that all lists contain the same value
        GREAT FILTER
        """
        fetched_at = time.time() if fetched_at is None else float(fetched_at)
        source = source or data.get("source") or "local"
        generated_at = valid_absolute_time(
            data.get("now") or data.get("_snapshot_generated_at"),
            now=fetched_at,
        )
        if generated_at is None:
            raise ValueError("aircraft snapshot has an invalid timestamp")

        raw_aircraft = data.get("ac")
        if not isinstance(raw_aircraft, list):
            raise ValueError("aircraft snapshot does not contain an aircraft list")

        normalized_input = []
        for raw in raw_aircraft:
            if not isinstance(raw, dict):
                continue
            ac = dict(raw)
            message_observed_at = ac.get("_message_observed_at")
            if message_observed_at is None:
                message_observed_at = observation_time(
                    generated_at, ac.get("seen"), fetched_at=fetched_at
                )
            if not is_fresh(
                message_observed_at,
                MESSAGE_FRESH_SECONDS,
                now=fetched_at,
            ):
                continue
            position_observed_at = ac.get("_position_observed_at")
            if position_observed_at is None and "lat" in ac and "lon" in ac:
                position_observed_at = observation_time(
                    generated_at, ac.get("seen_pos"), fetched_at=fetched_at
                )

            ac["_message_observed_at"] = message_observed_at
            ac["_position_observed_at"] = position_observed_at
            ac["_snapshot_generated_at"] = generated_at
            ac["_source_fetched_at"] = fetched_at
            ac["_source"] = source

            if not is_fresh(
                position_observed_at,
                POSITION_FRESH_SECONDS,
                now=fetched_at,
            ):
                ac.pop("lat", None)
                ac.pop("lon", None)
            elif "lat" in ac and "lon" in ac:
                latitude = finite_number(ac.get("lat"))
                longitude = finite_number(ac.get("lon"))
                if (
                    latitude is None or longitude is None
                    or not -90 <= latitude <= 90
                    or not -180 <= longitude <= 180
                    or latitude == 0 or longitude == 0
                ):
                    ac.pop("lat", None)
                    ac.pop("lon", None)
                    ac["_position_observed_at"] = None
                else:
                    ac["lat"] = latitude
                    ac["lon"] = longitude
            normalized_input.append(ac)

        data_ranged = self._range_to_point(
            single_point=False,
            data={"ac": normalized_input},
            max_range=max_range,
            reference_lat=reference_lat,
            reference_lon=reference_lon,
        )
        acs = data_ranged['ac']

        new_data = []

        # The great filter
        for ac in acs:
        
            plane = {}

            aircraft_hex = str(ac.get("hex") or "").strip().lower()
            if not aircraft_hex:
                continue
            plane['hex'] = aircraft_hex

            if 'flight' in ac:
                callsign = ac['flight'].strip()
            elif 'callsign' in ac:
                callsign = ac['callsign'].strip()
            else:
                callsign = "NA"
            plane['callsign'] = callsign

            plane['t'] = str(ac.get('t') or "NA").strip() or "NA"

            plane['r'] = str(ac.get('r') or "NA").strip() or "NA"

            if 'lat' in ac and 'lon' in ac:
                lat = ac['lat']
                lon = ac['lon']
            else: 
                lat = None
                lon = None

            plane['lat'] = lat
            plane['lon'] = lon

            # plane['range'] = self._range_to_point(single_point=True, data=[ac])
            plane['range'] = ac['range']

            plane['alt_baro'] = ac['alt_baro'] if 'alt_baro' in ac else "NA"

            if 'track' in ac:
                track = ac['track']
            elif 'heading' in ac:
                track = ac['heading']
            elif 'true_heading' in ac:
                track = ac['true_heading']
            elif 'mag_heading' in ac:
                track = ac['mag_heading']
            else:
                track = "NA"
            plane['track'] = track

            if 'gs' in ac:
                gs = ac['gs']
            elif 'ias' in ac:
                gs = ac['ias']
            else:
                gs = "NA"
            plane['gs'] = gs 

            # readsb uses ``t`` for the ICAO aircraft type.  The public API
            # sometimes also supplies a descriptive ``type`` field.
            plane['type'] = ac.get('type', "NA")

            plane['category'] = decode_category(ac['category']) if 'category' in ac else "NA"

            plane['flag'] = decode_flags(ac['dbFlags']) if 'dbFlags' in ac else ["NA"]
            plane['message_observed_at'] = ac.get("_message_observed_at")
            plane['position_observed_at'] = ac.get("_position_observed_at")
            plane['snapshot_generated_at'] = generated_at
            plane['source_fetched_at'] = fetched_at
            plane['source'] = source
            plane['position_source'] = source if lat is not None else None
            
            # Route data is resolved only when the user selects this aircraft.
            # None is stored as SQL NULL so "not looked up yet" stays distinct
            # from a completed lookup which found no route ("NA").
            plane['airline'] = None
            plane['airline_icao'] = None
            plane['departure'] = None
            plane['departure_name'] = None
            plane['destination'] = None
            plane['destination_name'] = None

            new_data.append(plane)
                
        return new_data


    def GET_flight_info(self, callsign, hex, permission_check=None):
        """Look up and return route codes for one selected aircraft."""
        unavailable = {
            'airline': 'NA',
            'airline_icao': None,
            'departure': 'NA',
            'departure_name': 'NA',
            'destination': 'NA',
            'destination_name': 'NA',
        }

        callsign = str(callsign or '').strip().upper()
        if not callsign or callsign in {'NA', 'N/A'}:
            return unavailable

        if permission_check is not None and not permission_check():
            raise ExternalLookupDisabled()

        # ADSBDB accepts either an ICAO or IATA callsign and includes the
        # airline plus both IATA and ICAO airport codes in a successful route.
        url = f"https://api.adsbdb.com/v0/callsign/{callsign}"
        response = self._external_get("adsbdb", url, timeout=15)

        if response.status_code == 200:
            route = response.json().get('response', {}).get('flightroute')
            if isinstance(route, dict):
                airline_data = route.get('airline') or {}
                origin_data = route.get('origin') or {}
                destination_data = route.get('destination') or {}

                return {
                    'airline': airline_data.get('name') or 'NA',
                    'airline_icao': airline_data.get('icao'),
                    'departure': (
                        origin_data.get('iata_code')
                        or origin_data.get('icao_code')
                        or 'NA'
                    ),
                    'departure_name': (
                        origin_data.get('name')
                        or origin_data.get('municipality')
                        or 'NA'
                    ),
                    'destination': (
                        destination_data.get('iata_code')
                        or destination_data.get('icao_code')
                        or 'NA'
                    ),
                    'destination_name': (
                        destination_data.get('name')
                        or destination_data.get('municipality')
                        or 'NA'
                    ),
                }
        elif response.status_code != 404:
            response.raise_for_status()

        # HexDB's route lookup expects the ICAO-style callsign broadcast by
        # ADS-B (AAL123, SWA456, etc.), not an IATA flight number.
        if permission_check is not None and not permission_check():
            raise ExternalLookupDisabled()
        url = f"https://hexdb.io/api/v1/route/icao/{callsign}"
        response = self._external_get("hexdb", url, timeout=15)

        if response.status_code == 200:
            route = response.json().get('route')
            if isinstance(route, str) and '-' in route:
                airport_codes = [
                    code.strip() for code in route.split('-') if code.strip()
                ]
                if len(airport_codes) < 2:
                    return unavailable

                departure = airport_codes[0]
                destination = airport_codes[-1]
                prefix = ''.join(
                    character for character in callsign if character.isalpha()
                )[:3]

                return {
                    'airline': 'NA',
                    'airline_icao': prefix or None,
                    'departure': departure or 'NA',
                    'departure_name': 'NA',
                    'destination': destination or 'NA',
                    'destination_name': 'NA',
                }
        elif response.status_code != 404:
            response.raise_for_status()

        return unavailable

        
    def GET_point(self, range, provider="public", *, lat=None, lon=None):
        """Fetch one geographic snapshot from the selected adsb.lol API."""
        latitude = self.LAT if lat is None else float(lat)
        longitude = self.LON if lon is None else float(lon)
        if provider == "feeder":
            url = (
                "https://re-api.adsb.lol/"
                f"?circle={latitude},{longitude},{range}"
            )
            provider_name = "adsblol_re_api"
        elif provider == "public":
            url = f"https://api.adsb.lol/v2/point/{latitude}/{longitude}/{range}"
            provider_name = "adsblol_point"
        else:
            raise ValueError("remote provider must be public or feeder")

        response = self._external_get(
            provider_name,
            url,
            timeout=REMOTE_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        fetched_at = time.time()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("adsb.lol response is not a JSON object")
        aircraft = payload.get("aircraft" if provider == "feeder" else "ac")
        if not isinstance(aircraft, list):
            raise ValueError("adsb.lol response does not contain an aircraft list")
        normalized = dict(payload)
        normalized["ac"] = aircraft
        data = self._filter_format(
            data=normalized,
            max_range=range,
            source=f"remote_{provider}",
            fetched_at=fetched_at,
            reference_lat=latitude,
            reference_lon=longitude,
        )
        
        return data


    def GET_closest(self, range, *, lat=None, lon=None):
        """Return the closest fresh aircraft within ``range`` nautical miles."""
        latitude = self.LAT if lat is None else float(lat)
        longitude = self.LON if lon is None else float(lon)
        url = f"https://api.adsb.lol/v2/closest/{latitude}/{longitude}/{range}"

        response = self._external_get(
            "adsblol_closest",
            url,
            timeout=REMOTE_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        fetched_at = time.time()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("adsb.lol response is not a JSON object")
        if not isinstance(payload.get("ac"), list):
            raise ValueError("adsb.lol response does not contain an aircraft list")
        return self._filter_format(
            data=payload,
            source="remote_closest",
            fetched_at=fetched_at,
            reference_lat=latitude,
            reference_lon=longitude,
        )

if __name__ == "__main__":
    adsb_lol = ADSB_LOL()
    data = adsb_lol.GET_all(range=30)
    for i in data:
        print(f"{i['callsign']} - {i['hex']} ({i['flag']}): {i['departure']}")
