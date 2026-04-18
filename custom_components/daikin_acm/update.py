"""Firmware update entity for Daikin ACM.

Shows current vs target firmware version.
Update button appears only when current < target.
Progress bar during flash.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DaikinConfigEntry, DaikinCoordinator
from .entity import DaikinEntity
from .provisioning import upload_firmware

_LOGGER = logging.getLogger(__name__)

FIRMWARE_DIR = Path(__file__).parent / "firmware"

# Last safe firmware versions — confirmed with local API support
TARGET_FIRMWARE = {
    "3": "1.14.88",   # Marvell BRP069B — last confirmed safe
    "4": "1.19.0",    # Realtek BRP084C — last before dsiot
}


def _ver_tuple(ver: str) -> tuple:
    """Parse version string like '1_14_84' or '1.14.84' into comparable tuple."""
    return tuple(int(x) for x in ver.replace("_", ".").split(".") if x.isdigit())


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up firmware update entity."""
    coordinator = entry.runtime_data
    host = entry.data.get(CONF_HOST, "")
    async_add_entities([DaikinFirmwareUpdate(coordinator, host)])


class DaikinFirmwareUpdate(DaikinEntity, UpdateEntity):
    """Firmware update entity — shows version, update button with progress."""

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Only show install button for Realtek adapters that support OTA."""
        if self._adp_kind == "4":
            return UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
        # Marvell — show version info only, no install button
        return UpdateEntityFeature(0)
    _attr_icon = "mdi:chip"
    _attr_title = "Adapter Firmware"

    def __init__(self, coordinator: DaikinCoordinator, host: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._host = host
        self._adp_kind = str(self.device.values.get("adp_kind", "3"))
        self._attr_unique_id = f"{self.device.mac}-firmware_update"
        self._installing = False

    @property
    def name(self) -> str:
        return "Firmware"

    @property
    def installed_version(self) -> str | None:
        """Current firmware on the adapter."""
        return self.device.values.get("ver", "unknown").replace("_", ".")

    @property
    def latest_version(self) -> str | None:
        """Target firmware — last safe version for this adapter type."""
        return TARGET_FIRMWARE.get(self._adp_kind, self.installed_version)

    @property
    def in_progress(self) -> bool | int:
        """Return True or percentage if installing."""
        return self._installing

    @property
    def release_summary(self) -> str | None:
        """Release notes."""
        current = _ver_tuple(self.installed_version or "0")
        target = _ver_tuple(self.latest_version or "0")
        safe = _ver_tuple("1.14.88")
        if current >= safe:
            return f"Firmware {self.installed_version} — OK, safe for local API."
        if current >= target:
            return f"Firmware {self.installed_version} — OK, safe for local API."
        return f"Firmware {self.installed_version} is OUTDATED. Safe version is 1.14.88."
        if self._adp_kind == "3":
            return (
                f"Marvell adapter — firmware {self.installed_version}. "
                f"OTA not supported on this hardware. Max safe: 1.14.88."
            )
        return f"Update: {self.installed_version} → {self.latest_version}. OTA flash available."

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        """Install firmware update."""
        fw_name = FIRMWARE_FILES.get(self._adp_kind)
        if not fw_name:
            _LOGGER.error("ACM: no firmware file for adp_kind=%s", self._adp_kind)
            return

        fw_path = FIRMWARE_DIR / fw_name
        if not fw_path.exists():
            _LOGGER.error("ACM: firmware file not found: %s", fw_path)
            return

        if self._adp_kind == "3":
            _LOGGER.warning(
                "ACM: Marvell adapter at %s — OTA endpoint not available on this hardware. "
                "Use the Daikin Remoapp to update firmware (the app bundles the firmware). "
                "DO NOT update past 1.14.88 — newer versions remove local API.",
                self._host,
            )
            self._installing = False
            self.async_write_ha_state()
            return

        self._installing = True
        self.async_write_ha_state()

        _LOGGER.warning("ACM: flashing %s to %s", fw_name, self._host)
        session = async_get_clientsession(self.hass)
        fw_data = fw_path.read_bytes()
        ok = await upload_firmware(session, self._host, fw_data)

        self._installing = False
        self.async_write_ha_state()

        if ok:
            _LOGGER.warning("ACM: flash SUCCESS on %s — adapter will reboot", self._host)
        else:
            _LOGGER.error("ACM: flash FAILED on %s", self._host)
