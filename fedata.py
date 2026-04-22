import asyncio, websockets, json, sqlite3, logging
from datetime import datetime
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from math import radians, sin, cos, sqrt, atan2, degrees

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = r"D:\Marine\AIS_Spoof_Detector\ais.db"
API_KEY = "5e64dbb5e05a04d49838d6852e1afd673d46c672"

KNOWN_TYPES = {0, 70,71,72,73,74,75,76,77,78,79, 80,81,82,83,84,85,86,87,88,89, 60,61,62,63,64,65,66,67,68,69}

_mmsi_type_cache: dict[int, int] = {}

def _load_type_cache_from_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute('SELECT mmsi, ship_type FROM mmsi_type_cache').fetchall()
        conn.close()
        for mmsi, stype in rows:
            _mmsi_type_cache[mmsi] = stype
        if rows:
            logger.info(f"Loaded {len(rows)} cached vessel types from DB")
    except Exception as e:
        logger.debug(f"Could not load type cache from DB: {e}")

MIN_SPEED_KN  = 0.1
MAX_GAP_HOURS = 2.0

# ====================== STEP 4: MAX SPEED PER VESSEL CLASS ======================
MAX_SPEED_KN = {
    "Cargo":            25.0,
    "Tanker":           20.0,
    "Passenger":        35.0,
    "High Speed Craft": 55.0,
    "Tug":              15.0,
    "default":          40.0
}

def get_max_speed(vtype: str) -> float:
    for key, val in MAX_SPEED_KN.items():
        if key.lower() in vtype.lower():
            return val
    return MAX_SPEED_KN["default"]

# ====================== GEOLOCATION & HELPERS ======================
_geolocator = Nominatim(user_agent="ais_listener_v1", timeout=8)
_loc_mem_cache: dict = {}
_last_nominatim_call = 0.0
_nominatim_lock: asyncio.Lock = None

def get_location(lat, lon):
    import time
    global _last_nominatim_call
    lat_r = round(lat, 2)
    lon_r = round(lon, 2)
    key = (lat_r, lon_r)
    if key in _loc_mem_cache:
        return _loc_mem_cache[key]
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute('SELECT location FROM location_cache WHERE lat_r=? AND lon_r=?', (lat_r, lon_r)).fetchone()
        conn.close()
        if row:
            _loc_mem_cache[key] = row[0]
            return row[0]
    except Exception:
        pass
    _last_nominatim_call = time.time()
    try:
        result = _geolocator.reverse(f"{lat},{lon}", language="en", exactly_one=True)
        if result:
            addr = result.raw.get("address", {})
            parts = [
                addr.get("city") or addr.get("town") or addr.get("village") or addr.get("suburb") or addr.get("county", ""),
                addr.get("state", ""),
                addr.get("country_code", "").upper()
            ]
            location = ", ".join(p for p in parts if p)
        else:
            location = f"{lat:.4f},{lon:.4f}"
    except (GeocoderTimedOut, GeocoderServiceError):
        location = f"{lat:.4f},{lon:.4f}"
    _loc_mem_cache[key] = location
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute('INSERT OR IGNORE INTO location_cache VALUES(?,?,?)', (lat_r, lon_r, location))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return location

async def get_location_async(lat, lon) -> str:
    import time
    lat_r = round(lat, 2)
    lon_r = round(lon, 2)
    key = (lat_r, lon_r)
    if key in _loc_mem_cache:
        return _loc_mem_cache[key]
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute('SELECT location FROM location_cache WHERE lat_r=? AND lon_r=?', (lat_r, lon_r)).fetchone()
        conn.close()
        if row:
            _loc_mem_cache[key] = row[0]
            return row[0]
    except Exception:
        pass
    async with _nominatim_lock:
        if key in _loc_mem_cache:
            return _loc_mem_cache[key]
        elapsed = time.time() - _last_nominatim_call
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, get_location, lat, lon)

