# Classification of Grids

The goal is to find representative grids for the LV-grids in Bayern / Germany.

The following steps are performed:

# 1. Join Regiostar and Gemeindeverzeichnis

* Regiostar: Import of the "Regionalstatistische Raumtypologie" classification of municipalities by the
  Bundesministerium für Digitales und Verkehr
    * for some exploration of the dataset see `analyse_regiostar_bayern.ipynb` and `analyse_regiostar_germany.ipynb`
* Gemeindeverzeichnis: Import and create a table that matches AGS (Amtlicher Gemeindeschlüssel) with PLZ (Postleitzahl),
  there are two main ways:
    * (I) Repository by Wissenschaftszentrum Berlin für Sozialforschung / WZB Berlin Social Science Center [githubWZB]
    * tool to map AGS (Amtliche Gemeindeschlüssel) with PLZ
    * some functionalities are demonstrated in the `einlesen_notebook.ipynb`
    * data sources:
        * PLZ Name table from [opendatasoft]
        * list_plz: [excel-karte]
        * AGS - PLZ table with official data by DESTATIS [destatis]
    * (II) table with plz specific data [suche-postleitzahl]
    * two tables are obtained that map plz with regiostar: `regiostar_plz_destatis.csv` and `regiostar_plz.csv`
* the information is merged and written in `write_regiostar_plz.py` and `regiostar_add_plz.py`
* `regiostar_plz.csv` is more complete and concides with the official data of destatis and is consequently used
  for further functions
* If you make any changes in the Regiostar of gemeindeverzeichnis data run `join_regiostar_gemeindeverzeichnis.py`

# 2. Preselection of PLZ areas for clustering:

* the algorithm that draws samples from the total number of 8.181 plz in Germany based on the Regiostar classes and
  the population density is demonstrated in `sampling_algorithm.ipynb`
    * The number of samples from each Regiostar class corresponds to the percentage of the class of the whole population
    * The samples within a class represent the population density distribution of the class
    * The sampling algorithm is performed in `sample.py`
    * The results are written to `regiostar_samples.csv` for Germany and `regiostar_samples_bayern.csv`
    * The results are analysed and visualised in `analyse_sampling_results.ipynb`
      and `analyse_sampling_results_bayern.ipynb`

# 3. Grid generation:

* The Building data for the samples `regiostar_samples_bayern.csv` need to be extracted:
    * In the raw_data subdirectory: insert the 7 regions in Bayern contained in the `Bayern_Building_Database.zip`
    * `import_builing_data.py` writes a list of dictionaries `OGR_FILE_LIST`
        * this list is imported by `config_data.py` in syngrid directory
        * when running the `sgc.ways_to_db()` in main_constructor the shape files are read from the list and added to
          the syngrid_db
        * be careful not to add building shape files multiple times
        * the added shape files can be found in the text files:`[]_added_files_list.txt` and so on in different formats
* The function in `grid_generation_for_classification.py` allows to generate all grids that were drawn as samples
  sequentially

# 4. Clustering

* Classification: classification of electrical grids
    * parameters for clustering
    * that classify and differentiate LV-grids

# 5. Generation of Representative Grids

[opendatasoft]: https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets/georef-germany-postleitzahl/exports/csv?lang=en&timezone=Europe%2FBerlin&use_labels=true&delimiter=%3B

[githubWZB]: https://github.com/WZBSocialScienceCenter/gemeindeverzeichnis

[excel-karte]: https://excel-karte.de/wp-content/uploads/2016/12/Liste-der-PLZ-in-Excel-Karte-Deutschland-Postleitzahlen.xlsx

[destatis]: https://www.destatis.de/DE/Themen/Laender-Regionen/Regionales/Gemeindeverzeichnis/Administrativ/Archiv/GV100ADQ/GV100AD3105.html

[suche-postleitzahl]: https://www.suche-postleitzahl.org/downloads
