"""Daikin WiFi adapter provisioning and key management.

Handles:
- UDP discovery of adapters on local network
- WiFi provisioning (AP mode setup)
- Terminal registration (API key)
- SPW retrieval and storage for re-pairing
- Firmware version checking
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DAIKIN_UDP_PORT = 30050
DAIKIN_UDP_MSG = b"DAIKIN_UDP/common/basic_info"
DAIKIN_AP_IPS = ["192.168.127.1", "192.168.0.100"]

FIRMWARE_SAFE = {
    "BRP069A": "1.16.0",
    "BRP069B": "1.14.88",
    "BRP084C": "1.19.0",
}
FIRMWARE_DANGEROUS = ["2.8.", "3."]


def parse_daikin_response(text: str) -> dict[str, str]:
    """Parse key=value,key=value Daikin response format."""
    result = {}
    for part in text.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


async def discover_adapters(timeout: float = 3.0) -> list[dict[str, str]]:
    """Discover Daikin adapters on the local network via UDP broadcast."""
    devices = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    sock.bind(("", 30000))

    try:
        sock.sendto(DAIKIN_UDP_MSG, ("<broadcast>", DAIKIN_UDP_PORT))
        seen = set()
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                ip = addr[0]
                if ip in seen:
                    continue
                seen.add(ip)
                info = parse_daikin_response(data.decode("utf-8", errors="ignore"))
                info["ip"] = ip
                devices.append(info)
            except socket.timeout:
                break
    finally:
        sock.close()

    return devices


async def get_basic_info(session: aiohttp.ClientSession, host: str) -> dict[str, str]:
    """GET /common/basic_info from adapter."""
    url = f"http://{host}/common/basic_info"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            return parse_daikin_response(text)


async def get_spw(session: aiohttp.ClientSession, host: str) -> str | None:
    """GET /common/get_spw — retrieves setup password WITHOUT auth.

    This endpoint intentionally strips Authorization headers (confirmed
    from Daikin Remoapp decompilation). Available on any adapter in
    station mode on the local network.
    """
    url = f"http://{host}/common/get_spw"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            data = parse_daikin_response(text)
            if data.get("ret") == "OK":
                return data.get("spw", "")
    except Exception as err:
        _LOGGER.debug("get_spw failed for %s: %s", host, err)
    return None


async def register_terminal(
    session: aiohttp.ClientSession, host: str, key: str
) -> bool:
    """Register this HA instance with the adapter using the sticker KEY.

    POST /common/register_terminal with key parameter.
    """
    url = f"http://{host}/common/register_terminal"
    params = {"key": key}
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            text = await resp.text()
            data = parse_daikin_response(text)
            return data.get("ret") == "OK"
    except Exception as err:
        _LOGGER.error("register_terminal failed for %s: %s", host, err)
        return False


async def scan_wifi(session: aiohttp.ClientSession, host: str) -> list[dict[str, str]]:
    """Scan available WiFi networks from adapter in AP mode.

    1. POST /common/start_wifi_scan
    2. Wait 3s
    3. GET /common/get_wifi_scan_result
    """
    try:
        # Start scan
        url = f"http://{host}/common/start_wifi_scan"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)):
            pass
        await asyncio.sleep(3)

        # Get results
        url = f"http://{host}/common/get_wifi_scan_result"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            data = parse_daikin_response(text)
            networks = []
            cnt = int(data.get("cnt", "0"))
            for i in range(1, cnt + 1):
                ssid = data.get(f"ssid{i}", "")
                sec = data.get(f"sec{i}", "")
                if ssid:
                    networks.append({"ssid": ssid, "security": sec})
            return networks
    except Exception as err:
        _LOGGER.error("WiFi scan failed on %s: %s", host, err)
        return []


async def connect_wifi(
    session: aiohttp.ClientSession,
    host: str,
    ssid: str,
    password: str,
    security: str = "mixed",
) -> bool:
    """Connect adapter to a WiFi network.

    1. POST /config/wlan/connect/start with SSID + password
    2. POST /config/wlan/connect/permit to confirm
    """
    try:
        url = f"http://{host}/config/wlan/connect/start"
        params = {"ssid": ssid, "security": security, "key": password}
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            text = await resp.text()
            data = parse_daikin_response(text)
            if data.get("ret") != "OK":
                return False

        url = f"http://{host}/config/wlan/connect/permit"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            data = parse_daikin_response(text)
            return data.get("ret") == "OK"
    except Exception as err:
        _LOGGER.error("WiFi connect failed on %s: %s", host, err)
        return False


async def reboot_adapter(session: aiohttp.ClientSession, host: str) -> bool:
    """Reboot the adapter after WiFi configuration."""
    try:
        url = f"http://{host}/common/reboot"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            data = parse_daikin_response(text)
            return data.get("ret") == "OK"
    except Exception:
        return True  # adapter may drop connection during reboot


def check_firmware_safety(version: str, model: str = "") -> dict[str, Any]:
    """Check if firmware version is safe for local API.

    Returns dict with:
        safe: bool — True if version is known safe
        warning: str — human-readable warning if unsafe
        max_safe: str — maximum safe version for this model
    """
    for prefix in FIRMWARE_DANGEROUS:
        if version.startswith(prefix):
            model_prefix = ""
            for mp in FIRMWARE_SAFE:
                if mp in model:
                    model_prefix = mp
                    break
            max_safe = FIRMWARE_SAFE.get(model_prefix, "1.14.88")
            return {
                "safe": False,
                "warning": (
                    f"Firmware {version} may not support local API. "
                    f"Last known safe version: {max_safe}. "
                    f"Do NOT update further."
                ),
                "max_safe": max_safe,
            }
    return {"safe": True, "warning": "", "max_safe": version}


async def upload_firmware(
    session: aiohttp.ClientSession,
    host: str,
    firmware_data: bytes,
) -> bool:
    """Upload firmware binary to adapter via POST /config/firmware/update.

    WARNING: Use only with known-safe firmware versions.
    """
    url = f"http://{host}/config/firmware/update"
    data = aiohttp.FormData()
    data.add_field("app1", firmware_data, content_type="application/octet-stream")
    try:
        async with session.post(
            url, data=data, timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
            text = await resp.text()
            result = parse_daikin_response(text)
            return result.get("ret") == "OK"
    except Exception as err:
        _LOGGER.error("Firmware upload failed for %s: %s", host, err)
        return False
