"""Support for Daikin AirBase zones."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DaikinConfigEntry, DaikinCoordinator
from .entity import DaikinEntity
from .provisioning import parse_daikin_response

_LOGGER = logging.getLogger(__name__)

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

    # ACM additional switches
    host = entry.data.get(CONF_HOST, "")
    if host:
        switches.append(DaikinOutdoorQuietSwitch(daikin_api, host))
        switches.append(DaikinNightModeSwitch(daikin_api, host))

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


# ---------------------------------------------------------------------------
# ACM additional switches
# ---------------------------------------------------------------------------


async def _get_demand_control(host: str) -> dict[str, str]:
    """GET /aircon/get_demand_control and parse response."""
    url = f"http://{host}/aircon/get_demand_control"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                text = await resp.text()
                return parse_daikin_response(text)
    except Exception as err:
        _LOGGER.warning("get_demand_control failed for %s: %s", host, err)
        return {}


async def _set_demand_control(
    host: str, en_demand: int, max_pow: int, mode: int = 0
) -> bool:
    """SET /aircon/set_demand_control."""
    url = (
        f"http://{host}/aircon/set_demand_control"
        f"?type=1&en_demand={en_demand}&mode={mode}&max_pow={max_pow}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                text = await resp.text()
                data = parse_daikin_response(text)
                return data.get("ret") == "OK"
    except Exception as err:
        _LOGGER.warning("set_demand_control failed for %s: %s", host, err)
        return False


class DaikinOutdoorQuietSwitch(DaikinEntity, SwitchEntity):
    """Outdoor unit quiet mode via demand control."""

    _attr_translation_key = "outdoor_quiet"
    _attr_icon = "mdi:volume-off"

    def __init__(self, coordinator: DaikinCoordinator, host: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._host = host
        self._attr_unique_id = f"{self.device.mac}-outdoor_quiet"
        self._is_on: bool = False

    @property
    def name(self) -> str:  # noqa: D102
        return "Outdoor quiet"

    @property
    def is_on(self) -> bool:
        """Return demand control state."""
        return self._is_on

    async def async_update(self) -> None:
        """Poll demand control status."""
        data = await _get_demand_control(self._host)
        self._is_on = data.get("en_demand", "0") == "1"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable demand control — limit outdoor to 50%."""
        ok = await _set_demand_control(self._host, en_demand=1, max_pow=50)
        if ok:
            self._is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable demand control — full power."""
        ok = await _set_demand_control(self._host, en_demand=0, max_pow=100)
        if ok:
            self._is_on = False
            self.async_write_ha_state()


class DaikinNightModeSwitch(DaikinEntity, SwitchEntity):
    """Night mode — combines econo + silence fan + outdoor quiet."""

    _attr_translation_key = "night_mode"
    _attr_icon = "mdi:weather-night"

    def __init__(self, coordinator: DaikinCoordinator, host: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._host = host
        self._attr_unique_id = f"{self.device.mac}-night_mode"
        self._is_on: bool = False

    @property
    def name(self) -> str:  # noqa: D102
        return "Night mode"

    @property
    def is_on(self) -> bool:
        """Return night mode composite state."""
        return self._is_on

    async def async_update(self) -> None:
        """Poll state — night mode is ON if demand control is on."""
        data = await _get_demand_control(self._host)
        self._is_on = data.get("en_demand", "0") == "1"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable night mode: econo ON, fan silence (B), demand 50%."""
        # 1. Econo mode on
        try:
            await self.device.set_advanced_mode("econo", "on")
        except Exception as err:
            _LOGGER.warning("Night mode: set econo on failed: %s", err)

        # 2. Fan to silence (B)
        try:
            await self.device.set({"f_rate": "B"})
        except Exception as err:
            _LOGGER.warning("Night mode: set fan silence failed: %s", err)

        # 3. Outdoor demand control 50%
        await _set_demand_control(self._host, en_demand=1, max_pow=50)

        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable night mode: econo OFF, fan auto (A), demand 100%."""
        # 1. Econo mode off
        try:
            await self.device.set_advanced_mode("econo", "off")
        except Exception as err:
            _LOGGER.warning("Night mode: set econo off failed: %s", err)

        # 2. Fan to auto (A)
        try:
            await self.device.set({"f_rate": "A"})
        except Exception as err:
            _LOGGER.warning("Night mode: set fan auto failed: %s", err)

        # 3. Outdoor demand control off / full power
        await _set_demand_control(self._host, en_demand=0, max_pow=100)

        self._is_on = False
        self.async_write_ha_state()
