"""Stub ``homeassistant.util.unit_conversion``: temperature only.

Same formulas as Home Assistant's ``TemperatureConverter``.
"""

from homeassistant.const import UnitOfTemperature

_TO_CELSIUS = {
    UnitOfTemperature.CELSIUS: lambda value: value,
    UnitOfTemperature.FAHRENHEIT: lambda value: (value - 32.0) / 1.8,
    UnitOfTemperature.KELVIN: lambda value: value - 273.15,
}
_FROM_CELSIUS = {
    UnitOfTemperature.CELSIUS: lambda value: value,
    UnitOfTemperature.FAHRENHEIT: lambda value: value * 1.8 + 32.0,
    UnitOfTemperature.KELVIN: lambda value: value + 273.15,
}


class TemperatureConverter:
    @classmethod
    def convert(cls, value: float, from_unit: str, to_unit: str) -> float:
        if from_unit == to_unit:
            return value
        return _FROM_CELSIUS[to_unit](_TO_CELSIUS[from_unit](value))