def haversine(lat1, lon1, lat2, lon2):
    R = 3440.065
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def _parse_ts(ts_str: str):
    ts_str = ts_str.strip()
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1]
    else:
        ts_str = ts_str.replace(" +0000 UTC", "").replace("+00:00", "")
    if "." in ts_str:
        base, frac = ts_str.split(".", 1)
        ts_str = f"{base}.{frac[:6]}"
    return datetime.fromisoformat(ts_str)

def check_teleport(lat1, lon1, t1, lat2, lon2, t2):
    try:
        t1 = _parse_ts(t1)
        t2 = _parse_ts(t2)
    except Exception:
        return False
    hours = (t2 - t1).total_seconds() / 3600
    if hours <= 0 or hours > MAX_GAP_HOURS:
        return False
    distance_nm = haversine(lat1, lon1, lat2, lon2)
    implied_speed = distance_nm / hours
    return implied_speed > 35.0

def is_invalid_mmsi(mmsi: int) -> bool:
    s = str(mmsi)
    if len(s) != 9: return True
    if len(set(s)) == 1: return True
    if s in ("123456789", "987654321"): return True
    if len(set(s[::2])) == 1 and len(set(s[1::2])) == 1: return True
    return False

def compute_bearing(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360

MAX_ACCEL_KN_PER_MIN = {
    "Cargo":            0.5,
    "Tanker":           0.4,
    "Passenger":        1.5,
    "Tug":              2.0,
    "Towing":           0.8,
    "Military":         3.0,
    "High Speed Craft": 4.0,
}
DEFAULT_MAX_ACCEL = 1.0

def check_acceleration(speed1, t1_str, speed2, t2_str, vtype) -> bool:
    if speed1 == 0.0:
        return False
    try:
        t1 = _parse_ts(t1_str)
        t2 = _parse_ts(t2_str)
    except Exception:
        return False
    minutes = (t2 - t1).total_seconds() / 60
    if minutes <= 0 or minutes > 60:
        return False
    delta_speed = abs(speed2 - speed1)
    limit = DEFAULT_MAX_ACCEL
    for key, val in MAX_ACCEL_KN_PER_MIN.items():
        if key.lower() in vtype.lower():
            limit = val
            break
    return delta_speed / minutes > limit

def check_course_mismatch(lat1, lon1, lat2, lon2, reported_course, threshold_deg=45) -> bool:
    if reported_course is None:
        return False
    dist = haversine(lat1, lon1, lat2, lon2)
    if dist < 0.1:
        return False
    actual = compute_bearing(lat1, lon1, lat2, lon2)
    diff = abs(actual - reported_course)
    if diff > 180:
        diff = 360 - diff
    return diff > threshold_deg

# ====================== DB ======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('''CREATE TABLE IF NOT EXISTS positions
                    (mmsi INT, location TEXT, lat REAL, lon REAL,
                     speed REAL, course REAL, heading INT,
                     timestamp TEXT, vessel_type TEXT,
                     UNIQUE(mmsi, timestamp))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS vessel_last
                    (mmsi INT PRIMARY KEY,
                     lat REAL, lon REAL, timestamp TEXT,
                     speed REAL, course REAL)''')
    for col, coltype in [("speed", "REAL"), ("course", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE vessel_last ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass
    conn.execute('''CREATE TABLE IF NOT EXISTS mmsi_type_cache
                    (mmsi INT PRIMARY KEY, ship_type INT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS location_cache
                    (lat_r REAL, lon_r REAL, location TEXT,
                     PRIMARY KEY (lat_r, lon_r))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS anomalies
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     mmsi INT, anomaly_type TEXT,
                     detail TEXT, lat REAL, lon REAL,
                     timestamp TEXT)''')
    conn.commit()
    conn.close()

# ====================== save_position (with Step 4 checks) ======================
async def save_position(mmsi, lat, lon, speed, course, heading, ts, vtype, ship_type=0):
    if is_invalid_mmsi(mmsi):
        conn = sqlite3.connect(DB_FILE)
        conn.execute('INSERT INTO anomalies VALUES(NULL,?,?,?,?,?,?)',
                     (mmsi, "INVALID_MMSI", f"MMSI {mmsi} fails ITU format", lat, lon, ts))
        conn.commit()
        conn.close()
        logger.warning(f"🚨 INVALID MMSI {mmsi} | still recording position")

    location = await get_location_async(lat, lon)

    conn = sqlite3.connect(DB_FILE)
    prev = conn.execute('SELECT lat, lon, timestamp, speed, course FROM vessel_last WHERE mmsi=?', (mmsi,)).fetchone()

    if prev:
        prev_lat, prev_lon, prev_ts, prev_speed, prev_course = prev
        if check_teleport(prev_lat, prev_lon, prev_ts, lat, lon, ts):
            conn.execute('INSERT INTO anomalies VALUES(NULL,?,?,?,?,?,?)',
                         (mmsi, "TELEPORT", f"Jumped from {prev_lat:.4f},{prev_lon:.4f} to {lat:.4f},{lon:.4f}", lat, lon, ts))
            logger.warning(f"🚨 TELEPORT — MMSI {mmsi} | {location}")
        if prev_speed is not None and check_acceleration(prev_speed, prev_ts, speed, ts, vtype):
            conn.execute('INSERT INTO anomalies VALUES(NULL,?,?,?,?,?,?)',
                         (mmsi, "IMPOSSIBLE_ACCEL", f"Speed jumped {prev_speed:.1f}→{speed:.1f}kn ({vtype})", lat, lon, ts))
            logger.warning(f"🚨 ACCEL ANOMALY — MMSI {mmsi} | {prev_speed:.1f}→{speed:.1f}kn | {location}")
        if check_course_mismatch(prev_lat, prev_lon, lat, lon, course):
            actual_brg = compute_bearing(prev_lat, prev_lon, lat, lon)
            conn.execute('INSERT INTO anomalies VALUES(NULL,?,?,?,?,?,?)',
                         (mmsi, "COURSE_MISMATCH",
                          f"Reported course {course:.1f}° but actually moving {actual_brg:.1f}°" if course is not None else
                          f"No course reported but actually moving {actual_brg:.1f}°", lat, lon, ts))
            logger.warning(f"🚨 COURSE MISMATCH — MMSI {mmsi} | reported {course:.1f}° actual {actual_brg:.1f}° | {location}")

    conn.execute('INSERT OR REPLACE INTO vessel_last VALUES(?,?,?,?,?,?)', (mmsi, lat, lon, ts, speed, course))
    conn.execute('INSERT OR IGNORE INTO positions VALUES(?,?,?,?,?,?,?,?,?)',
                 (mmsi, location, lat, lon, speed, course, heading, ts, vtype))
    conn.commit()
    conn.close()

    # ====================== STEP 4 TRACK ANALYSIS ======================
    try:
        track = get_vessel_track(mmsi, limit=500)
        anoms = detect_anomalies(track)
        if anoms:
            conn = sqlite3.connect(DB_FILE)
            for a in anoms:
                recent = conn.execute(
                    """SELECT COUNT(*) FROM anomalies 
                       WHERE mmsi=? AND anomaly_type=? 
                       AND timestamp > datetime(?, '-60 minutes')""",
                    (mmsi, a['anomaly_type'], a['timestamp'])).fetchone()[0]
                if recent == 0:
                    conn.execute('INSERT INTO anomalies (mmsi, anomaly_type, detail, lat, lon, timestamp) VALUES (?,?,?,?,?,?)',
                                 (mmsi, a['anomaly_type'], a['detail'], a['lat'], a['lon'], a['timestamp']))
                    logger.warning(f"🚨 TRACK ANOMALY [{a['anomaly_type']}] sev={a['severity']:.2f} — MMSI {mmsi} | {a['detail']}")
            conn.commit()
            conn.close()
    except Exception as e:
        logger.debug(f"Track analysis failed for MMSI {mmsi}: {e}")

    logger.info(f"MMSI {mmsi} | {location} | {speed or 0:.1f}kn | {vtype}")

# ====================== VTYPE_MAP ======================
VTYPE_MAP = {
    20: "Wing-in-Ground", 21: "WIG Hazardous A", 22: "WIG Hazardous B",
    23: "WIG Hazardous C", 24: "WIG Hazardous D",
    30: "Fishing", 31: "Towing", 32: "Tug (Large Tow)",
    33: "Dredger", 34: "Dive Vessel", 35: "Military",
    36: "Sailing", 37: "Pleasure Craft", 38: "Reserved", 39: "Reserved",
    40: "High Speed Craft", 41: "HSC Hazardous A", 42: "HSC Hazardous B",
    43: "HSC Hazardous C", 44: "HSC Hazardous D", 49: "HSC (No Info)",
    50: "Pilot Vessel", 51: "Search & Rescue", 52: "Tug",
    53: "Port Tender", 54: "Anti-Pollution", 55: "Law Enforcement",
    57: "Medical Transport", 58: "Non-combatant", 59: "Passenger (No Info)",
    60: "Passenger", 61: "Passenger Hazardous A", 62: "Passenger Hazardous B",
    63: "Passenger Hazardous C", 64: "Passenger Hazardous D",
    65: "Passenger", 66: "Passenger", 67: "Passenger", 68: "Passenger",
    69: "Passenger (No Info)",
    70: "Cargo", 71: "Cargo Hazardous A", 72: "Cargo Hazardous B",
    73: "Cargo Hazardous C", 74: "Cargo Hazardous D",
    75: "Cargo", 76: "Cargo", 77: "Cargo", 78: "Cargo",
    79: "Cargo (No Info)",
    80: "Tanker", 81: "Tanker Hazardous A", 82: "Tanker Hazardous B",
    83: "Tanker Hazardous C", 84: "Tanker Hazardous D",
    85: "Tanker", 86: "Tanker", 87: "Tanker", 88: "Tanker",
    89: "Tanker (No Info)",
    90: "Other", 91: "Other Hazardous A", 92: "Other Hazardous B",
    93: "Other Hazardous C", 94: "Other Hazardous D",
    95: "Other", 96: "Other", 97: "Other", 98: "Other",
    99: "Other (No Info)",
}

# ====================== parse_ais ======================
def parse_ais(raw):
    try:
        data     = json.loads(raw)
        msg_type = data.get("MessageType")
        if msg_type == "ShipStaticData":
            inner_s  = data.get("Message", {}).get("ShipStaticData", {})
            meta_s   = data.get("MetaData", {})
            mmsi_s   = inner_s.get("UserID") or meta_s.get("MMSI")
            stype    = inner_s.get("Type") or inner_s.get("ShipType")
            name_s   = inner_s.get("Name", "").strip()
            if mmsi_s and stype is not None:
                mmsi_i = int(mmsi_s)
                stype_i = int(stype)
                _mmsi_type_cache[mmsi_i] = stype_i
                resolved_vtype = VTYPE_MAP.get(stype_i, f"Unknown (code {stype_i})")
                try:
                    _c = sqlite3.connect(DB_FILE)
                    _c.execute("INSERT OR REPLACE INTO mmsi_type_cache VALUES(?,?)", (mmsi_i, stype_i))
                    _c.execute("UPDATE positions SET vessel_type=? WHERE mmsi=?", (resolved_vtype, mmsi_i))
                    updated = _c.execute("SELECT changes()").fetchone()[0]
                    _c.commit(); _c.close()
                    if stype_i in KNOWN_TYPES and stype_i != 0:
                        if updated:
                            logger.debug(f"[TYPE] MMSI {mmsi_i} → {resolved_vtype}" + (f" ({name_s})" if name_s else "") + f" | backfilled {updated} position rows")
                        else:
                            logger.debug(f"[TYPE] MMSI {mmsi_i} → {resolved_vtype}" + (f" ({name_s})" if name_s else ""))
                except Exception as e:
                    logger.debug(f"ShipStaticData DB error: {e}")
            return None
        if msg_type not in ("PositionReport", "ExtendedClassBPositionReport", "StandardClassBPositionReport", "LongRangeAisBroadcastMessage"):
            return None
        inner = data.get("Message", {}).get(msg_type, {})
        meta  = data.get("MetaData", {})
        ts = meta.get("time_utc") or meta.get("TimeReceived") or meta.get("timestamp")
        if not ts: return None
        mmsi = inner.get("UserID") or meta.get("MMSI")
        lat  = inner.get("Latitude")
        lon  = inner.get("Longitude")
        if not mmsi or lat is None or lon is None:
            return None
        _sog    = inner.get("Sog")
        speed   = _sog if _sog is not None else inner.get("Speed")
        _cog    = inner.get("Cog")
        course  = _cog if _cog is not None else inner.get("Course")
        _hdg    = inner.get("TrueHeading")
        heading = _hdg if _hdg is not None else inner.get("Heading")
        _raw_type = inner.get("Type") or inner.get("ShipType")
        ship_type = int(_raw_type) if _raw_type is not None else 0
        vtype = VTYPE_MAP.get(ship_type, f"Unknown (code {ship_type})")
        if ship_type != 0 and ship_type not in KNOWN_TYPES:
            logger.debug(f"⏭ Skipped code {ship_type} vessel MMSI {mmsi}")
            return None
        speed = float(speed) if speed is not None else 0.0
        return (
            int(mmsi),
            float(lat),
            float(lon),
            speed,
            float(course) if course is not None else None,
            int(heading) if heading is not None else None,
            ts,
            vtype,
            ship_type
        )
    except Exception as e:
        logger.debug(f"parse_ais error: {e}")
        return None

# ====================== ais_stream (FIXED - full bounding boxes) ======================
async def ais_stream():
    global _nominatim_lock
    _nominatim_lock = asyncio.Lock()
    init_db()
    _load_type_cache_from_db()
    uri = "wss://stream.aisstream.io/v0/stream"
    subscription = {
        "Apikey": API_KEY,
        "FilterMessageTypes": ["PositionReport", "ExtendedClassBPositionReport",
                              "StandardClassBPositionReport", "ShipStaticData",
                              "LongRangeAisBroadcastMessage"],
        "BoundingBoxes": [
            [[25.5,  56.0], [27.5,  58.5]],
            [[10.0,  43.0], [15.0,  51.0]],
            [[ 3.0,   3.0], [ 7.0,  10.0]],
            [[49.0,  -2.0], [52.0,   2.5]],
            [[ 1.0, 103.5], [ 1.5, 104.5]],
            [[12.0,  32.0], [30.0,  44.0]],
            [[22.0,  50.0], [26.0,  57.0]],
            [[ 1.0, 103.0], [10.0, 115.0]],
            [[30.0,  -5.0], [46.0,  37.0]],
            [[ 7.0, -82.0], [10.0, -77.0]],
            [[ 5.0, -80.0], [15.0, -70.0]],
            [[35.0, 129.0], [40.0, 132.0]],
            [[30.0, 120.0], [40.0, 130.0]],
            [[20.0, 120.0], [30.0, 130.0]],
            [[30.0, 125.0], [40.0, 135.0]],
            [[50.0,   4.0], [60.0,  10.0]],
            [[60.0,   5.0], [70.0,  15.0]],
            [[55.5, -10.5], [60.5,   2.5]]
        ]
    }
    retry_delay = 5
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=30) as ws:
                await ws.send(json.dumps(subscription))
                logger.info("✅ Connected to AIS stream")
                retry_delay = 5
                async for msg in ws:
                    pos = parse_ais(msg)
                    if pos:
                        *pos_args, ship_type_val = pos
                        if ship_type_val == 0:
                            mmsi_from_pos = pos_args[0]
                            cached_type = _mmsi_type_cache.get(int(mmsi_from_pos), 0)
                            if cached_type == 0:
                                continue
                            if cached_type not in KNOWN_TYPES:
                                continue
                            ship_type_val = cached_type
                            pos_args[-1] = VTYPE_MAP.get(cached_type, f"Unknown (code {cached_type})")
                        await save_position(*pos_args, ship_type_val)
        except asyncio.CancelledError:
            logger.info("Shutting down cleanly.")
            break
        except websockets.exceptions.ConnectionClosedError as e:
            logger.error(f"❌ Connection closed: {e} — retrying in {retry_delay}s")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 120)
        except Exception as e:
            logger.error(f"❌ {e} — retrying in {retry_delay}s")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 120)

# ====================== STEP 4 DETECTION FUNCTIONS ======================
def _is_open_ocean(location: str) -> bool:
    if not location or not isinstance(location, str):
        return True
    return location[0].isdigit() and ',' in location and location.count('.') >= 2

def _detect_stopped_in_ocean(track: list[dict]) -> list[dict]:
    anomalies = []
    STOP_KN = 0.5
    MIN_HOURS = 4.0
    i = 0
    n = len(track)
    while i < n:
        if track[i]['speed'] >= STOP_KN or not _is_open_ocean(track[i]['location']):
            i += 1
            continue
        start_ts = _parse_ts(track[i]['timestamp'])
        j = i + 1
        while j < n and track[j]['speed'] < STOP_KN and _is_open_ocean(track[j]['location']):
            j += 1
        end_ts = _parse_ts(track[j-1]['timestamp'])
        hours = (end_ts - start_ts).total_seconds() / 3600.0
        vtype = track[i]['vessel_type']
        if hours >= MIN_HOURS and any(k in vtype for k in ["Cargo", "Tanker", "Passenger"]):
            anomalies.append({
                'anomaly_type': 'STOPPED_IN_OPEN_OCEAN',
                'severity': min(1.0, hours / 8.0),
                'detail': f"{vtype} stopped {hours:.1f}h in open ocean – suspicious",
                'lat': track[i]['lat'],
                'lon': track[i]['lon'],
                'timestamp': track[i]['timestamp'],
            })
        i = j
    return anomalies

def speed_check(track: list[dict]) -> list[dict]:
    anomalies = []
    for i in range(1, len(track)):
        prev, curr = track[i-1], track[i]
        try:
            t1 = _parse_ts(prev['timestamp'])
            t2 = _parse_ts(curr['timestamp'])
            hours = (t2 - t1).total_seconds() / 3600
            if hours <= 0 or hours > MAX_GAP_HOURS: continue
            dist = haversine(prev['lat'], prev['lon'], curr['lat'], curr['lon'])
            implied = dist / hours
            max_spd = get_max_speed(curr['vessel_type'])
            if implied > max_spd * 3:
                anomalies.append({
                    'anomaly_type': 'IMPOSSIBLE_SPEED',
                    'severity': min(1.0, (implied / (max_spd * 3)) * 0.5 + 0.5),
                    'detail': f"Implied {implied:.1f} kn (3× max for {curr['vessel_type']})",
                    'lat': curr['lat'], 'lon': curr['lon'], 'timestamp': curr['timestamp']
                })
        except Exception:
            continue
    return anomalies

def jump_check(track: list[dict]) -> list[dict]:
    anomalies = []
    for i in range(1, len(track)):
        prev, curr = track[i-1], track[i]
        if check_teleport(prev['lat'], prev['lon'], prev['timestamp'], curr['lat'], curr['lon'], curr['timestamp']):
            anomalies.append({
                'anomaly_type': 'TELEPORT_JUMP',
                'severity': 0.9,
                'detail': "Large jump in short time (teleport)",
                'lat': curr['lat'], 'lon': curr['lon'], 'timestamp': curr['timestamp']
            })
    return anomalies

def identity_clone_check(track: list[dict]) -> list[dict]:
    anomalies = []
    for i in range(len(track)):
        for j in range(i+1, len(track)):
            dt = (_parse_ts(track[j]['timestamp']) - _parse_ts(track[i]['timestamp'])).total_seconds() / 60
            if dt > 1.0: break
            dist = haversine(track[i]['lat'], track[i]['lon'], track[j]['lat'], track[j]['lon'])
            if dist > 0.5:
                anomalies.append({
                    'anomaly_type': 'MMSI_CLONE',
                    'severity': 0.95,
                    'detail': f"Same MMSI {dist:.1f} nm apart within {dt:.1f} min – cloned identity",
                    'lat': track[j]['lat'], 'lon': track[j]['lon'], 'timestamp': track[j]['timestamp']
                })
                break
    return anomalies

def course_mismatch_check(track: list[dict]) -> list[dict]:
    anomalies = []
    for i in range(1, len(track)):
        prev, curr = track[i-1], track[i]
        if curr.get('course') is None: continue
        dist = haversine(prev['lat'], prev['lon'], curr['lat'], curr['lon'])
        if dist < 0.1: continue
        actual = compute_bearing(prev['lat'], prev['lon'], curr['lat'], curr['lon'])
        diff = abs(actual - curr['course'])
        if diff > 180: diff = 360 - diff
        if diff > 45:
            anomalies.append({
                'anomaly_type': 'COURSE_MISMATCH',
                'severity': 0.75,
                'detail': f"Reported course {curr['course']:.1f}° but actual bearing {actual:.1f}°",
                'lat': curr['lat'], 'lon': curr['lon'], 'timestamp': curr['timestamp']
            })
    return anomalies

def zone_check(track: list[dict]) -> list[dict]:
    return _detect_stopped_in_ocean(track)

def dark_period_check(track: list[dict]) -> list[dict]:
    anomalies = []
    MAX_EXPECTED_GAP = 4.0
    for i in range(1, len(track)):
        gap = (_parse_ts(track[i]['timestamp']) - _parse_ts(track[i-1]['timestamp'])).total_seconds() / 3600
        if gap > MAX_EXPECTED_GAP:
            anomalies.append({
                'anomaly_type': 'DARK_PERIOD',
                'severity': min(1.0, gap / 12.0),
                'detail': f"Vessel silent for {gap:.1f} h (normal max ≈ {MAX_EXPECTED_GAP}h)",
                'lat': track[i]['lat'], 'lon': track[i]['lon'], 'timestamp': track[i]['timestamp']
            })
    return anomalies

def detect_anomalies(track: list[dict]) -> list[dict]:
    if len(track) < 2:
        return []
    anoms = []
    anoms.extend(speed_check(track))
    anoms.extend(jump_check(track))
    anoms.extend(identity_clone_check(track))
    anoms.extend(course_mismatch_check(track))
    anoms.extend(zone_check(track))
    anoms.extend(dark_period_check(track))
    anoms.sort(key=lambda x: x['timestamp'])
    return anoms

def get_vessel_track(mmsi: int, conn=None, limit: int = None) -> list[dict]:
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(DB_FILE)
    try:
        query = '''SELECT mmsi, location, lat, lon, speed, course, heading, timestamp, vessel_type
                   FROM positions WHERE mmsi = ? ORDER BY timestamp ASC'''
        params = [mmsi]
        if limit is not None:
            query += ' LIMIT ?'
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
    finally:
        if owns_conn:
            conn.close()
    columns = ("mmsi", "location", "lat", "lon", "speed", "course", "heading", "timestamp", "vessel_type")
    return [dict(zip(columns, row)) for row in rows]

def show_recent(n=10):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        'SELECT mmsi, location, speed, course, heading, timestamp, vessel_type '
        'FROM positions ORDER BY timestamp DESC LIMIT ?', (n,)
    ).fetchall()
    conn.close()
    if not rows:
        print("No data yet.")
        return
    print(f"\n{'MMSI':<12} {'Location':<45} {'Speed':>6} {'Course':>7} {'Heading':>8} {'Type':<10} Timestamp")
    print("-" * 110)
    for mmsi, location, speed, course, heading, ts, vtype in rows:
        print(
            f"{mmsi:<12} {(location or 'Unknown'):<45} "
            f"{speed or 0:>5.1f}kn {course or 0:>6.1f}° {heading or 0:>7}° "
            f"{vtype:<10} {ts}"
        )
    print()

# ====================== ENTRY POINT ======================
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "query":
        show_recent(10)
    else:
        if not API_KEY:
            print("🚨 Add your API key first")
        else:
            print("Starting AIS listener... (Step 4 detection checks are now ACTIVE)")
            try:
                asyncio.run(ais_stream())
            except KeyboardInterrupt:
                print("\n👋 Stopped by user.")