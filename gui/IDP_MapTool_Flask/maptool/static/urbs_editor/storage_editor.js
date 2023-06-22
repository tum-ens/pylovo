//TODO: Commodity selection object in editor

var maptool_urbs_storage = function () {

    let StorageObject = {
        "storagePropertiesList": {},
        "storagePropertiesTemplate": {}
    }

    let container = document.getElementById('storage_confHOTContainer');
    let hot = new Handsontable(container, {
        data: [[]],
        rowHeaders: true,
        colHeaders: [],
        minRows: 1,
        minCols: 1,
        minSpareRows: 1,
        overflow: 'auto',
        licenseKey: 'non-commercial-and-evaluation'
        });

    /**
     * called from generate_urbs_setup_editor.js during setup of the urbs setup editor window
     * @returns Promise signalling that the fetch operation concluded
     */
    function fetchProfiles() {
        return fetch('urbs/storage_profiles')
        .then(function (response) {
            return response.json();
        }).then(function (data) {
            let storage = JSON.parse(data["sto_prop"])
            let storagePropertyJSONTemplate = {};
            
            for (idx in storage) {
                if(idx != 'name') {
                    storagePropertyJSONTemplate[idx] = '';
                }
            }

            StorageObject.storagePropertiesTemplate = storagePropertyJSONTemplate;

            let i = 0;
            for (idx in storage['name']) {
                let storagePropertyJSON = JSON.parse(JSON.stringify(storagePropertyJSONTemplate));
                for (feature_idx in storage) {
                    if (feature_idx != 'name') {
                        storagePropertyJSON[feature_idx] = storage[feature_idx][i];
                    }
                }
                let name = storage['name'][idx];
                StorageObject.storagePropertiesList[name] = storagePropertyJSON;
                i++;
            }
            maptool_urbs_process.populateProcessEditorList('storage', Object.keys(StorageObject.storagePropertiesList));

            createSto_Conf_Editor();
        });
    }

    function fillStorageEditorCommodityList(commodities) {
        let commoditySelect = document.getElementById('storageFormDiv').querySelector('#commodity');
        for (idx in commodities) {
            let option = document.createElement('option');
            option.text = commodities[idx];
            option.value = commodities[idx];
            commoditySelect.append(option);
        }
    }

    function createSto_Conf_Editor() {
        var data = [];
        var headers_c = ['urbs_name'];
        var headers_p = [];
        var placeholders = []

        for (storage in StorageObject.storagePropertiesList) {
            headers_c.push(storage + '.c');
            headers_p.push(storage + '.p');
            placeholders.push('0');
            placeholders.push('0');
        }

        for (bus in maptool_urbs_buildings.BuildingsObject.busWithLoadList) {
            data.push([maptool_urbs_buildings.BuildingsObject.busWithLoadList[bus].feature.properties.name].concat(placeholders));
        }

        hot.loadData(data);

        hot.updateSettings({
            cells(row, col, prop) {
                const cellProperties = {};
            
                if (col == 0) {
                  cellProperties.readOnly = true;
            
                } else {
                  cellProperties.editor = 'numeric';
                }
            
                return cellProperties;
              }, 
              colHeaders: headers_c.concat(headers_p)
        })
    }

    return {
        StorageObject: StorageObject,
        hot: hot,
        fetchProfiles: fetchProfiles,
        fillStorageEditorCommodityList: fillStorageEditorCommodityList,
        createSto_Conf_Editor: createSto_Conf_Editor
    };
}();