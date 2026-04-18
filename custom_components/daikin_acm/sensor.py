"""Support for Daikin AC sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydaikin.daikin_base import Appliance

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_COMPRESSOR_FREQUENCY,
    ATTR_COOL_ENERGY,
    ATTR_ENERGY_TODAY,
    ATTR_HEAT_ENERGY,
    ATTR_HUMIDITY,
    ATTR_INSIDE_TEMPERATURE,
    ATTR_OUTSIDE_TEMPERATURE,
    ATTR_TARGET_HUMIDITY,
    ATTR_TOTAL_ENERGY_TODAY,
    ATTR_TOTAL_POWER,
)
from .coordinator import DaikinConfigEntry, DaikinCoordinator
from .entity import DaikinEntity


@dataclass(frozen=True, kw_only=True)
class DaikinSensorEntityDescription(SensorEntityDescription):
    """Describes Daikin sensor entity."""

    value_func: Callable[[Appliance], float | None]


SENSOR_TYPES: tuple[DaikinSensorEntityDescription, ...] = (
    DaikinSensorEntityDescription(
        key=ATTR_INSIDE_TEMPERATURE,
        translation_key="inside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_func=lambda device: device.inside_temperature,
    ),
    DaikinSensorEntityDescription(
        key=ATTR_OUTSIDE_TEMPERATURE,
        translation_key="outside_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_func=lambda device: device.outside_temperature,
    ),
    DaikinSensorEntityDescription(
        key=ATTR_HUMIDITY,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_func=lambda device: device.humidity,
    ),
    DaikinSensorEntityDescription(
        key=ATTR_TARGET_HUMIDITY,
        translation_key="target_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_func=lambda device: device.humidity,
    ),
    DaikinSensorEntityDescription(
        key=ATTR_TOTAL_POWER,
        translation_key="compressor_estimated_power_consumption",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        value_func=lambda device: round(device.current_total_power_consumption, 2),
    ),
    DaikinSensorEntityDescription(
        key=ATTR_COOL_ENERGY,
        translation_key="cool_energy_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_registry_enabled_default=False,
        value_func=lambda device: round(device.last_hour_cool_energy_consumption, 2),
    ),
    DaikinSensorEntityDescription(
        key=ATTR_HEAT_ENERGY,
        translation_key="heat_energy_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_registry_enabled_default=False,
        value_func=lambda device: round(device.last_hour_heat_energy_consumption, 2),
    ),
    DaikinSensorEntityDescription(
        key=ATTR_ENERGY_TODAY,
        translation_key="energy_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_func=lambda device: round(device.today_energy_consumption, 2),
    ),
    DaikinSensorEntityDescription(
        key=ATTR_COMPRESSOR_FREQUENCY,
        translation_key="compressor_frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        entity_registry_enabled_default=False,
        value_func=lambda device: device.compressor_frequency,
    ),
    DaikinSensorEntityDescription(
        key=ATTR_TOTAL_ENERGY_TODAY,
        translation_key="compressor_energy_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_registry_enabled_default=False,
        value_func=lambda device: round(device.today_total_energy_consumption, 2),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Daikin climate based on config_entry."""
    daikin_api = entry.runtime_data
    sensors = [ATTR_INSIDE_TEMPERATURE]
    if daikin_api.device.support_outside_temperature:
        sensors.append(ATTR_OUTSIDE_TEMPERATURE)
    if daikin_api.device.support_energy_consumption:
        sensors.append(ATTR_ENERGY_TODAY)
        sensors.append(ATTR_COOL_ENERGY)
        sensors.append(ATTR_HEAT_ENERGY)
        sensors.append(ATTR_TOTAL_POWER)
        sensors.append(ATTR_TOTAL_ENERGY_TODAY)
    if daikin_api.device.support_humidity:
        sensors.append(ATTR_HUMIDITY)
        sensors.append(ATTR_TARGET_HUMIDITY)
    if daikin_api.device.support_compressor_frequency:
        sensors.append(ATTR_COMPRESSOR_FREQUENCY)

    entities = [
        DaikinSensor(daikin_api, description)
        for description in SENSOR_TYPES
        if description.key in sensors
    ]

    # ACM additional sensors — always added
    entities.append(DaikinFirmwareSensor(daikin_api))
    entities.append(DaikinRuntimeSensor(daikin_api))

    async_add_entities(entities)


class DaikinSensor(DaikinEntity, SensorEntity):
    """Representation of a Sensor."""

    entity_description: DaikinSensorEntityDescription

    def __init__(
        self, coordinator: DaikinCoordinator, description: DaikinSensorEntityDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self.device.mac}-{description.key}"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        return self.entity_description.value_func(self.device)


# ---------------------------------------------------------------------------
# ACM additional sensors
# ---------------------------------------------------------------------------


class DaikinFirmwareSensor(DaikinEntity, SensorEntity):
    """Shows firmware version string from the adapter."""

    _attr_translation_key = "firmware_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: DaikinCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.device.mac}-firmware_version"

    @property
    def name(self) -> str:  # noqa: D102
        return "Firmware version"

    @property
    def native_value(self) -> str | None:
        """Return firmware version string."""
        ver = self.device.values.get("ver", None)
        if ver:
            return ver.replace("_", ".")
        return None


class DaikinRuntimeSensor(DaikinEntity, SensorEntity):
    """Shows today_runtime minutes from the adapter."""

    _attr_translation_key = "today_runtime"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: DaikinCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{self.device.mac}-today_runtime"

    @property
    def name(self) -> str:  # noqa: D102
        return "Today runtime"

    @property
    def native_value(self) -> int | None:
        """Return today's runtime in minutes."""
        val = self.device.values.get("today_runtime")
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
        return None
