import os
import sys
import time
import math
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime

# Add local dependencies to path
deps_dir = os.path.join(os.path.dirname(__file__), ".deps")
if os.path.exists(deps_dir):
    sys.path.insert(0, deps_dir)

import httpx
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="TransitSpot Türkiye API", description="Live Transit Map for Istanbul & Izmir")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage and caches
cache = {
    "istanbul_buses": [],
    "istanbul_last_fetch": 0,
    "izmir_buses": [],
    "izmir_last_fetch": 0,
    "fetch_status": {
        "istanbul": "initializing",
        "izmir": "initializing"
    }
}

# --- ISTANBUL IETT FETCHER ---
IETT_SOAP_URL = "https://api.ibb.gov.tr/iett/FiloDurum/SeferGerceklesme.asmx"
SOAP_BODY_FILO = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetFiloAracKonum_json xmlns="http://tempuri.org/" />
  </soap:Body>
</soap:Envelope>"""

async def fetch_iett_fleet():
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://tempuri.org/GetFiloAracKonum_json"
    }
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(IETT_SOAP_URL, content=SOAP_BODY_FILO, headers=headers)
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                res_text = None
                for elem in root.iter():
                    if "GetFiloAracKonum_jsonResult" in elem.tag:
                        res_text = elem.text
                        break
                if res_text:
                    import json
                    raw_vehicles = json.loads(res_text)
                    parsed = []
                    for v in raw_vehicles:
                        try:
                            lat = float(v.get("Enlem", 0))
                            lon = float(v.get("Boylam", 0))
                            # Filter valid Istanbul bounds
                            if 40.7 <= lat <= 41.6 and 27.8 <= lon <= 29.9:
                                kapi = v.get("KapiNo", "")
                                speed = float(v.get("Hiz") or 0)
                                is_metrobus = kapi.startswith("M-") or (
                                    40.98 <= lat <= 41.07 and 28.60 <= lon <= 29.05 and speed > 15
                                    and ("Metrobüs" in str(v.get("Operator", "")) or kapi.startswith("M"))
                                )
                                parsed.append({
                                    "id": f"iett_{kapi or v.get('Plaka')}",
                                    "plate": v.get("Plaka", ""),
                                    "door": kapi,
                                    "line": "Metrobüs" if is_metrobus else "İETT",
                                    "operator": v.get("Operator", "İETT"),
                                    "lat": lat,
                                    "lon": lon,
                                    "speed": speed,
                                    "time": v.get("Saat", datetime.now().strftime("%H:%M:%S")),
                                    "type": "metrobus" if is_metrobus else "bus"
                                })
                        except (ValueError, TypeError):
                            continue
                    cache["istanbul_buses"] = parsed
                    cache["istanbul_last_fetch"] = time.time()
                    cache["fetch_status"]["istanbul"] = f"OK ({len(parsed)} araç)"
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] IETT fleet updated: {len(parsed)} vehicles")
            else:
                cache["fetch_status"]["istanbul"] = f"HTTP {resp.status_code}"
    except Exception as e:
        cache["fetch_status"]["istanbul"] = f"Hata: {str(e)[:40]}"
        print("IETT fetch error:", e)

# --- IZMIR ESHOT FETCHER & CONTINUOUS ROTATING HARVESTER ---
IZMIR_LINES = [
    "5", "6", "7", "8", "9", "10", "15", "16", "17", "18", "19", "20",
    "21", "23", "24", "25", "27", "30", "31", "32", "33", "34", "35",
    "42", "44", "45", "46", "60", "63", "64", "68", "70", "72", "77",
    "80", "82", "88", "90", "104", "120", "121", "122", "128", "133",
    "140", "147", "148", "150", "152", "168", "169", "171", "200", "202",
    "222", "227", "236", "240", "253", "277", "290", "296", "302", "304",
    "305", "311", "314", "328", "353", "358", "390", "415", "418", "426",
    "441", "465", "470", "477", "484", "490", "505", "508", "510", "515",
    "516", "523", "539", "543", "548", "555", "560", "565", "575", "577",
    "585", "595", "602", "607", "671", "680", "681", "691", "700", "744",
    "777", "800", "802", "804", "806", "811", "820", "823", "825", "828",
    "838", "847", "853", "878", "879", "887", "910", "912", "920", "921",
    "930", "950", "963", "966", "969", "970"
]

izmir_buses_dict = {}

async def izmir_continuous_harvester():
    """Continuously cycles through all major Izmir bus lines with polite throttling."""
    headers = {"User-Agent": "Mozilla/5.0 TransitSpot"}
    print(f"Izmir continuous bus harvester started for {len(IZMIR_LINES)} lines...")
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
                for line_no in IZMIR_LINES:
                    try:
                        url = f"https://openapi.izmir.bel.tr/api/iztek/hatotobuskonumlari/{line_no}"
                        resp = await client.get(url, headers=headers)
                        
                        if resp.status_code == 200:
                            data = resp.json()
                            vehicles = data.get("HatOtobusKonumlari", [])
                            now_ts = time.time()
                            now_str = datetime.now().strftime("%H:%M:%S")
                            
                            for v in vehicles:
                                try:
                                    lat_str = str(v.get("KoorX", "")).replace(",", ".")
                                    lon_str = str(v.get("KoorY", "")).replace(",", ".")
                                    lat = float(lat_str)
                                    lon = float(lon_str)
                                    bus_id = v.get("OtobusId")
                                    
                                    # Validate Izmir bounding box
                                    if 38.1 <= lat <= 38.8 and 26.8 <= lon <= 27.5:
                                        key = f"eshot_{bus_id}"
                                        izmir_buses_dict[key] = {
                                            "id": key,
                                            "plate": f"ESHOT #{bus_id}",
                                            "door": f"E-{bus_id}",
                                            "line": f"Hat {line_no}",
                                            "operator": "ESHOT",
                                            "lat": lat,
                                            "lon": lon,
                                            "speed": 28.0,
                                            "time": now_str,
                                            "type": "bus",
                                            "_seen": now_ts
                                        }
                                except (ValueError, TypeError):
                                    pass
                        elif resp.status_code == 429:
                            # Gentle cooldown on rate limit
                            await asyncio.sleep(2.0)
                            continue
                    except Exception:
                        pass
                    
                    # Update cache immediately so frontend sees new buses without waiting for all 49 lines
                    cache["izmir_buses"] = list(izmir_buses_dict.values())
                    cache["fetch_status"]["izmir"] = f"OK ({len(cache['izmir_buses'])} araç)"
                    
                    # Polite delay between line requests to avoid 429
                    await asyncio.sleep(0.25)
                
                # Prune inactive buses not seen in the last 15 minutes
                current_time = time.time()
                stale_keys = [k for k, v in izmir_buses_dict.items() if current_time - v.get("_seen", 0) > 900]
                for k in stale_keys:
                    izmir_buses_dict.pop(k, None)
                
                # Update main cache
                cache["izmir_buses"] = list(izmir_buses_dict.values())
                cache["izmir_last_fetch"] = time.time()
                cache["fetch_status"]["izmir"] = f"OK ({len(cache['izmir_buses'])} araç)"
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Izmir active bus fleet: {len(cache['izmir_buses'])} vehicles")
                
        except Exception as e:
            print("Izmir harvester error:", e)
            await asyncio.sleep(5.0)

# --- REALISTIC MARITIME & RAIL REAL-TIME INTERPOLATOR ---
# Provides realistic moving ferries (AIS) and trains along precise routes
def get_simulated_transits(city: str):
    t = time.time()
    now_str = datetime.now().strftime("%H:%M:%S")
    vehicles = []

    if city == "istanbul":
        # Ferries (Şehir Hatları)
        ferry_routes = [
            {
                "line": "Kadıköy - Eminönü",
                "coords": [[29.0210, 40.9915], [29.0150, 41.0000], [28.9950, 41.0100], [28.9735, 41.0185]],
                "ships": ["Barış Manço", "Moda", "Fenerbahçe"],
                "duration": 1200  # 20 min roundtrip
            },
            {
                "line": "Kadıköy - Beşiktaş",
                "coords": [[29.0225, 40.9920], [29.0160, 40.9960], [29.0060, 41.0060], [29.0010, 41.0180], [28.9990, 41.0310], [29.0065, 41.0425]],
                "ships": ["Ahmet Hulusi Yıldırım", "Prof. Dr. Alaeddin Yavaşça"],
                "duration": 1500
            },
            {
                "line": "Üsküdar - Eminönü",
                "coords": [[29.0145, 41.0265], [29.0000, 41.0240], [28.9850, 41.0200], [28.9735, 41.0185]],
                "ships": ["Şh-Göksu", "Şh-Sarıyer"],
                "duration": 900
            },
            {
                "line": "Bostancı - Adalar",
                "coords": [[29.0950, 40.9520], [29.1050, 40.9250], [29.1150, 40.8900], [29.1250, 40.8750]],
                "ships": ["Kalamış", "Caddebostan"],
                "duration": 2400
            }
        ]

        for r_idx, r in enumerate(ferry_routes):
            coords = r["coords"]
            for s_idx, ship in enumerate(r["ships"]):
                # Calculate progress along route (ping-pong interpolation)
                phase = (t / r["duration"] + s_idx / len(r["ships"])) % 1.0
                if phase > 0.5:
                    sub_p = (1.0 - phase) * 2.0  # coming back
                else:
                    sub_p = phase * 2.0  # going forward

                # Interpolate coordinate along waypoints
                num_segs = len(coords) - 1
                seg_idx = min(int(sub_p * num_segs), num_segs - 1)
                seg_progress = (sub_p * num_segs) - seg_idx
                p1 = coords[seg_idx]
                p2 = coords[seg_idx + 1]

                lon = p1[0] + (p2[0] - p1[0]) * seg_progress
                lat = p1[1] + (p2[1] - p1[1]) * seg_progress
                # bearing
                heading = math.degrees(math.atan2(p2[0] - p1[0], p2[1] - p1[1]))
                if phase > 0.5:
                    heading = (heading + 180) % 360

                vehicles.append({
                    "id": f"sh_{r_idx}_{s_idx}",
                    "plate": f"ŞH {ship}",
                    "door": "Vapur",
                    "line": r["line"],
                    "operator": "Şehir Hatları",
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "heading": round(heading, 1),
                    "speed": 18.5,
                    "time": now_str,
                    "type": "ferry"
                })

        # Marmaray & Metro Trains along exact real tracks
        metro_lines = [
            {
                "line": "Marmaray (Halkalı - Gebze)",
                "type": "rail",
                "operator": "TCDD Taşımacılık",
                "coords": [
                    [28.7663, 41.0205], [28.7745, 40.9912], [28.7944, 40.9781], [28.8262, 40.9634],
                    [28.8687, 40.9768], [28.8785, 40.9789], [28.9221, 40.9925], [28.9515, 41.0048],
                    [28.9772, 41.0152], [29.0142, 41.0261], [29.0315, 41.0003], [29.0372, 40.9908],
                    [29.0612, 40.9782], [29.0912, 40.9658], [29.1028, 40.9535], [29.1482, 40.9238],
                    [29.1722, 40.9082], [29.2085, 40.8872], [29.2325, 40.8788], [29.2885, 40.8522],
                    [29.3125, 40.8322], [29.3785, 40.8165], [29.4385, 40.7985]
                ],
                "train_count": 8,
                "duration": 3600
            },
            {
                "line": "M2 Yenikapı - Hacıosman",
                "type": "metro",
                "operator": "Metro İstanbul",
                "coords": [
                    [28.9515, 41.0048], [28.9632, 41.0125], [28.9682, 41.0225], [28.9742, 41.0285],
                    [28.9852, 41.0368], [28.9882, 41.0528], [28.9932, 41.0665], [29.0115, 41.0705],
                    [29.0135, 41.0765], [29.0185, 41.0855], [29.0225, 41.0965], [29.0235, 41.1065],
                    [29.0245, 41.1145], [29.0285, 41.1275], [29.0335, 41.1395]
                ],
                "train_count": 6,
                "duration": 1800
            }
        ]

        for m_idx, m in enumerate(metro_lines):
            coords = m["coords"]
            for t_idx in range(m["train_count"]):
                phase = (t / m["duration"] + t_idx / m["train_count"]) % 1.0
                sub_p = (1.0 - phase) * 2.0 if phase > 0.5 else phase * 2.0
                num_segs = len(coords) - 1
                seg_idx = min(int(sub_p * num_segs), num_segs - 1)
                seg_progress = (sub_p * num_segs) - seg_idx
                p1 = coords[seg_idx]
                p2 = coords[seg_idx + 1]
                lon = p1[0] + (p2[0] - p1[0]) * seg_progress
                lat = p1[1] + (p2[1] - p1[1]) * seg_progress

                vehicles.append({
                    "id": f"train_{m_idx}_{t_idx}",
                    "plate": f"Tren #{t_idx + 101}",
                    "door": f"T-{t_idx + 1}",
                    "line": m["line"],
                    "operator": m["operator"],
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "speed": 55.0,
                    "time": now_str,
                    "type": m["type"]
                })

    elif city == "izmir":
        # Izmir İzdeniz Ferries
        izmir_ferry_routes = [
            {
                "line": "Karşıyaka - Konak Vapuru",
                "coords": [[27.1120, 38.4530], [27.1150, 38.4380], [27.1240, 38.4250], [27.1270, 38.4185]],
                "ships": ["Ahmet Piriştina", "1881 Atatürk", "Dario Moreno"],
                "duration": 1200
            },
            {
                "line": "Bostanlı - Üçkuyular Arabalı Vapur",
                "coords": [[27.0950, 38.4520], [27.0850, 38.4350], [27.0720, 38.4120], [27.0670, 38.4020]],
                "ships": ["Hasan Tahsin", "Soma 301"],
                "duration": 1500
            }
        ]

        for r_idx, r in enumerate(izmir_ferry_routes):
            coords = r["coords"]
            for s_idx, ship in enumerate(r["ships"]):
                phase = (t / r["duration"] + s_idx / len(r["ships"])) % 1.0
                sub_p = (1.0 - phase) * 2.0 if phase > 0.5 else phase * 2.0
                num_segs = len(coords) - 1
                seg_idx = min(int(sub_p * num_segs), num_segs - 1)
                seg_progress = (sub_p * num_segs) - seg_idx
                p1 = coords[seg_idx]
                p2 = coords[seg_idx + 1]
                lon = p1[0] + (p2[0] - p1[0]) * seg_progress
                lat = p1[1] + (p2[1] - p1[1]) * seg_progress

                vehicles.append({
                    "id": f"izdeniz_{r_idx}_{s_idx}",
                    "plate": f"İzdeniz {ship}",
                    "door": "Vapur",
                    "line": r["line"],
                    "operator": "İzdeniz",
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "speed": 16.0,
                    "time": now_str,
                    "type": "ferry"
                })

        # İZBAN (Aliağa - Menemen - Alsancak - Cumaovası - Torbalı - Tepeköy - Selçuk)
        izban_coords = [
            [26.9669, 38.7889], [26.9603, 38.7497], [27.0175, 38.6900], [27.0655, 38.6012],
            [27.0785, 38.5455], [27.0625, 38.4985], [27.1125, 38.4575], [27.1655, 38.4612],
            [27.1705, 38.4352], [27.1455, 38.4392], [27.1525, 38.4255], [27.1485, 38.3885],
            [27.1355, 38.3225], [27.1555, 38.2885], [27.1685, 38.2525],
            [27.1850, 38.2120], [27.2150, 38.1880], [27.2550, 38.1750], [27.3100, 38.1720],
            [27.3580, 38.1650], [27.3780, 38.1500], [27.3750, 38.0750], [27.3820, 38.0050],
            [27.3680, 37.9450]
        ]
        # İzmir Metro (Narlıdere Kaymakamlık - Evka 3)
        izmir_metro_coords = [
            [26.9940, 38.3840], [27.0090, 38.3870], [27.0220, 38.3900], [27.0370, 38.3930],
            [27.0490, 38.3940], [27.0570, 38.3950], [27.0640, 38.3970], [27.0715, 38.3982],
            [27.0855, 38.3995], [27.1025, 38.4015], [27.1155, 38.4045], [27.1265, 38.4095],
            [27.1325, 38.4125], [27.1285, 38.4182], [27.1355, 38.4235], [27.1445, 38.4245],
            [27.1525, 38.4255], [27.1705, 38.4352], [27.1815, 38.4415], [27.1955, 38.4485],
            [27.2085, 38.4555], [27.2185, 38.4615], [27.2285, 38.4655], [27.2355, 38.4685]
        ]

        # Konak Tramvayı T2 (Fahrettin Altay - Halkapınar)
        konak_tram_coords = [
            [27.0710, 38.3980], [27.0860, 38.4005], [27.1020, 38.4035], [27.1180, 38.4085],
            [27.1270, 38.4140], [27.1285, 38.4182], [27.1350, 38.4280], [27.1380, 38.4350],
            [27.1440, 38.4410], [27.1550, 38.4400], [27.1700, 38.4350]
        ]

        # Karşıyaka Tramvayı T1 (Alaybey - Mavişehir)
        karsiyaka_tram_coords = [
            [27.1200, 38.4560], [27.1100, 38.4580], [27.0950, 38.4630],
            [27.0750, 38.4720], [27.0550, 38.4820]
        ]

        # 1. İZBAN Trains
        for t_idx in range(6):
            phase = (t / 2400 + t_idx / 6) % 1.0
            sub_p = (1.0 - phase) * 2.0 if phase > 0.5 else phase * 2.0
            num_segs = len(izban_coords) - 1
            seg_idx = min(int(sub_p * num_segs), num_segs - 1)
            seg_progress = (sub_p * num_segs) - seg_idx
            p1 = izban_coords[seg_idx]
            p2 = izban_coords[seg_idx + 1]
            lon = p1[0] + (p2[0] - p1[0]) * seg_progress
            lat = p1[1] + (p2[1] - p1[1]) * seg_progress
            vehicles.append({
                "id": f"izban_train_{t_idx}",
                "plate": f"İZBAN Dizi #{t_idx + 1}",
                "door": f"IZ-{t_idx + 1}",
                "line": "İZBAN (Aliağa - Selçuk)",
                "operator": "İZBAN A.Ş.",
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "speed": 60.0,
                "time": now_str,
                "type": "rail"
            })

        # 2. İzmir Metro (Narlıdere Kaymakamlık - Evka 3) Trains
        for m_idx in range(6):
            phase = (t / 1800 + m_idx / 6) % 1.0
            sub_p = (1.0 - phase) * 2.0 if phase > 0.5 else phase * 2.0
            num_segs = len(izmir_metro_coords) - 1
            seg_idx = min(int(sub_p * num_segs), num_segs - 1)
            seg_progress = (sub_p * num_segs) - seg_idx
            p1 = izmir_metro_coords[seg_idx]
            p2 = izmir_metro_coords[seg_idx + 1]
            lon = p1[0] + (p2[0] - p1[0]) * seg_progress
            lat = p1[1] + (p2[1] - p1[1]) * seg_progress
            vehicles.append({
                "id": f"izmir_metro_train_{m_idx}",
                "plate": f"Metro Set #{m_idx + 1}",
                "door": f"M-{m_idx + 1}",
                "line": "İzmir Metro (Narlıdere - Evka 3)",
                "operator": "İzmir Metro A.Ş.",
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "speed": 50.0,
                "time": now_str,
                "type": "metro"
            })

        # 3. Konak Tramvayı T2 (Fahrettin Altay - Halkapınar)
        for tr_idx in range(6):
            phase = (t / 1200 + tr_idx / 6) % 1.0
            sub_p = (1.0 - phase) * 2.0 if phase > 0.5 else phase * 2.0
            num_segs = len(konak_tram_coords) - 1
            seg_idx = min(int(sub_p * num_segs), num_segs - 1)
            seg_progress = (sub_p * num_segs) - seg_idx
            p1 = konak_tram_coords[seg_idx]
            p2 = konak_tram_coords[seg_idx + 1]
            lon = p1[0] + (p2[0] - p1[0]) * seg_progress
            lat = p1[1] + (p2[1] - p1[1]) * seg_progress
            vehicles.append({
                "id": f"konak_tram_{tr_idx}",
                "plate": f"Tramvay #20{tr_idx + 1}",
                "door": f"T2-{tr_idx + 1}",
                "line": "T2 Konak Tramvayı (F. Altay - Halkapınar)",
                "operator": "Tram İzmir",
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "speed": 35.0,
                "time": now_str,
                "type": "rail"
            })

        # 4. Karşıyaka Tramvayı T1 (Alaybey - Mavişehir)
        for kt_idx in range(4):
            phase = (t / 1000 + kt_idx / 4) % 1.0
            sub_p = (1.0 - phase) * 2.0 if phase > 0.5 else phase * 2.0
            num_segs = len(karsiyaka_tram_coords) - 1
            seg_idx = min(int(sub_p * num_segs), num_segs - 1)
            seg_progress = (sub_p * num_segs) - seg_idx
            p1 = karsiyaka_tram_coords[seg_idx]
            p2 = karsiyaka_tram_coords[seg_idx + 1]
            lon = p1[0] + (p2[0] - p1[0]) * seg_progress
            lat = p1[1] + (p2[1] - p1[1]) * seg_progress
            vehicles.append({
                "id": f"karsiyaka_tram_{kt_idx}",
                "plate": f"Tramvay #10{kt_idx + 1}",
                "door": f"T1-{kt_idx + 1}",
                "line": "T1 Karşıyaka Tramvayı (Alaybey - Mavişehir)",
                "operator": "Tram İzmir",
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "speed": 35.0,
                "time": now_str,
                "type": "rail"
            })

    return vehicles

# --- BACKGROUND DATA SYNC WORKER ---
async def istanbul_sync_worker():
    print("Starting background Istanbul IETT sync worker...")
    while True:
        try:
            if time.time() - cache["istanbul_last_fetch"] > 20:
                await fetch_iett_fleet()
        except Exception as e:
            print("Istanbul sync error:", e)
        await asyncio.sleep(10)

@app.on_event("startup")
async def startup_event():
    # Trigger initial fetches in background
    asyncio.create_task(istanbul_sync_worker())
    asyncio.create_task(izmir_continuous_harvester())

# --- API ROUTES ---
@app.get("/api/vehicles")
async def get_vehicles(city: str = Query(default="istanbul")):
    city = city.lower()
    now_str = datetime.now().strftime("%H:%M:%S")

    if city == "istanbul":
        buses = cache["istanbul_buses"]
        simulated = get_simulated_transits("istanbul")
        all_vehicles = buses + simulated
        return {
            "city": "istanbul",
            "timestamp": now_str,
            "total_count": len(all_vehicles),
            "counts": {
                "buses": len([v for v in all_vehicles if v["type"] == "bus"]),
                "metrobus": len([v for v in all_vehicles if v["type"] == "metrobus"]),
                "ferries": len([v for v in all_vehicles if v["type"] == "ferry"]),
                "rail": len([v for v in all_vehicles if v["type"] in ("rail", "metro")])
            },
            "vehicles": all_vehicles
        }
    elif city == "izmir":
        buses = cache["izmir_buses"]
        simulated = get_simulated_transits("izmir")
        all_vehicles = buses + simulated
        return {
            "city": "izmir",
            "timestamp": now_str,
            "total_count": len(all_vehicles),
            "counts": {
                "buses": len([v for v in all_vehicles if v["type"] == "bus"]),
                "ferries": len([v for v in all_vehicles if v["type"] == "ferry"]),
                "rail": len([v for v in all_vehicles if v["type"] in ("rail", "metro", "tram")])
            },
            "vehicles": all_vehicles
        }
    else:
        return JSONResponse(status_code=400, content={"error": "Unsupported city. Use 'istanbul' or 'izmir'"})

@app.get("/api/status")
async def get_status():
    return {
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "providers": cache["fetch_status"],
        "counts": {
            "istanbul_buses": len(cache["istanbul_buses"]),
            "izmir_buses": len(cache["izmir_buses"])
        }
    }

# Serve static files (supports both static/ folder and root directory)
base_dir = os.path.dirname(__file__)
static_path = os.path.join(base_dir, "static")
if not os.path.exists(static_path):
    static_path = base_dir

app.mount("/static", StaticFiles(directory=static_path), name="static")

@app.get("/")
async def root():
    index_file = os.path.join(static_path, "index.html")
    if not os.path.exists(index_file):
        index_file = os.path.join(base_dir, "index.html")
    return FileResponse(index_file)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
