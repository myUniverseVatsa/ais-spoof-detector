import sqlite3
import json
from datetime import datetime
import folium
from folium import PolyLine, CircleMarker

DB_FILE = r"D:\Marine\AIS_Spoof_Detector\ais.db"

ANOMALY_WEIGHTS = {
    "IMPOSSIBLE_SPEED": 30, "TELEPORT_JUMP": 25, "TELEPORT": 25,
    "MMSI_CLONE": 40, "COURSE_MISMATCH": 15, "STOPPED_IN_OPEN_OCEAN": 20,
    "DARK_PERIOD": 18, "IMPOSSIBLE_ACCEL": 22, "INVALID_MMSI": 10,
}

def get_vessel_track(mmsi: int, limit: int = 1000):
    conn = sqlite3.connect(DB_FILE)
    try:
        rows = conn.execute(
            '''SELECT mmsi, location, lat, lon, speed, course, heading, timestamp, vessel_type
               FROM positions WHERE mmsi = ? ORDER BY timestamp ASC LIMIT ?''',
            (mmsi, limit)
        ).fetchall()
    finally:
        conn.close()
    columns = ("mmsi", "location", "lat", "lon", "speed", "course", "heading", "timestamp", "vessel_type")
    return [dict(zip(columns, row)) for row in rows]

def generate_report(min_score=50):
    conn = sqlite3.connect(DB_FILE)
    anomalies = conn.execute("""
        SELECT mmsi, anomaly_type, detail, lat, lon, timestamp
        FROM anomalies ORDER BY mmsi, timestamp
    """).fetchall()
    conn.close()

    from collections import defaultdict
    vessel_data = defaultdict(lambda: {"anomalies": [], "score": 0})

    for mmsi, atype, detail, lat, lon, ts in anomalies:
        weight = ANOMALY_WEIGHTS.get(atype, 10)
        vessel_data[mmsi]["score"] += weight
        vessel_data[mmsi]["anomalies"].append({
            "type": atype, "detail": detail, "lat": lat, "lon": lon, "timestamp": ts
        })

    flagged = []
    for mmsi, data in vessel_data.items():
        if data["score"] >= min_score:
            track = get_vessel_track(mmsi)
            flagged.append({
                "mmsi": mmsi,
                "suspicion_score": data["score"],
                "confidence": round(min(100, data["score"] / 2), 1),
                "anomalies": data["anomalies"],
                "track_points": [{"lat": p["lat"], "lon": p["lon"], "ts": p["timestamp"]} for p in track]
            })

    report = {
        "generated_at": datetime.utcnow().isoformat(),
        "total_flagged_vessels": len(flagged),
        "min_score_threshold": min_score,
        "vessels": flagged
    }
    with open("suspicious_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    m = folium.Map(location=[20, 0], zoom_start=2, tiles="CartoDB positron")
    for v in flagged:
        coords = [(p["lat"], p["lon"]) for p in v["track_points"]]
        if len(coords) > 1:
            PolyLine(coords, weight=3, color="blue", popup=f"MMSI {v['mmsi']}").add_to(m)
        for a in v["anomalies"]:
            CircleMarker(location=(a["lat"], a["lon"]), radius=8, color="red", fill=True,
                         popup=f"<b>MMSI {v['mmsi']}</b><br>{a['type']}<br>{a['detail']}").add_to(m)

    m.save("suspicious_vessels.html")

    print(f"✅ Report generated! {len(flagged)} high-risk vessels.")
    print("   Open suspicious_vessels.html in your browser")

if __name__ == "__main__":
    generate_report(min_score=50)