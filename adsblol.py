from math import atan2, cos, radians, sin, sqrt

import requests

from config import DEFAULT_LAT, DEFAULT_LON, DEFAULT_SEARCH_RANGE


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


def decode_flags(db_flags: int) -> list:
    return [
        name for bit, name in DB_FLAG_NAMES.items()
        if db_flags & bit
    ]


def decode_category(cat: str) -> str:
    if cat[0] in CATEGORY_NAMES:
        return CATEGORY_NAMES[cat[0]][int(cat[1])]


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
    ):
        """
        Calculates the distance of each aircraft from the given point (self.LAT, self.LON) and adds it to the aircraft data.
        """
        if single_point:
            items = data
        else:
            items = data['ac']
            

        for ac in items:
            if 'lat' in ac:
                lat = ac['lat']
                lon = ac['lon']
            elif 'rr_lat' in ac:
                lat = ac['rr_lat']
                lon = ac['rr_lon']
            elif 'lastPosition' in ac:
                lat = ac['lastPosition']['lat']
                lon = ac['lastPosition']['lon']
            else: 
                lat = lon = 0

            if lat != 0 and lon != 0:
                # Calculation
                dlat = radians(self.LAT - lat)
                dlon = radians(self.LON - lon)

                a = sin(dlat / 2)**2 + cos(radians(lat)) * cos(radians(self.LAT)) * sin(dlon / 2)**2
                c = 2 * atan2(sqrt(a), sqrt(1 - a))
                distance = EARTH_RADIUS_NM * c
            else: 
                distance = 100000

            # Add to dictionary for individual item
            ac['range'] = distance

        if not single_point:
            aircraft = data['ac']

            if max_range is not None:
                aircraft = [
                    ac for ac in aircraft
                    if ac['range'] <= max_range
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


    def _filter_format(self, data, max_range=None):
        """
        Filters the data so that all lists contain the same value
        GREAT FILTER
        """
        data_ranged = self._range_to_point(
            single_point=False,
            data=data,
            max_range=max_range,
        )
        acs = data_ranged['ac']

        new_data = []

        # The great filter
        for ac in acs:
        
            plane = {}

            plane['hex'] = ac['hex'].strip() if 'hex' in ac else "NA"

            if 'flight' in ac:
                callsign = ac['flight'].strip()
            elif 'callsign' in ac:
                callsign = ac['callsign'].strip()
            else:
                callsign = "NA"
            plane['callsign'] = callsign

            plane['t'] = ac['t'].strip() if 't' in ac else "NA"

            plane['r'] = ac['r'].strip() if 'r' in ac else "NA"

            if 'lat' and 'lon' in ac:
                lat = ac['lat']
                lon = ac['lon']
            elif 'rr_lat' and 'rr_lon' in ac:
                lat = ac['rr_lat']
                lon = ac['rr_lon']
            elif 'lastPosition' in ac:
                lat = ac['lastPosition']['lat']
                lon = ac['lastPosition']['lon']
            else: 
                lat = 0
                lon = 0

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

            plane['type'] = ac['type']

            plane['category'] = decode_category(ac['category']) if 'category' in ac else "NA"

            plane['flag'] = decode_flags(ac['dbFlags']) if 'dbFlags' in ac else ["NA"]
            
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


    def GET_flight_info(self, callsign, hex):
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

        # ADSBDB accepts either an ICAO or IATA callsign and includes the
        # airline plus both IATA and ICAO airport codes in a successful route.
        url = f"https://api.adsbdb.com/v0/callsign/{callsign}"
        response = requests.get(url, timeout=15)

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
        url = f"https://hexdb.io/api/v1/route/icao/{callsign}"
        response = requests.get(url, timeout=15)

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

        
    def GET_point(self, range):
        
        url = f"https://api.adsb.lol/v2/point/{self.LAT}/{self.LON}/{range}"

        response = requests.get(url, timeout=15)
        response.raise_for_status()
        response = response.json()
        data = self._filter_format(data=response, max_range=range)
        
        return data


    def GET_closest(self, range):
        """
        Returns closest aircraft to given point, set to 50mi range by default, cannot be changed.
        """

        url = f"https://api.adsb.lol/v2/closest/{self.LAT}/{self.LON}/{range}"
        
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        response = response.json()
        data = self._filter_format(data=response)
        
        return data

if __name__ == "__main__":
    adsb_lol = ADSB_LOL()
    data = adsb_lol.GET_all(range=30)
    for i in data:
        print(f"{i['callsign']} - {i['hex']} ({i['flag']}): {i['departure']}")
