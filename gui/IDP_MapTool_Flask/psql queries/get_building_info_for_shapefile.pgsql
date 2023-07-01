SELECT osm_id, area, building_t, geom, ST_Centroid(geom), floors::int FROM res
WHERE ST_Contains(ST_Transform(ST_GeomFromGeoJSON('{
    "type": "Polygon",
    "coordinates": [
        [
            [
                11.03744,
                49.715771
            ],
            [
                11.03744,
                49.717894
            ],
            [
                11.04377,
                49.717894
            ],
            [
                11.04377,
                49.715771
            ],
            [
                11.03744,
                49.715771
            ]
        ]
    ]
}'), 3035), ST_Centroid(res.geom));


