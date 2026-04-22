# 🚢 AIS Spoof Detector

Real-time detection of **AIS (Automatic Identification System) spoofing** using physics-based checks for maritime vessels.

## 🔍 What is AIS Spoofing?

AIS (Automatic Identification System) is a tracking system used by ships worldwide to broadcast their location, speed, and identity. **AIS spoofing** is when this data is faked or manipulated — used for:

- 🐟 Illegal fishing in restricted zones
- 🚢 Smuggling and sanctions evasion
- 👻 Ghost ships hiding their real location
- 💰 Insurance fraud
- 🚫 Hiding in restricted or protected waters

This tool detects such suspicious behavior in real time using physics-based anomaly checks.

---

## ⚙️ Detection Logic

All checks are based on real-world maritime physics:

| Check | Description |
|-------|-------------|
| **Impossible Speed** | Vessel exceeds 3× the realistic max speed for its class |
| **Teleport/Jump** | Impossible distance covered in a short time (haversine formula) |
| **MMSI Clone** | Same MMSI number appearing in two distant locations simultaneously |
| **Course Mismatch** | Reported course doesn't match actual bearing between positions |
| **Stopped in Open Ocean** | Commercial vessel stationary for >4 hours far from any port |
| **Dark Period** | Long silence gaps in AIS transmission — a common spoofing technique |

---

## 📁 Project Structure

```
ais-spoof-detector/          ← your project folder
├── fedata.py                ← main script that fetches AIS data & detects spoofing
├── report_anomalies.py      ← generates the final report
├── requirements.txt         ← list of libraries needed to run the project
└── README.md                ← this documentation file itself
```
---

## 🚀 Installation & Usage

### ✅ Requirements
- Python 3.x
- pip
- Git

### 📥 Step 1 — Clone the Repository

```bash
git clone https://github.com/myUniverseVatsa/ais-spoof-detector.git
cd ais-spoof-detector
```

### 📦 Step 2 — Install Dependencies

```bash
pip install -r requirements.txt
```

### ▶️ Step 3 — Run the Detector

```bash
python fedata.py
```

### 📊 Step 4 — View the Anomaly Report

```bash
python report_anomalies.py
```

---

## 🛠️ Built With

- Python 3
- Physics-based anomaly detection (Haversine formula)
- AIS data

---

## 👤 Author

**Gagan H S**
- GitHub: [@myUniverseVatsa](https://github.com/myUniverseVatsa)

---

## 📜 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.