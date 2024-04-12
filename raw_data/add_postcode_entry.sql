INSERT INTO public.postcode (gid,
                             plz,
                             note,
                             qkm,
                             einwohner,
                             geom)
VALUES (9994,
        '10004',
        'test review',
        9999,
        9999,
        ST_Transform(ST_SetSRID(
                             ST_Multi(ST_GeomFromText('POLYGON((11.0428689 49.7256704,11.0484700 49.7256662,11.0482742 49.7236382,11.0426169 49.7239382,11.0428689 49.7256704))')),
                             4326), 3035)
           -- this needs to be a closed polygon
       );