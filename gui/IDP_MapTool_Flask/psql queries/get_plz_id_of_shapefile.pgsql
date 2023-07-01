SELECT "plz" 
FROM "postcode" 
WHERE ST_Intersects(ST_Transform("postcode".geom, 4326),
ST_GeomFromGeoJSON('{
    "type": "Polygon",
    "coordinates": [
        [
            [
                11.280212,
                47.995205
            ],
            [
                11.280212,
                48.255644
            ],
            [
                11.835022,
                48.255644
            ],
            [
                11.835022,
                47.995205
            ],
            [
                11.280212,
                47.995205
            ]
        ]
    ]
}'));


