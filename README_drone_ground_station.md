# Drone Ground Station

This is a cautious Python upper-computer program for an unknown Wi-Fi drone.

## What it can do

- Probe common TCP ports on the drone IP.
- Probe common UDP command ports.
- Listen for UDP sensor/status packets.
- Parse Tello-style state packets such as `pitch:0;roll:0;bat:80;...`.
- Parse JSON or plain text packets.
- Send WiFi UFO heartbeat packets on UDP `40000`.
- Send UDP commands manually.
- Keep motion/takeoff/landing commands locked by default.
- Export received packets to CSV.

## Start

```powershell
python .\drone_ground_station.py
```

Current detected Wi-Fi settings:

- SSID: `WiFiUFO-3BE7F2`
- Computer WLAN IP: `192.168.0.2`
- Drone/gateway IP: `192.168.0.1`

Recommended first test:

1. Remove propellers.
2. Run the app.
3. Keep `Drone IP = 192.168.0.1`.
4. Click `Probe TCP`.
5. Click `Start Listener`.
6. Click `Probe UDP`.
7. If the profile is `Tello-compatible UDP`, click `SDK Init`, then `Battery?`.
8. If the SSID starts with `WiFiUFO`, select `WiFi UFO UDP`, click `Start Listener`, then click `UFO Ping`.

Do not unlock flight commands until the protocol is confirmed and the aircraft is physically safe.
