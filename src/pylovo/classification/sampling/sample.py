"""Draw a representative sample of postcode areas (PLZ) for the grid classification.

The number of samples per RegioStaR 7 class is proportional to the population of
the class (``N_SAMPLES`` in ``config_classification.yaml``). Within a class, PLZ
are drawn with probabilities that follow the population-density distribution.
The sample set is stored in ``pylovo.sample_set`` under ``CLASSIFICATION_VERSION``.
"""
import numpy as np
import pandas as pd

import pylovo.database.database_client as dbc
from pylovo.config_loader import (
    CLASSIFICATION_REGION,
    CLASSIFICATION_VERSION,
    CLASSIFICATION_VERSION_COMMENT,
    MUNICIPAL_REGISTER,
    N_SAMPLES,
    REGION_DICT,
    VERSION_ID,
)

_db_client: dbc.DatabaseClient | None = None


def _get_db_client() -> dbc.DatabaseClient:
    """Return the module's shared database client and connect on first use."""
    global _db_client
    if _db_client is None:
        _db_client = dbc.DatabaseClient()
    return _db_client


def check_if_classification_version_exists() -> None:
    """Register ``CLASSIFICATION_VERSION`` in ``pylovo.classification_version``.

    Despite its name, the function also creates the entry (with
    ``CLASSIFICATION_VERSION_COMMENT`` and ``CLASSIFICATION_REGION``) if it is new.

    Raises:
        Exception: If the classification version already exists.
    """
    db_client = _get_db_client()
    cur = db_client.cur
    cur.execute(
        'SELECT COUNT(*) FROM pylovo.classification_version WHERE "classification_id" = %(c)s',
        {"c": CLASSIFICATION_VERSION},
    )
    version_exists = cur.fetchone()[0]
    if version_exists:
        raise Exception(f"Classification version:  {CLASSIFICATION_VERSION} already exists. Create a new one.")

    cur.execute(
        """INSERT INTO pylovo.classification_version
               (classification_id, classification_version_comment, classification_region)
           VALUES (%(c)s, %(comment)s, %(region)s)""",
        {"c": CLASSIFICATION_VERSION, "comment": CLASSIFICATION_VERSION_COMMENT, "region": CLASSIFICATION_REGION},
    )
    print(cur.statusmessage)
    db_client.conn.commit()
    print(f"Classification version: {CLASSIFICATION_VERSION} was added")


def perc_of_pop_per_class(regiostar_plz: pd.DataFrame) -> dict:
    """Split ``N_SAMPLES`` over the RegioStaR 7 classes in proportion to their population.

    Args:
        regiostar_plz: Municipal register rows with ``pop`` and ``regio7`` columns.

    Returns:
        Mapping ``regio7 class -> number of samples`` (rounded).
    """
    total_pop = regiostar_plz["pop"].sum()
    pop_per_class = regiostar_plz.groupby("regio7")["pop"].sum()
    samples_dyn = round(pop_per_class / total_pop * N_SAMPLES)
    return {k: int(v) for k, v in samples_dyn.items()}


def get_samples_within_regiostar_class(reg_class, no_samples, regiostar_plz):
    """Draw ``no_samples`` PLZ of one RegioStaR 7 class without replacement.

    The population densities of the class are split into 5 bins (fewer than 100
    PLZ) or 10 bins. Every PLZ is drawn with probability ``bin share / PLZ in bin``,
    so the sample follows the density distribution of the class.

    Args:
        reg_class: RegioStaR 7 class (``regio7``).
        no_samples: Number of PLZ to draw.
        regiostar_plz: Municipal register rows.

    Returns:
        pd.DataFrame: The selected PLZ with their register data and bin columns
        (``bin_no``, ``bins``, ``perc_bin``, ``count``, ``perc``).
    """
    regiostar_i = regiostar_plz[regiostar_plz['regio7'] == reg_class].copy()
    len_i = len(regiostar_i)
    if len_i < 100:
        no_bins = 5
    else:
        no_bins = 10
    # Same bin edges and counts as matplotlib's hist(), without drawing a figure.
    count, bins = np.histogram(regiostar_i["pop_den"], no_bins)
    count = count.astype(float)
    perc = count / len_i
    df_bins = pd.DataFrame()
    df_bins["bins"] = pd.Series(bins)
    df_bins["perc_bin"] = pd.Series(perc)
    df_bins["bin_no"] = df_bins.index
    df_bins["count"] = pd.Series(count)
    labels = np.arange(len(df_bins) - 1)
    # assign dataframe rows to bins
    regiostar_i.loc[:, "bin_no"] = pd.cut(x=regiostar_i['pop_den'], bins=df_bins["bins"], labels=labels,
                                          include_lowest=True, ordered=False)
    regiostar_i_bins = pd.merge(regiostar_i, df_bins, on="bin_no")
    regiostar_i_bins["perc"] = regiostar_i_bins["perc_bin"] / regiostar_i_bins["count"]
    # Sampling:
    selected = np.random.choice(regiostar_i_bins["plz"], no_samples, p=regiostar_i_bins["perc"],
                                replace=False)
    plz_selected = pd.DataFrame()
    plz_selected["plz"] = pd.Series(selected)
    reg_i_selected = pd.merge(plz_selected, regiostar_i_bins, on="plz")

    return reg_i_selected


