# This file contains the path to each file that is to be imported
import os
import sys

PATH_ZUORDNUNG_PLZ = os.path.join(os.path.dirname(__file__), 'data', 'zuordnung_plz_ort.xls')
sys.path.append(PATH_ZUORDNUNG_PLZ)

PATH_PLZ_EINWOHNER = os.path.join(os.path.dirname(__file__), 'data', 'plz_einwohner.xls')
sys.path.append(PATH_PLZ_EINWOHNER)
