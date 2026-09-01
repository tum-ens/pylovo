"""
Component specification dataclasses for electrical grid elements.

These backend-agnostic dataclasses define the interface between grid generation
algorithms and electrical simulation backends. Grid algorithms create spec objects,
and backends translate them to native API calls.

Note:
    Default values are configured for German distribution grids (0.4kV LV, 20kV MV).
    For other regions, override defaults when creating spec instances or consider
    config-driven regional parameters.

Classes:
    ComponentSpec: Base class for all specifications
    BusSpec: Bus/node specification
    TransformerSpec: MV/LV transformer specification
    LineSpec: Cable/line specification
    LoadSpec: Load specification
    ExtGridSpec: External grid connection specification
"""

from dataclasses import dataclass
from typing import Optional, Tuple


def normalize_cable_name(name: str) -> str:
    """
    Normalize cable name to canonical underscore format.

    SINGLE SOURCE OF TRUTH for cable name normalization.
    Canonical format: NAYY_4_120 (underscores, no 'x', no 'SE')

    Examples:
        "NAYY 4x120 SE" -> "NAYY_4_120"
        "NAYY 4 120"    -> "NAYY_4_120"
        "NAYY_4_120"    -> "NAYY_4_120"
    """
    import re
    normalized = name.replace(' ', '_')
    normalized = re.sub(r'(\d)x(\d)', r'\1_\2', normalized)
    normalized = re.sub(r'_?SE$', '', normalized)
    return normalized


@dataclass
class ComponentSpec:
    """Base class for all component specifications."""
    name: str
    component_type: str = ""


@dataclass
class BusSpec(ComponentSpec):
    """
    Bus specification for German 3-phase grids.

    Standard voltages:
    - 20kV for MV buses
    - 0.4kV for LV buses
    """
    voltage_kv: float = 0.4  # Nominal voltage in kV
    coordinates: Optional[Tuple[float, float]] = None  # (lon, lat) for visualization
    zone: Optional[str] = None  # Zone for load type tracking

    def __post_init__(self):
        self.component_type = "bus"


@dataclass
class TransformerSpec(ComponentSpec):
    """
    Transformer specification for MV/LV transformers.

    All transformers are 3-phase, 20kV/0.4kV.
    Ratings determined by grid generation algorithm.
    """
    bus1: str = ""  # Primary (MV) side bus name
    bus2: str = ""  # Secondary (LV) side bus name
    kva: float = 630.0  # Rated apparent power in kVA
    parallel: int = 1  # Number of parallel transformers

    def __post_init__(self):
        self.component_type = "transformer"


@dataclass
class LineSpec(ComponentSpec):
    """
    Line/Cable specification for distribution cables.

    All cables are 3-phase, underground NAYY cables.
    Cable type name references the registered cable definitions.
    """
    bus1: str = ""  # From bus name
    bus2: str = ""  # To bus name
    cable_name: str = ""  # Cable type from configured cable definitions (e.g. "NAYY_4_150")
    length_km: float = 0.0  # Cable length in km
    parallel: int = 1  # Number of parallel cables
    coordinates: Optional[list] = None  # Line geometry for visualization
    

    def __post_init__(self):
        self.component_type = "line"


@dataclass
class LoadSpec(ComponentSpec):
    """
    Load specification for household/building loads.

    All loads are 3-phase balanced.
    """
    bus: str = ""  # Bus name where load is connected
    kw: float = 0.0  # Active power in kW
    kvar: float = 0.0  # Reactive power in kvar
    max_p_mw: float = 0.0  # Maximum active power in MW
    service_design_p_mw: float = 0.0  # Local coincident load used to size the service cable
    operating_point_basis: Optional[str] = None  # Meaning of kw and kvar in the exported network
    category: Optional[str] = None  # Electrical simultaneity category
    load_units: float = 1.0  # Households for residential, otherwise component count
    consumer_vertex: Optional[int] = None  # Shared physical connection vertex

    def __post_init__(self):
        self.component_type = "load"

@dataclass
class ExtGridSpec(ComponentSpec):
    """External grid specification."""

    bus: str = ""
    vm_pu: float = 1.0

    def __post_init__(self):
        self.component_type = "ext_grid"