def get_samples_with_regiostar(samples_per_class, regiostar_plz):
    """Draw the sample set over all RegioStaR 7 classes.

    Args:
        samples_per_class: Mapping ``regio7 class -> number of samples``.
        regiostar_plz: Municipal register rows.

    Returns:
        pd.DataFrame: Selected PLZ with ``ags`` and the bin columns of
        :func:`get_samples_within_regiostar_class` (register attributes dropped).
    """
    reg_selected = pd.DataFrame()
    for i in samples_per_class:
        reg_i_selected = get_samples_within_regiostar_class(i, samples_per_class[i], regiostar_plz)
        reg_selected = pd.concat([reg_selected, reg_i_selected])
    # Drop columns before returning
    reg_selected = reg_selected.drop(columns=[
        'pop', 'area', 'lat', 'lon', 'name_city', 'fed_state', 'regio7', 'regio5', 'pop_den'
    ], errors='ignore')
    return reg_selected


def sample_set_to_db(regiostar_samples_result: pd.DataFrame):
    """Append the sample set to the table ``pylovo.sample_set``.

    Args:
        regiostar_samples_result: Selected PLZ with ``classification_id`` column.
    """
    db_client = _get_db_client()
    regiostar_samples_result.to_sql('sample_set', con=db_client.sqla_engine, if_exists='append', index=False)
    print(db_client.cur.statusmessage)
    db_client.conn.commit()


def get_federal_state_id() -> int:
    """Return the federal-state id of ``CLASSIFICATION_REGION`` (key in ``REGION_DICT``).

    Returns:
        int: Id as used in the ``fed_state`` column of the municipal register.
    """
    return [k for k, v in REGION_DICT.items() if v == CLASSIFICATION_REGION][0]


def create_sample_set(restrict_to_postcode_result: bool = False):
    """Create the sample set of representative PLZ for ``CLASSIFICATION_REGION``.

    Registers ``CLASSIFICATION_VERSION``, draws the sample from the municipal
    register (Germany or one federal state) and writes it to ``pylovo.sample_set``.

    Args:
        restrict_to_postcode_result: Only sample PLZ that already have a
            ``pylovo.postcode_result`` entry for ``VERSION_ID``.

    Raises:
        Exception: If ``CLASSIFICATION_VERSION`` already exists.
    """
    check_if_classification_version_exists()
    db_client = _get_db_client()
    regiostar_plz = db_client.get_municipal_register()

    # some PLZ might appear multiple times for small municipalities that share PLZ
    regiostar_plz = regiostar_plz.drop_duplicates(subset="plz")

    if restrict_to_postcode_result:
        query = """SELECT DISTINCT postcode_result_plz
                   FROM pylovo.postcode_result
                   WHERE version_id = %(v)s;"""
        db_client.cur.execute(query, {"v": VERSION_ID})
        available_plz = {row[0] for row in db_client.cur.fetchall()}
        regiostar_plz = regiostar_plz[regiostar_plz["plz"].isin(available_plz)]

    # restrict to federal state if indicated in config classification
    if CLASSIFICATION_REGION != 'Germany':
        federal_state_id = get_federal_state_id()
        regiostar_plz = regiostar_plz[regiostar_plz['fed_state'] == federal_state_id]

    # create sample dataset
    samples = perc_of_pop_per_class(regiostar_plz)
    regiostar_samples_result = get_samples_with_regiostar(samples, regiostar_plz)
    regiostar_samples_result = regiostar_samples_result.reset_index()
    regiostar_samples_result = regiostar_samples_result.rename(columns={'index': 'classification_id'})
    regiostar_samples_result['classification_id'] = CLASSIFICATION_VERSION
    sample_set_to_db(regiostar_samples_result)


def get_sample_set() -> pd.DataFrame:
    """Read the sample set of ``CLASSIFICATION_VERSION`` joined with the municipal register.

    Returns:
        pd.DataFrame: One row per sampled PLZ with the ``MUNICIPAL_REGISTER`` columns.
    """
    cur = _get_db_client().cur
    query = """SELECT ss.plz, mr.pop, mr.area, mr.lat, mr.lon, ss.ags, mr.name_city, mr.fed_state, mr.regio7, mr.regio5, mr.pop_den
    FROM pylovo.sample_set ss
    JOIN pylovo.municipal_register mr ON ss.plz = mr.plz AND ss.ags = mr.ags
    WHERE ss.classification_id = %(c)s;"""
    cur.execute(query, {"c": CLASSIFICATION_VERSION})
    sample_set = cur.fetchall()
    df_sample_set = pd.DataFrame(sample_set, columns=MUNICIPAL_REGISTER)
    return df_sample_set
