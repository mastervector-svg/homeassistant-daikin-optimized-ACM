"""Button entities for Daikin ACM — firmware flash."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.button import ButtonEntity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DaikinConfigEntry, DaikinCoordinator
from .entity import DaikinEntity
from .provisioning import upload_firmware, check_firmware_safety

_LOGGER = logging.getLogger(__name__)

FIRMWARE_DIR = Path(__file__).parent / "firmware"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Daikin ACM buttons."""
    coordinator = entry.runtime_data
    host = entry.data.get(CONF_HOST, "")

    # Flash button only for Realtek adapters (adp_kind=4) that support OTA
    adp_kind = str(coordinator.device.values.get("adp_kind", ""))
    entities = []
    if adp_kind == "4":
        entities.append(DaikinFlashFirmwareButton(coordinator, host))
    async_add_entities(entities)


class DaikinFlashFirmwareButton(DaikinEntity, ButtonEntity):
    """Button to flash firmware to adapter."""

    _attr_translation_key = "flash_firmware"
    _attr_icon = "mdi:cellphone-arrow-down"

    def __init__(self, coordinator: DaikinCoordinator, host: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._host = host
        self._attr_unique_id = f"{self.device.mac}-flash_firmware"

    @property
    def name(self) -> str:
        """Return name."""
        return "Flash Firmware"

    async def async_press(self) -> None:
        """Flash bundled firmware to the adapter."""
        adp_kind = self.device.values.get("adp_kind", "")

        # Marvell adapters (adp_kind=3) need AP mode for firmware flash
        if str(adp_kind) == "3":
            _LOGGER.warning(
                "ACM: adapter %s is Marvell (adp_kind=3) — firmware must be "
                "flashed in AP mode (connect to DaikinAPxxxxx WiFi). "
                "Skipping OTA flash.",
                self._host,
            )
            return

        ver = self.device.values.get("ver", "").replace("_", ".")
        check_firmware_safety(ver)

        # Find appropriate firmware file
        fw_file = None
        for f in FIRMWARE_DIR.glob("*.bin"):
            fw_file = f
            break

        if not fw_file:
            _LOGGER.error("ACM: no firmware file found in %s", FIRMWARE_DIR)
            return

        _LOGGER.warning(
            "ACM: flashing %s to %s (current FW: %s, adp_kind=%s)",
            fw_file.name,
            self._host,
            ver,
            adp_kind,
        )

        session = async_get_clientsession(self.hass)
        fw_data = fw_file.read_bytes()
        ok = await upload_firmware(session, self._host, fw_data)

        if ok:
            _LOGGER.warning("ACM: firmware flash SUCCESS on %s", self._host)
        else:
            _LOGGER.error("ACM: firmware flash FAILED on %s", self._host)
