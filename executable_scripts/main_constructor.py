"""
This script creates a pylovo database and fills with raw data from referenced files.
Do not use SyngridDatabaseConstructor unless you want to create a new database.
"""

from raw_data.municipal_register.join_regiostar_gemeindeverz import create_municipal_register
from pylovo.SyngridDatabaseConstructor import SyngridDatabaseConstructor

### Create constructor class
sgc = SyngridDatabaseConstructor()
### Creata database with predefined table structure
sgc.create_table(table_name="all")
### Add defined csv raw data from CSV_FILE_LIST to the database
sgc.csv_to_db()
### Add transformer data from geojson to the database
sgc.transformers_to_db(sgc)
### Create table with data from osm
sgc.create_public_2po_table()
### Transform these data into our ways table
sgc.ways_to_db()
### Add additional required sql functions to the database
sgc.dump_functions()
### Create table with entries of all German municipalities and cities
create_municipal_register()
