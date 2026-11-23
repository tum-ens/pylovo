"""What the stages of the LV generation of one postcode share."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class GenerationContext:
    """The postcode being generated and the objects every stage works with.

    Attributes:
        plz: The postcode area.
        dbc: The :class:`~pylovo.database.database_client.DatabaseClient` of the run (one connection, one
            transaction per postcode, the temporary tables of the postcode).
        logger: The logger of the run.
        consumer_categories: The consumer categories of the configuration, indexed by
            :func:`pylovo.utils.index_consumer_categories`.
        inf_dbc: The :class:`~pylovo.infdb.infdb_client.InfdbClient` for the source data; only the input
            preparation reads it.
    """

    plz: int
    dbc: Any
    logger: logging.Logger
    consumer_categories: pd.DataFrame | None = None
    inf_dbc: Any = None


class Stage:
    """One stage of the LV generation of a postcode.

    A stage is created for one :class:`GenerationContext`; ``plz``, ``dbc``, ``logger``,
    ``consumer_categories`` and ``inf_dbc`` are the context's.
    """

    def __init__(self, context: GenerationContext):
        self.context = context
        self.plz = context.plz
        self.dbc = context.dbc
        self.logger = context.logger
        self.consumer_categories = context.consumer_categories
        self.inf_dbc = context.inf_dbc
