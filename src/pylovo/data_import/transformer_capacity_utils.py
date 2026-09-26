"""Transformer ratings from ``config_generation.yaml`` (``TRANSFORMERS``) for the transformer UI."""

from typing import Dict, List

from pylovo.config_loader import CONFIG_GENERATION


def get_transformer_capacities() -> List[Dict[str, int]]:
    """Return the configured transformer types sorted by rated power.

    Returns:
        One dictionary per transformer with ``name`` (e.g. ``'Tr_100'``), ``s_max_kva``,
        ``cost_eur`` and ``typ``; an empty list if ``TRANSFORMERS`` is not configured.
    """
    if not CONFIG_GENERATION or 'TRANSFORMERS' not in CONFIG_GENERATION:
        return []

    transformer_capacities = [
        {
            'name': transformer['name'],
            's_max_kva': transformer['s_max_kva'],
            'cost_eur': transformer['cost_eur'],
            'typ': transformer['typ'],
        }
        for transformer in CONFIG_GENERATION['TRANSFORMERS']
    ]
    transformer_capacities.sort(key=lambda x: x['s_max_kva'])
    return transformer_capacities


def get_transformer_capacity_options() -> List[Dict[str, str]]:
    """Return the transformer ratings as dropdown options for the UI.

    Returns:
        One dictionary per transformer with ``value`` (kVA as string), ``label``
        (e.g. ``'100 kVA (Tr_100)'``) and ``name``.
    """
    return [
        {
            'value': str(capacity['s_max_kva']),
            'label': f"{capacity['s_max_kva']} kVA ({capacity['name']})",
            'name': capacity['name'],
        }
        for capacity in get_transformer_capacities()
    ]
