"""
Equipment dataclasses for electrical components.

These dataclasses represent physical equipment parameters loaded from the database.
They provide a structured way to pass equipment data to backend implementations.

Classes:
    CableEquipment: Cable/line electrical parameters
"""
from dataclasses import dataclass


@dataclass
class CableEquipment:
    """
    Minimal cable equipment adapter for electrical backends.

    Converts pylovo database cable tuples to backend-compatible objects (e.g. for OpenDSS).
    """
    name: str
    r_ohm_per_km: float
    x_ohm_per_km: float
    max_i_ka: float
    n_phases: int = 3
    voltage_level: str = "LV"
    max_i_a: int = 0

    def __post_init__(self):
        if self.max_i_a == 0:
            self.max_i_a = int(self.max_i_ka * 1000)
