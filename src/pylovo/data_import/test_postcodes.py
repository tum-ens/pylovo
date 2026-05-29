"""
Load polygon geometries into the pylovo database for testing purposes.
"""

from pylovo.database.database_client import DatabaseClient
from pylovo.config_loader import DBNAME, HOST, PORT, TARGET_EPSG

# =============================================================================
# TEST POSTCODE DATA CONFIGURATION
# =============================================================================
# Define test postcode data with 4 coordinates forming polygons
TEST_POSTCODES = [
    {
        'postcode_id': 99999,
        'plz': 99999,
        'allocated_plz': 91301,  # Add the PlZ region within which the test postcode is located
        'note': 'Forchheim Test - Buckenhofen',
        'qkm': 1.0,
        'population': 1000,
        'coordinates': [
            (11.0278000, 49.7360000),
            (11.0530000, 49.7360000),
            (11.0530000, 49.7090000),
            (11.0278000, 49.7090000),
            (11.0278000, 49.7360000)  # Close the polygon
        ]
    },
    {
        'postcode_id': 99998,
        'plz': 99998,
        'allocated_plz': 91301,
        'note': 'Forchheim Test - Serlbach',
        'qkm': 1.0,
        'population': 1000,
        'coordinates': [
            (11.089368, 49.736172),
            (11.100773, 49.736172),
            (11.100773, 49.73191),
            (11.089368, 49.73191),
            (11.089368, 49.736172)  # Close the polygon
        ]
    },
]


def load_test_postcodes():
    """
    Load test postcode data into the pylovo database.

    This function creates test postcode entries similar to the SQL file
    add_testing_postcodes.sql, but using Python instead of direct SQL execution.

    Returns:
        bool: True if successful, False otherwise
    """

    # Initialize database connection
    try:
        dbc = DatabaseClient()
        print("✅ Connected to database successfully")
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        return False

    try:
        with dbc.conn.cursor() as cur:
            for postcode in TEST_POSTCODES:
                # Create the polygon WKT string
                coords_str = ', '.join([f"{lon} {lat}" for lon, lat in postcode['coordinates']])
                polygon_wkt = f"POLYGON(({coords_str}))"

                # Insert the postcode data
                insert_query = f"""
                INSERT INTO pylovo.postcode (postcode_id, plz, allocated_plz, note, qkm, population, geom)
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    ST_Transform(
                        ST_SetSRID(
                            ST_Multi(
                                ST_GeomFromText(%s)
                            ),
                            4326
                        ),
                        {TARGET_EPSG}
                    )
                )
                ON CONFLICT (plz) DO UPDATE SET
                    allocated_plz = EXCLUDED.allocated_plz,
                    note = EXCLUDED.note,
                    qkm = EXCLUDED.qkm,
                    population = EXCLUDED.population,
                    geom = EXCLUDED.geom
                """

                cur.execute(insert_query, (
                    postcode['postcode_id'],
                    postcode['plz'],
                    postcode['allocated_plz'],
                    postcode['note'],
                    postcode['qkm'],
                    postcode['population'],
                    polygon_wkt
                ))

                print(f"✅ Inserted test postcode {postcode['plz']}: {postcode['note']}")

            # Commit the transaction
            dbc.conn.commit()
            print("✅ All test postcodes loaded successfully!")

    except Exception as e:
        print(f"❌ Error loading test postcodes: {e}")
        dbc.conn.rollback()
        return False

    finally:
        # Close the database connection
        dbc.cur.close()
        dbc.conn.close()
        print("🔌 Database connection closed")

    return True


def main():
    """Main function to run the test data loading."""
    import sys

    print("🚀 Starting test postcode data loading...")
    print("=" * 50)

    # Check if environment variables are set
    try:
        print(f"Database: {DBNAME}")
        print(f"Host: {HOST}")
        print(f"Port: {PORT}")
        print("Schema: pylovo")
        print("=" * 50)
    except Exception as e:
        print(f"❌ Configuration error: {e}")
        print("Make sure your .env file is properly configured.")
        sys.exit(1)

    success = load_test_postcodes()

    if success:
        print("=" * 50)
        print("🎉 Test data loading completed successfully!")
    else:
        print("=" * 50)
        print("💥 Test data loading failed!")
        sys.exit(1)


__all__ = ["load_test_postcodes", "TEST_POSTCODES"]

