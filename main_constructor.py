"""
This script creates a syngrid database and fills with raw data from referenced files. Do not use SyngridDatabaseConstructor
unless you want to create a new database.
"""
from syngrid.SyngridDatabaseConstructor import SyngridDatabaseConstructor


sgc = SyngridDatabaseConstructor()
#sgc.create_functions_from_dump()
#sgc.create_table(table_name="all")
#sgc.csv_to_db()
#sgc.ogr_to_db()
sgc.ways_to_db()
sgc.con.close()
