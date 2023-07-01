SELECT json_build_object(
    'type', 'FeatureCollection',
    'features', json_agg(ST_AsGeoJSON(t.*)::json)
)
FROM (SELECT ST_Transform(geom, 4326) from res
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
}'), 3035), ST_Centroid(res.geom))) as t(geom);