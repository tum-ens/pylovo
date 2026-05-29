import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pylovo.database.database_client as dbc
from pylovo.config_loader import *
from pylovo.database.utils_mixin import UtilsMixin

# According to the population distribution and energy consumption
# it is defined how many samples are to be choosen per class
samples_per_class_pop = {
    71: 18,
    72: 14,
    73: 25,
    74: 6,
    75: 6,
    76: 14,
    77: 16,
}

db_client = dbc.DatabaseClient()

def check_if_classification_version_exists():
    """checks whether classification version already exists.
     creates a new entry in classification version

    :raises Exception: if classification version already exists
    """
    cur = db_client.cur
    count_query = f"""SELECT COUNT(*) 
            FROM pylovo.classification_version 
            WHERE "classification_id" = {CLASSIFICATION_VERSION}"""
    cur.execute(count_query)
    version_exists = cur.fetchone()[0]
    if version_exists:
        raise Exception(f"Classification version:  {CLASSIFICATION_VERSION} already exists. Create a new one.")
    # df_plz.to_sql('sample_set', con=sqlalchemy_engine, if_exists='replace', index=False)
    # print(cur.statusmessage)
    # conn.commit()
    else:
        # create new version
        insert_query = f"""INSERT INTO pylovo.classification_version (classification_id, classification_version_comment, classification_region) VALUES
        ({CLASSIFICATION_VERSION}, '{CLASSIFICATION_VERSION_COMMENT}', '{CLASSIFICATION_REGION}')"""
        cur.execute(insert_query)
        print(cur.statusmessage)
        db_client.conn.commit()
        print(f"Classification version: {CLASSIFICATION_VERSION} was added")


def get_municipal_register_as_dataframe() -> pd.DataFrame:
    """get municipal register

    :return: municipal register
    :rtype: pd.DataFrame
    """
    regiostar_plz = db_client.get_municipal_register()
    return regiostar_plz


def perc_of_pop_per_class(regiostar_plz: pd.DataFrame) -> dict:
    """calculates the percentage of population for each regiostar class
    returns a dict"""
    total_pop = regiostar_plz["pop"].sum()
    pop_per_class = regiostar_plz.groupby("regio7")["pop"].sum()
    samples_dyn = round(pop_per_class / total_pop * N_SAMPLES)
    samples_dyn.to_dict()
    samples_dyn = dict((k, int(v)) for k, v in samples_dyn.items())
    return samples_dyn


def get_samples_within_regiostar_class(reg_class, no_samples, regiostar_plz):
    """
    for a given regiostar7 class and the number of samples, samples are representatively picked
    according to the population density distribution
    """

    regiostar_i = regiostar_plz[regiostar_plz['regio7'] == reg_class]
    len_i = len(regiostar_i)
    if len_i < 100:
        no_bins = 5
    else:
        no_bins = 10
    count, bins, ignored = plt.hist(regiostar_i["pop_den"], no_bins)
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
    """
    From regiostar7 - PLZ dataset (regiostar_plz) samples are extracted
    samples_per_class defines how many samples should be extracted per regiostar7 class
    The samples are choosen representatively based on the population density distribution of each class
    returns
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
    """writes sample set to database in table sample set

    :param regiostar_samples_result: table with the selected PLZ and their information
    :type regiostar_samples_result: pd.DataFrame

    """
    cur = db_client.cur
    regiostar_samples_result.to_sql('sample_set', con=db_client.sqla_engine, if_exists='append', index=False)
    print(cur.statusmessage)
    db_client.conn.commit()


def get_federal_state_id() -> int:
    """for a federal state / Bundesland get the id that is used in regiostar tables


    :return: id of federal state
    :rtype: int
    """

    id = [k for k, v in REGION_DICT.items() if v == CLASSIFICATION_REGION][0]
    return id


def create_sample_set(restrict_to_postcode_result: bool = False):
    """complete process of creating a sample set of representative PLZ for a Region
    that is either Germany or a federal state
    All subprocesses of sampling the PLZ are executed in this function.
    The result is written to database table 'sample set' with the classification version set in
    config_classification
    """

    check_if_classification_version_exists()
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
    """get a sample set from the database that has already been created

    :return: table of a complete sample set
    :rtype: pd.DataFrame
    """
    cur = db_client.cur
    query = f"""SELECT ss.plz, mr.pop, mr.area, mr.lat, mr.lon, ss.ags, mr.name_city, mr.fed_state, mr.regio7, mr.regio5, mr.pop_den
    FROM pylovo.sample_set ss
    JOIN pylovo.municipal_register mr ON ss.plz = mr.plz AND ss.ags = mr.ags
    WHERE ss.classification_id = {CLASSIFICATION_VERSION};"""
    cur.execute(query)
    sample_set = cur.fetchall()
    df_sample_set = pd.DataFrame(sample_set, columns=MUNICIPAL_REGISTER)
    return df_sample_set
