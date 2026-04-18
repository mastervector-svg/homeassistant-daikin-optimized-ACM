"""Support for Daikin AirBase zones."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DaikinConfigEntry, DaikinCoordinator
from .entity import DaikinEntity
from .provisioning import parse_daikin_response

DAIKIN_ATTR_ADVANCED = "adv"
DAIKIN_ATTR_STREAMER = "streamer"
DAIKIN_ATTR_MODE = "mode"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Daikin climate based on config_entry."""
    daikin_api = entry.runtime_data
    switches: list[SwitchEntity] = []
    if zones := daikin_api.device.zones:
        switches.extend(
            DaikinZoneSwitch(daikin_api, zone_id)
            for zone_id, zone in enumerate(zones)
            if zone[0] != "-"
        )
    if daikin_api.device.support_advanced_modes:
        # It isn't possible to find out from the API responses if a specific
        # device supports the streamer, so assume so if it does support
        # advanced modes.
        switches.append(DaikinStreamerSwitch(daikin_api))
    switches.append(DaikinToggleSwitch(daikin_api))

    # ACM: outdoor unit quiet mode (demand control)
    if daikin_api.device.values.get("dmnd") == "1" or daikin_api.device.values.get("en_demand") is not None:
        switches.append(DaikinOutdoorQuietSwitch(daikin_api, entry.data.get("host", "")))

    async_add_entities(switches)


class DaikinZoneSwitch(DaikinEntity, SwitchEntity):
    """Representation of a zone."""

    _attr_translation_key = "zone"

    def __init__(self, coordinator: DaikinCoordinator, zone_id: int) -> None:
        """Initialize the zone."""
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._attr_unique_id = f"{self.device.mac}-zone{zone_id}"

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self.device.zones[self._zone_id][0]

    @property
    def is_on(self) -> bool:
        """Return the state of the sensor."""
        return self.device.zones[self._zone_id][1] == "1"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the zone on."""
        await self.device.set_zone(self._zone_id, "zone_onoff", "1")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the zone off."""
        await self.device.set_zone(self._zone_id, "zone_onoff", "0")


class DaikinStreamerSwitch(DaikinEntity, SwitchEntity):
    """Streamer state."""

    _attr_name = "Streamer"
    _attr_translation_key = "streamer"

    def __init__(self, coordinator: DaikinCoordinator) -> None:
        """Initialize switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.device.mac}-streamer"

    @property
    def is_on(self) -> bool:
        """Return the state of the sensor."""
        return DAIKIN_ATTR_STREAMER in self.device.represent(DAIKIN_ATTR_ADVANCED)[1]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the zone on."""
        await self.device.set_streamer("on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the zone off."""
        await self.device.set_streamer("off")


class DaikinToggleSwitch(DaikinEntity, SwitchEntity):
    """Switch state."""

    _attr_translation_key = "toggle"

    def __init__(self, coordinator: DaikinCoordinator) -> None:
        """Initialize switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.device.mac}-toggle"

    @property
    def is_on(self) -> bool:
        """Return the state of the sensor."""
        return "off" not in self.device.represent(DAIKIN_ATTR_MODE)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the device on."""
        await self.device.set({DAIKIN_ATTR_MODE: "auto"})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the device off."""
        await self.device.set({DAIKIN_ATTR_MODE: "off"})


class DaikinOutdoorQuietSwitch(DaikinEntity, SwitchEntity):
    """Outdoor unit quiet mode (demand control).

    Uses /aircon/set_demand_control to limit outdoor unit power.
    When ON: en_demand=1, max_pow=50 (quiet)
    When OFF: en_demand=0
    """

    _attr_name = "Outdoor Quiet"
    _attr_icon = "mdi:volume-off"

    def __init__(self, coordinator: DaikinCoordinator, host: str) -> None:
        """Initialize switch."""
        super().__init__(coordinator)
        self._host = host
        self._attr_unique_id = f"{self.device.mac}-outdoor_quiet"
        self._is_on = False

    @property
    def is_on(self) -> bool:
        """Return True if demand control is active."""
        return self._is_on

    async def async_update(self) -> None:
        """Fetch current demand control state."""
        try:
            session = async_get_clientsession(self.hass)
            url = f"http://{self._host}/aircon/get_demand_control"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                text = await resp.text()
                data = parse_daikin_response(text)
                self._is_on = data.get("en_demand") == "1"
        except Exception:
            pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable outdoor quiet mode."""
        session = async_get_clientsession(self.hass)
        url = f"http://{self._host}/aircon/set_demand_control"
        params = {"type": "1", "en_demand": "1", "mode": "0", "max_pow": "50"}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)):
                self._is_on = True
        except Exception:
            pass

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable outdoor quiet mode."""
        session = async_get_clientsession(self.hass)
        url = f"http://{self._host}/aircon/set_demand_control"
        params = {"type": "1", "en_demand": "0", "mode": "0", "max_pow": "100"}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)):
                self._is_on = False
        except Exception:
            pass
