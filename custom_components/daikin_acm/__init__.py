"""Platform for the Daikin AC."""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

from aiohttp import ClientConnectionError
from pydaikin.daikin_base import Appliance
from pydaikin.factory import DaikinFactory

from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_UUID,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .const import DOMAIN, KEY_MAC, TIMEOUT
from .coordinator import DaikinConfigEntry, DaikinCoordinator
from .provisioning import upload_firmware, check_firmware_safety, get_basic_info

_LOGGER = logging.getLogger(__name__)


def get_daikin_ssl_context() -> ssl.SSLContext:
    """Create SSL context with legacy Daikin support."""
    ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    # Lower security level to allow legacy Daikin SSL/TLS configurations
    # Fixes HA 2025.10 SSL WRONG_SIGNATURE_TYPE error
    try:
        ssl_context.set_ciphers('DEFAULT:@SECLEVEL=0')
    except ssl.SSLError:
        pass  # Fallback for systems that don't support SECLEVEL
    return ssl_context


PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: DaikinConfigEntry) -> bool:
    """Establish connection with Daikin."""
    conf = entry.data
    # For backwards compat, set unique ID
    if entry.unique_id is None or ".local" in entry.unique_id:
        hass.config_entries.async_update_entry(entry, unique_id=conf[KEY_MAC])

    session = async_get_clientsession(hass)
    host = conf[CONF_HOST]
    # Create SSL context in executor to avoid blocking the event loop
    ssl_context = await hass.async_add_executor_job(get_daikin_ssl_context)
    try:
        async with asyncio.timeout(TIMEOUT):
            device: Appliance = await DaikinFactory(
                host,
                session,
                key=entry.data.get(CONF_API_KEY),
                uuid=entry.data.get(CONF_UUID),
                password=entry.data.get(CONF_PASSWORD),
                ssl_context=ssl_context,
            )
        _LOGGER.debug("Connection to %s successful", host)
    except TimeoutError as err:
        _LOGGER.debug("Connection to %s timed out in 60 seconds", host)
        raise ConfigEntryNotReady from err
    except ClientConnectionError as err:
        _LOGGER.debug("ClientConnectionError to %s", host)
        raise ConfigEntryNotReady from err

    coordinator = DaikinCoordinator(hass, entry, device)

    await coordinator.async_config_entry_first_refresh()

    await async_migrate_unique_id(hass, entry, device)

    entry.runtime_data = coordinator
    _LOGGER.warning("ACM: forwarding platforms %s for %s", PLATFORMS, host)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.warning("ACM: platforms setup complete for %s — device mac=%s", host, device.mac)
    return True


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Daikin ACM services."""

    async def handle_flash_firmware(call):
        """Flash firmware to a Daikin adapter.

        Service: daikin_acm.flash_firmware
        Data: entity_id or host, firmware (bundled name or path)
        """
        host = call.data.get("host")
        firmware_name = call.data.get("firmware", "DKWL3G_OTA_CS_V1_16_0.bin")

        if not host:
            _LOGGER.error("daikin_acm.flash_firmware: host is required")
            return

        # Safety check — get current firmware first
        session = async_get_clientsession(hass)
        try:
            info = await get_basic_info(session, host)
            current_ver = info.get("ver", "").replace("_", ".")
            _LOGGER.info(
                "ACM flash: %s currently on firmware %s", host, current_ver
            )
        except Exception as err:
            _LOGGER.error("ACM flash: cannot reach %s: %s", host, err)
            return

        # Load firmware binary
        fw_path = Path(__file__).parent / "firmware" / firmware_name
        if not fw_path.exists():
            _LOGGER.error("ACM flash: firmware file not found: %s", fw_path)
            return

        fw_data = fw_path.read_bytes()
        if len(fw_data) < 1000:
            _LOGGER.error("ACM flash: firmware file too small, aborting")
            return

        _LOGGER.warning(
            "ACM flash: uploading %s (%d bytes) to %s...",
            firmware_name, len(fw_data), host,
        )

        ok = await upload_firmware(session, host, fw_data)
        if ok:
            _LOGGER.warning(
                "ACM flash: firmware uploaded to %s. Adapter will reboot.",
                host,
            )
        else:
            _LOGGER.error("ACM flash: upload FAILED for %s", host)

    async def handle_scan_network(call):
        """Scan network for Daikin adapters.

        Service: daikin_acm.scan_network
        """
        from .provisioning import discover_adapters
        devices = await hass.async_add_executor_job(_discover_sync_wrapper)
        for dev in devices:
            name = dev.get("name", "?")
            ip = dev.get("ip", "?")
            ver = dev.get("ver", "?").replace("_", ".")
            mac = dev.get("mac", "?")
            safety = check_firmware_safety(ver)
            status = "SAFE" if safety["safe"] else "DANGEROUS"
            _LOGGER.info(
                "ACM scan: found %s at %s (MAC: %s, FW: %s) — %s",
                name, ip, mac, ver, status,
            )
        hass.bus.async_fire(f"{DOMAIN}_scan_result", {
            "count": len(devices),
            "devices": [
                {
                    "ip": d.get("ip"),
                    "name": d.get("name"),
                    "mac": d.get("mac"),
                    "firmware": d.get("ver", "").replace("_", "."),
                }
                for d in devices
            ],
        })

    def _discover_sync_wrapper():
        import socket, time
        from .provisioning import parse_daikin_response
        devices = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(3.0)
        try:
            sock.bind(("", 30000))
        except OSError:
            sock.bind(("", 0))
        try:
            sock.sendto(b"DAIKIN_UDP/common/basic_info", ("<broadcast>", 30050))
            seen = set()
            deadline = time.time() + 3.0
            while time.time() < deadline:
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

    hass.services.async_register(DOMAIN, "flash_firmware", handle_flash_firmware)
    hass.services.async_register(DOMAIN, "scan_network", handle_scan_network)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DaikinConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_unique_id(
    hass: HomeAssistant, config_entry: DaikinConfigEntry, device: Appliance
) -> None:
    """Migrate old entry."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    old_unique_id = config_entry.unique_id
    new_unique_id = device.mac
    new_mac = dr.format_mac(new_unique_id)
    new_name = device.values.get("name", "Daikin AC")

    @callback
    def _update_unique_id(entity_entry: er.RegistryEntry) -> dict[str, str] | None:
        """Update unique ID of entity entry."""
        return update_unique_id(entity_entry, new_unique_id)

    if new_unique_id == old_unique_id:
        return

    duplicate = dev_reg.async_get_device(
        connections={(CONNECTION_NETWORK_MAC, new_mac)}, identifiers=None
    )

    # Remove duplicated device
    if duplicate is not None:
        if config_entry.entry_id in duplicate.config_entries:
            _LOGGER.debug(
                "Removing duplicated device %s",
                duplicate.name,
            )

            # The automatic cleanup in entity registry is scheduled as a task, remove
            # the entities manually to avoid unique_id collision when the entities
            # are migrated.
            duplicate_entities = er.async_entries_for_device(
                ent_reg, duplicate.id, True
            )
            for entity in duplicate_entities:
                if entity.config_entry_id == config_entry.entry_id:
                    ent_reg.async_remove(entity.entity_id)

            dev_reg.async_update_device(
                duplicate.id, remove_config_entry_id=config_entry.entry_id
            )

    # Migrate devices
    for device_entry in dr.async_entries_for_config_entry(
        dev_reg, config_entry.entry_id
    ):
        for connection in device_entry.connections:
            if connection[1] == old_unique_id:
                new_connections = {(CONNECTION_NETWORK_MAC, new_mac)}

                _LOGGER.debug(
                    "Migrating device %s connections to %s",
                    device_entry.name,
                    new_connections,
                )
                dev_reg.async_update_device(
                    device_entry.id,
                    merge_connections=new_connections,
                )

        if device_entry.name is None:
            _LOGGER.debug(
                "Migrating device name to %s",
                new_name,
            )
            dev_reg.async_update_device(
                device_entry.id,
                name=new_name,
            )

        # Migrate entities
        await er.async_migrate_entries(hass, config_entry.entry_id, _update_unique_id)

        new_data = {**config_entry.data, KEY_MAC: dr.format_mac(new_unique_id)}

        hass.config_entries.async_update_entry(
            config_entry, unique_id=new_unique_id, data=new_data
        )


@callback
def update_unique_id(
    entity_entry: er.RegistryEntry, unique_id: str
) -> dict[str, str] | None:
    """Update unique ID of entity entry."""
    if entity_entry.unique_id.startswith(unique_id):
        # Already correct, nothing to do
        return None

    unique_id_parts = entity_entry.unique_id.split("-")
    unique_id_parts[0] = unique_id
    entity_new_unique_id = "-".join(unique_id_parts)

    _LOGGER.debug(
        "Migrating entity %s from %s to new id %s",
        entity_entry.entity_id,
        entity_entry.unique_id,
        entity_new_unique_id,
    )
    return {"new_unique_id": entity_new_unique_id}
