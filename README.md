# Daikin AC — ACM Edition

> **Fork of [homeassistant-daikin-optimized](https://github.com/Chris971991/homeassistant-daikin-optimized)** by Chris971991, with complete Daikin app replacement, firmware safety management, and crowdsourced KEY database.

## Why this fork?

Daikin is progressively removing local API access from their WiFi adapters through firmware updates. Their official apps (Remoapp, Onecta) push firmware that breaks the local `/aircon/*` HTTP API, replacing it with cloud-only `dsiot` protocol. **We don't know when Daikin will kill the app entirely.**

This integration ensures you **never need the Daikin app again**:

- WiFi adapter provisioning (setup new adapter without Daikin app)
- API key registration and storage
- Firmware version safety checking and update blocking
- Local-only climate control via `/aircon/*` endpoints
- Crowdsourced KEY database to eliminate sticker dependency
- DNS block list for Daikin cloud servers

## Firmware Safety

**Do NOT update your adapter firmware blindly.** Not all versions support local API.

| Adapter Model | Last Safe Firmware | Dangerous | Notes |
|---|---|---|---|
| **BRP069B41/B45** | **1.14.88** | 3.1.33+ | `/aircon/*` confirmed working |
| **BRP084Cxx** | **1.19.0** | 2.8.0+ | Switches to `/dsiot/multireq`, no rollback |
| **BRP069A4x** | 1.16.0 | Unknown | Oldest adapters, generally safe |

Sources: [HA Community](https://community.home-assistant.io/t/daikin-brp069b41-module-new-firmware-1-14-78-safe/565851), [GH #99251](https://github.com/home-assistant/core/issues/99251)

## Reverse Engineering — Technical Details

Based on decompilation of **Daikin Remoapp 4.11.1** (`ao.daikin.remoapp`).

### Discovery
UDP broadcast `DAIKIN_UDP/common/basic_info` to port **30050**. Response is `key=value,key=value` format.

### Local API Endpoints

**Device info:**
| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/common/basic_info` | GET | None | MAC, firmware, name |
| `/common/get_spw` | GET | **None** (!) | Returns setup password |
| `/common/register_terminal` | SET | KEY | Register client |
| `/common/reboot` | SET | UUID | Restart adapter |

**Climate:**
| Endpoint | Method | Description |
|---|---|---|
| `/aircon/get_control_info` | GET | Mode, temp, fan, swing |
| `/aircon/set_control_info` | SET | Change settings |
| `/aircon/get_sensor_info` | GET | Inside/outside temp |
| `/aircon/get_model_info` | GET | Capabilities |
| `/aircon/get_timer` | GET | Timer settings |
| `/aircon/get_week_power` | GET | Power consumption |

**WiFi provisioning (AP mode, IP: `192.168.127.1`):**
| Endpoint | Method | Description |
|---|---|---|
| `/common/start_wifi_scan` | SET | Trigger scan |
| `/common/get_wifi_scan_result` | GET | Available networks |
| `/config/wlan/connect/start` | POST | Send SSID + password |
| `/config/wlan/connect/permit` | POST | Confirm |
| `/config/firmware/update` | POST | Upload .bin firmware |
| `/config/system/reboot` | POST | Restart |

**Daikin response format:** `ret=OK,pow=1,mode=3,stemp=22.0,...` (NOT JSON)

### Firmware Files Bundled in APK

The app ships firmware in APK assets (no cloud download):

| File | Adapter | Version |
|---|---|---|
| `RealtekWireless/RA/3_12_3/DKWL4G_OTA_CS_V3_12_3.bin` | BRP069C/084C | 3.12.3 |
| `MarvellWireless/1_16_0/DKWL3G_OTA_CS_V1_16_0.bin` | BRP069A/B | 1.16.0 |

### Daikin Cloud Servers (block these)

From decompiled app — servers the adapter contacts:

| Server | Purpose |
|---|---|
| `daikinsmartdb.jp` | Production cloud API |
| `daikinsmartdbt.jp` | Staging |
| `sha2.daikinonlinecontroller.com` | Legacy controller |
| `secure.daikindev.com` | Dev |
| `proddit.ditdeneb.com` | DIT production |
| `dit.ditdsiotdemo.com` | DIT dsiot demo |
| `scr.dspsph.com` | Demo mode |

**DNS blocklist** (add to Pi-hole, AdGuard, or router):
```
0.0.0.0 daikinsmartdb.jp
0.0.0.0 daikinsmartdbt.jp
0.0.0.0 sha2.daikinonlinecontroller.com
0.0.0.0 secure.daikindev.com
0.0.0.0 proddit.ditdeneb.com
0.0.0.0 dit.ditdsiotdemo.com
```

This prevents firmware auto-updates and cloud phone-home.

### Authentication

**Old API (BRP069A/B):**
- `X-Daikin-uuid: <UUID>` header
- `?lpw=<password>` query param (or empty)
- `/common/get_spw` strips auth — always accessible

**New API (BRP084C, FW 2.8.0+):**
- OAuth2 to `/dsiot/multireq`
- `client_id: 568d8m7qbcecog3ujignsj43kd`
- `client_secret: 7pj8a5fuhkf4ilbqe38edb886b1c99ie5p6kiqcpscccc2p3bum`

## ACM Features

### WiFi Provisioning
Set up new adapter directly from HA. No Daikin app needed.

### Auto SPW Storage
Retrieves and stores setup password for future re-pairing.

### Firmware Safety Guard
Warns if firmware version is dangerous for local API.

### KEY Crowdsourcing (Community-driven)
When you enter your adapter KEY, the integration offers to open a pre-filled GitHub issue. You review the data (MAC + KEY) and submit it publicly. Maintainers merge it into `keys.json`. **No external servers, no hidden calls** — everything is a public PR/issue in this repo. Goal: collect enough pairs to reverse-engineer the KEY algorithm.

Current database: `custom_components/daikin/keys.json` (3 adapters and counting).

## Installation

### HACS
1. Add as custom repository in HACS
2. Install "Daikin AC (ACM Edition)"
3. Restart HA

### Manual
Copy `custom_components/daikin/` to your HA config directory.

## Credits

- **Chris971991** — base fork with BRP084 2.8.0 support
- **Apoc182** — firmware 2.8.0 research
- **ael-code** — original API documentation
- **MaxCloud ACM** — reverse engineering, provisioning, firmware management, KEY crowdsourcing
