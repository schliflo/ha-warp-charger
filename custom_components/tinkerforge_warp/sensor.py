"""Sensors for the Tinkerforge WARP integration."""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from warp_charger.const import (
    CHARGER_STATE,
    DC_FAULT_STATE,
    ERROR_STATE,
    IEC61851_STATE,
    INT32_MAX,
    METER_VALUES,
)

from .entity import (
    VALUE_ERRORS,
    States,
    WarpConfigEntry,
    WarpEntity,
    WarpEntityDescription,
    config_enabled,
    evse_version,
    has_feature,
)

PARALLEL_UPDATES = 0

MAX_METER_SLOTS = 10
METER_CLASS_NONE = 0
METER_CLASS_ISO15118 = 9
EVSE_UPTIME_WRAP = 2**32
RESTART_TOLERANCE = timedelta(seconds=5)

_UNITS: dict[str, str] = {
    "V": UnitOfElectricPotential.VOLT,
    "A": UnitOfElectricCurrent.AMPERE,
    "W": UnitOfPower.WATT,
    "kWh": UnitOfEnergy.KILO_WATT_HOUR,
    "Hz": UnitOfFrequency.HERTZ,
    "%": PERCENTAGE,
}

_MEASUREMENT = SensorStateClass.MEASUREMENT
_TOTAL = SensorStateClass.TOTAL_INCREASING

# meter value id -> (device class, state class, enabled by default)
METER_SENSORS: dict[int, tuple[SensorDeviceClass, SensorStateClass, bool]] = {
    **dict.fromkeys((1, 2, 3), (SensorDeviceClass.VOLTAGE, _MEASUREMENT, False)),
    **dict.fromkeys((14, 18, 22), (SensorDeviceClass.CURRENT, _MEASUREMENT, False)),
    **dict.fromkeys((39, 48, 57), (SensorDeviceClass.POWER, _MEASUREMENT, False)),
    74: (SensorDeviceClass.POWER, _MEASUREMENT, True),
    209: (SensorDeviceClass.ENERGY, _TOTAL, True),
    211: (SensorDeviceClass.ENERGY, _TOTAL, True),
    364: (SensorDeviceClass.FREQUENCY, _MEASUREMENT, False),
    10: (SensorDeviceClass.VOLTAGE, _MEASUREMENT, True),
    35: (SensorDeviceClass.CURRENT, _MEASUREMENT, True),
    160: (SensorDeviceClass.POWER, _MEASUREMENT, True),
    405: (SensorDeviceClass.ENERGY, _TOTAL, True),
    407: (SensorDeviceClass.ENERGY, _TOTAL, True),
    430: (SensorDeviceClass.VOLTAGE, _MEASUREMENT, False),
    441: (SensorDeviceClass.CURRENT, _MEASUREMENT, False),
    453: (SensorDeviceClass.POWER, _MEASUREMENT, True),
    472: (SensorDeviceClass.ENERGY, _TOTAL, True),
    387: (SensorDeviceClass.BATTERY, _MEASUREMENT, True),
    475: (SensorDeviceClass.ENERGY_STORAGE, _MEASUREMENT, True),
}


@dataclass(frozen=True, kw_only=True)
class WarpSensorEntityDescription(WarpEntityDescription, SensorEntityDescription):
    """Describes a WARP sensor."""


class WarpSensor(WarpEntity, SensorEntity):
    """Sensor whose native value follows its description's ``value_fn``."""

    entity_description: WarpSensorEntityDescription

    @callback
    def _update_from_value(self, value: Any) -> None:
        self._attr_native_value = value


class WarpLastRestartSensor(WarpSensor):
    """Restart time derived from the 1 Hz keep-alive; ignores jitter below 5 s."""

    @callback
    def _recompute(self) -> bool:
        previous = self.warp_value
        was_initialized = self._initialized
        was_available = self._available
        changed = super()._recompute()
        if (
            changed
            and was_initialized
            and was_available == self._available
            and isinstance(previous, datetime)
            and isinstance(self.warp_value, datetime)
            and abs(self.warp_value - previous) <= RESTART_TOLERANCE
        ):
            self.warp_value = previous
            self._update_from_value(previous)
            return False
        return changed


# ---- value helpers ----


def _enum(
    key: str, topic: str, field: str, table: dict[int, str], **kwargs: Any
) -> WarpSensorEntityDescription:
    return WarpSensorEntityDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENUM,
        options=list(table.values()),
        topics=(topic,),
        value_fn=lambda states: table.get(states[topic][field]),
        **kwargs,
    )


def _session_energy(states: States) -> float | None:
    current = states["charge_tracker/current_charge"]
    if current["user_id"] == -1:
        return None
    start = current["meter_start"]
    if states.get("meter/values") and start is not None:
        total = states["meter/values"]["energy_abs"]
    else:
        slot = states["evse/meter_config"]["slot"]
        total = states[f"meters/{slot}/values"][states[f"meters/{slot}/value_ids"].index(209)]
    return max(0.0, round(total - start, 3))


