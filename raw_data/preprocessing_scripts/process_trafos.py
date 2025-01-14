import time
import os
import geopandas as gpd
import numpy as np
import pandas as pd

AREA_THRESHOLD = 60
MIN_DISTANCE_BETWEEN_TRAFOS = 8
VOLTAGE_THRESHOLD = 110000
EPSG = 32633

SUBSTATIONS_GEOJSON = os.path.normpath(os.path.abspath('./raw_data/transformer_query/substations_bayern.geojson'))
SHOPPING_MALL_GEOJSON = os.path.normpath(os.path.abspath('./raw_data/transformer_query/shopping_mall_bayern.geojson'))
OUTPUT_GEOJSON = os.path.normpath(os.path.abspath('./raw_data/transformer_query/substations_bayern_processed.geojson'))

# timing of the script
start_time = time.time()

# Import geojson of substations/trafos. Trafos of "Deutsche Bahn" and historic trafos have already been deleted.
if not os.path.isfile(SUBSTATIONS_GEOJSON):
    raise FileNotFoundError(f"Path does not exist: {SUBSTATIONS_GEOJSON}")
gdf_substations = gpd.read_file(SUBSTATIONS_GEOJSON)
print('start:')
print(len(gdf_substations))

# the geodata imported from the geojson is imported in the CRS (Coordinate Reference System) WGS84, "EPSG","4326".
# It has lan and lat values. For area calculations it needs to be converted into a planar projection.
gdf_substations = gdf_substations.to_crs(EPSG)

# 1. eliminate all trafos that lay within other trafo geometries
gdf_substations['geom_type'] = gdf_substations.geom_type
gdf_points = gdf_substations.groupby('geom_type').get_group('Point')
gdf_polygon = gdf_substations.groupby('geom_type').get_group('Polygon')
union_of_polygons = gdf_polygon.geometry.unary_union
gdf_points['within_poly'] = gdf_points.within(union_of_polygons)
gdf_substations.drop(gdf_points[gdf_points['within_poly'] == True].index, inplace=True)
print('After step 1:')
print(len(gdf_substations))

# 2. eliminate all Umspannungswerke area larger than threshold and all transformers that are tagged within that area
gdf_substations['area'] = gdf_substations.area
gdf_substations.drop(gdf_substations[gdf_substations['area'] >= AREA_THRESHOLD].index, inplace=True)
print('After step 2:')
print(len(gdf_substations))

# 3. Delete all high voltage transformers
# replace any values that cannot be converted to float by nan
gdf_substations['voltage'] = (gdf_substations['voltage'].fillna(1).apply(lambda x: pd.to_numeric(x, errors='coerce')))
gdf_substations['voltage'] = gdf_substations['voltage'].astype(float)
gdf_substations.drop(gdf_substations[gdf_substations['voltage'] >= VOLTAGE_THRESHOLD].index, inplace=True)
print('After step 3:')
print(len(gdf_substations))

# 4. how many transformers are there in a radius of 5, 10, 15 m of each other
gdf_substations['centroid'] = gdf_substations.centroid
distance_matrix = gdf_substations['centroid'].apply(lambda c: gdf_substations['centroid'].distance(c))
# set lower triangle of matrix to nan
distance_matrix = distance_matrix.where(np.triu(np.ones(distance_matrix.shape)).astype(bool))
# set diagonal to nan
np.fill_diagonal(distance_matrix.values, float('nan'))
distance_matrix = distance_matrix[(distance_matrix < MIN_DISTANCE_BETWEEN_TRAFOS).any(axis=1)]
index_list = list(distance_matrix.index)
gdf_substations.drop(index=index_list, inplace=True)
print('After step 4:')
print(len(gdf_substations))

# 5. how many trafos are there in / next to mall?
gdf_shopping = gpd.read_file(SHOPPING_MALL_GEOJSON)
gdf_shopping = gdf_shopping.to_crs(EPSG)
union_of_shopping = gdf_shopping.geometry.unary_union
gdf_substations['within_shopping'] = gdf_substations.within(union_of_shopping)
gdf_substations.drop(gdf_substations[gdf_substations['within_shopping']].index, inplace=True)
print('After step 5:')
print(len(gdf_substations))

# drop geometry that can be of type polygon and point, use centroid as new geometry instead
# drop tag columns
gdf_substations.drop('geometry', axis=1, inplace=True)
gdf_substations.rename(columns={"centroid": "geometry"}, inplace=True)
gdf_substations.dropna(axis='columns', inplace=True)

# transform column id into osm_id as is used for buildings
gdf_substations['id'] = gdf_substations['id'].map(lambda x: x.lstrip('way/node/'))
gdf_substations.rename(columns={"id": "osm_id"}, inplace=True)
gdf_substations.drop('@id', axis=1, inplace=True)

# export geojson
gdf_substations.to_file(OUTPUT_GEOJSON, driver='GeoJSON')
print("--- %s seconds ---" % (time.time() - start_time))
