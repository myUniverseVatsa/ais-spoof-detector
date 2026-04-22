# AIS Spoofing Detector

Real-time detection of **AIS (Automatic Identification System) spoofing** using physics-based checks.

## Why This Matters (Threat Model)
AIS spoofing is used for illegal fishing, smuggling, ghost ships, insurance fraud, and hiding in restricted waters. This tool detects suspicious behavior in real time.

## Detection Logic
All checks are based on real-world maritime physics:
- **Impossible Speed** — exceeds 3× realistic max speed for vessel class
- **Teleport/Jump** — impossible distance in short time (haversine formula)
- **MMSI Clone** — same MMSI appearing in two distant locations simultaneously
- **Course Mismatch** — reported course vs actual bearing from positions
- **Stopped in Open Ocean** — commercial vessel stationary >4 hours far from ports
- **Dark Period** — long silence gaps (common spoofing technique)

## Project Structure