def _session_start(states: States) -> datetime | None:
    current = states["charge_tracker/current_charge"]
    if current["user_id"] == -1:
        return None
    if (minutes := current["timestamp_minutes"]) > 0:
        return datetime.fromtimestamp(minutes * 60, UTC)
    # No RTC time: both values are EVSE uptime in ms (32-bit, wraps).
    elapsed = states["evse/low_level_state"]["uptime"] - current["evse_uptime_start"]
    if elapsed < 0:
        elapsed += EVSE_UPTIME_WRAP
    started = dt_util.utcnow() - timedelta(milliseconds=elapsed)
    return started.replace(second=0, microsecond=0)


def _session_user(states: States) -> str | None:
    user_id = states["charge_tracker/current_charge"]["user_id"]
    if user_id == -1:
        return None
    for user in states["users/config"]["users"]:
        if user["id"] == user_id:
            return user["display_name"]
    return None


def _last_session_energy(states: States) -> float:
    return round(states["charge_tracker/last_charges"][-1]["energy_charged"], 3)


def _price(states: States) -> float | None:
    price = states["day_ahead_prices/state"]["current_price"]
    return None if price == INT32_MAX else price / 1000


def _forecast(field: str) -> Callable[[States], float | None]:
    def value(states: States) -> float | None:
        wh = states["solar_forecast/state"][field]
        return None if wh < 0 else wh / 1000

    return value


def _rssi(states: States) -> int | None:
    rssi = states["wifi/state"]["sta_rssi"]
    return None if rssi <= -127 or rssi == 0 else rssi


def _last_restart(states: States) -> datetime:
    return dt_util.utcnow() - timedelta(milliseconds=states["info/keep_alive"]["uptime"])


def _meter_value_fn(slot: int, index: int, digits: int) -> Callable[[States], float | int | None]:
    topic = f"meters/{slot}/values"

    def value(states: States) -> float | int | None:
        raw = states[topic][index]
        if raw is None:
            return None
        return round(raw, digits) if digits else round(raw)

    return value


# ---- descriptions ----


def _charger(states: States) -> list[WarpSensorEntityDescription]:
    version = evse_version(states)
    result = [
        _enum("charger_state", "evse/state", "charger_state", CHARGER_STATE),
        WarpSensorEntityDescription(
            key="allowed_charging_current",
            translation_key="allowed_charging_current",
            device_class=SensorDeviceClass.CURRENT,
            state_class=_MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=1,
            topics=("evse/state",),
            value_fn=lambda s: s["evse/state"]["allowed_charging_current"] / 1000,
        ),
        _enum(
            "error_state",
            "evse/state",
            "error_state",
            ERROR_STATE,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        _enum(
            "iec61851_state",
            "evse/state",
            "iec61851_state",
            IEC61851_STATE,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        ),
    ]
    if version >= 20:
        result.append(
            _enum(
                "dc_fault_state",
                "evse/state",
                "dc_fault_current_state",
                DC_FAULT_STATE,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
            )
        )
    if version >= 30:
        result += [
            WarpSensorEntityDescription(
                key="phases_active",
                translation_key="phases_active",
                topics=("evse/low_level_state",),
                value_fn=lambda s: s["evse/low_level_state"]["phases_current"] or None,
            ),
            WarpSensorEntityDescription(
                key="temperature",
                translation_key="temperature",
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=_MEASUREMENT,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                entity_category=EntityCategory.DIAGNOSTIC,
                topics=("evse/low_level_state",),
                value_fn=lambda s: round(s["evse/low_level_state"]["temperature"] / 100, 1),
            ),
        ]
    return result


def _session(states: States) -> list[WarpSensorEntityDescription]:
    topics = ["charge_tracker/current_charge", "meter/values"]
    with contextlib.suppress(*VALUE_ERRORS):
        topics.append(f"meters/{states['evse/meter_config']['slot']}/values")
    return [
        WarpSensorEntityDescription(
            key="session_energy",
            translation_key="session_energy",
            device_class=SensorDeviceClass.ENERGY,
            state_class=_TOTAL,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            topics=tuple(topics),
            value_fn=_session_energy,
        ),
        WarpSensorEntityDescription(
            key="session_start",
            translation_key="session_start",
            device_class=SensorDeviceClass.TIMESTAMP,
            topics=("charge_tracker/current_charge",),
            value_fn=_session_start,
        ),
        WarpSensorEntityDescription(
            key="session_user",
            translation_key="session_user",
            topics=("charge_tracker/current_charge", "users/config"),
            value_fn=_session_user,
        ),
        WarpSensorEntityDescription(
            key="last_session_energy",
            translation_key="last_session_energy",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            topics=("charge_tracker/last_charges",),
            value_fn=_last_session_energy,
        ),
    ]


def _meters(states: States) -> list[WarpSensorEntityDescription]:
    result: list[WarpSensorEntityDescription] = []
    try:
        charger_slot = states["evse/meter_config"]["slot"] if has_feature(states, "evse") else None
    except VALUE_ERRORS:
        charger_slot = None
    for slot in range(MAX_METER_SLOTS):
        try:
            if states[f"meters/{slot}/config"][0] in (METER_CLASS_NONE, METER_CLASS_ISO15118):
                continue  # ISO 15118 vehicle meter: covered by the ev/state entities
            value_ids = list(states[f"meters/{slot}/value_ids"])
        except VALUE_ERRORS:
            continue
        device = None if slot == charger_slot else f"meter_{slot}"
        seen: set[int] = set()
        for index, value_id in enumerate(value_ids):
            if value_id in seen or value_id not in METER_SENSORS or value_id not in METER_VALUES:
                continue
            seen.add(value_id)
            meter_value = METER_VALUES[value_id]
            device_class, state_class, enabled = METER_SENSORS[value_id]
            result.append(
                WarpSensorEntityDescription(
                    key=f"meter_{slot}_{meter_value.key}",
                    translation_key=meter_value.key,
                    device_class=device_class,
                    state_class=state_class,
                    native_unit_of_measurement=_UNITS.get(meter_value.unit or ""),
                    entity_registry_enabled_default=enabled,
                    topics=(f"meters/{slot}/values",),
                    value_fn=_meter_value_fn(slot, index, meter_value.digits),
                    device=device,
                )
            )
    return result


def _vehicle() -> list[WarpSensorEntityDescription]:
    return [
        WarpSensorEntityDescription(
            key="vehicle_capacity",
            translation_key="vehicle_capacity",
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            state_class=_MEASUREMENT,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            topics=("ev/state",),
            value_fn=lambda s: s["ev/state"]["capacity"],
        ),
        WarpSensorEntityDescription(
            key="vehicle_name",
            translation_key="vehicle_name",
            topics=("ev/state",),
            value_fn=lambda s: s["ev/state"]["name"] or None,
        ),
    ]


def _energy_management(states: States) -> list[WarpSensorEntityDescription]:
    result: list[WarpSensorEntityDescription] = []
    if config_enabled(states, "p14a_enwg/config"):
        result.append(
            WarpSensorEntityDescription(
                key="p14a_limit",
                translation_key="p14a_limit",
                device_class=SensorDeviceClass.POWER,
                state_class=_MEASUREMENT,
                native_unit_of_measurement=UnitOfPower.WATT,
                topics=("p14a_enwg/state",),
                value_fn=lambda s: (
                    s["p14a_enwg/state"]["limit_w"] if s["p14a_enwg/state"]["active"] else None
                ),
            )
        )
    if config_enabled(states, "day_ahead_prices/config"):
        result.append(
            WarpSensorEntityDescription(
                key="electricity_price",
                translation_key="electricity_price",
                state_class=_MEASUREMENT,
                native_unit_of_measurement="ct/kWh",
                suggested_display_precision=2,
                topics=("day_ahead_prices/state",),
                value_fn=_price,
            )
        )
    if config_enabled(states, "solar_forecast/config"):
        result += [
            WarpSensorEntityDescription(
                key=key,
                translation_key=key,
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                suggested_display_precision=1,
                topics=("solar_forecast/state",),
                value_fn=_forecast(field),
            )
            for key, field in (
                ("solar_forecast_today", "wh_today"),
                ("solar_forecast_today_remaining", "wh_today_remaining"),
                ("solar_forecast_tomorrow", "wh_tomorrow"),
            )
        ]
    return result


def _diagnostics(states: States) -> list[WarpSensorEntityDescription]:
    result: list[WarpSensorEntityDescription] = []
    if "esp32/temperature" in states:
        result.append(
            WarpSensorEntityDescription(
                key="esp_temperature",
                translation_key="esp_temperature",
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=_MEASUREMENT,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                entity_category=EntityCategory.DIAGNOSTIC,
                topics=("esp32/temperature",),
                value_fn=lambda s: round(s["esp32/temperature"]["temperature"] / 100, 1),
            )
        )
    if config_enabled(states, "wifi/sta_config", "enable_sta"):
        result.append(
            WarpSensorEntityDescription(
                key="wifi_rssi",
                translation_key="wifi_rssi",
                device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                state_class=_MEASUREMENT,
                native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                topics=("wifi/state",),
                value_fn=_rssi,
            )
        )
    if "info/keep_alive" in states:
        result.append(
            WarpSensorEntityDescription(
                key="last_restart",
                translation_key="last_restart",
                device_class=SensorDeviceClass.TIMESTAMP,
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
                topics=("info/keep_alive",),
                value_fn=_last_restart,
            )
        )
    return result


def descriptions(states: States) -> list[WarpSensorEntityDescription]:
    """Return the sensors this device supports."""
    result: list[WarpSensorEntityDescription] = []
    if has_feature(states, "evse"):
        result += _charger(states)
        if "charge_tracker/current_charge" in states:
            result += _session(states)
    if has_feature(states, "meters"):
        result += _meters(states)
    if "ev/state" in states:
        result += _vehicle()
    result += _energy_management(states)
    result += _diagnostics(states)
    return result


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WarpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the WARP sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        (WarpLastRestartSensor if description.key == "last_restart" else WarpSensor)(
            runtime, description
        )
        for description in descriptions(runtime.client.states)
    )
