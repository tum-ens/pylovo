var maptool_return_urbs =  function() {
    function returnUrbsSetup() {
        const buildings = returnUrbsSetup_Buildings();
        const demand = returnUrbsSetup_DemandConf();
        //const bsp = returnUrbsSetup_BuySellPrice();
        const transmission = returnUrbsSetup_Transmissions();
        const global = returnUrbsSetup_Global();
        const commodity = returnUrbsSetup_Commodity();
        const process = returnUrbsSetup_Processes();
        const storage = returnUrbsSetup_Storage();
        const supim = returnUrbsSetup_SupIm();
        const timevareff = returnUrbsSetup_Timevareff();
        
        //makes sure we have returned all data before telling the backend to run the pdp2urbs function and switching to the urbs_results window
        Promise.all([buildings, demand, transmission, global, commodity,process, storage, supim, timevareff]).then((res) => {
            const pdp2urbs = runPdp2Urbs();
            Promise.all([pdp2urbs]).then(res => {
                document.location.href = "/urbs_results";
            })
        });
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_DemandConf() {
        let demand_json = {};
        for(idx in maptool_urbs_buildings.BuildingsObject.busWithLoadList) {
            demand_json[maptool_urbs_buildings.BuildingsObject.busWithLoadList[idx].feature.properties.name] = maptool_urbs_demand.DemandObject.bus_demands[idx];
        }
        return postData("http://127.0.0.1:5000/urbs/demand_csv_setup", demand_json);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_Buildings() {
        let buildings_json = JSON.stringify(maptool_urbs_buildings.BuildingsObject.buildingsPropertiesList);
        return postData("http://127.0.0.1:5000/urbs/buildings_csv_setup", buildings_json);
    }
    //DONE
    function returnUrbsSetup_BuySellPrice() {
        //postData("http://127.0.0.1:5000/urbs/urbs_setup", jsonData);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_Transmissions() {
        let transmission_json = {};

        transmission_json['cable_data'] = maptool_urbs_trans.TransmissionObject.cable_dataList;

        transmission_json['trafo_data'] = maptool_urbs_trans.TransmissionObject.trafo_dataList;
        console.log(maptool_urbs_trans.TransmissionObject.trafo_dataList);

        const voltageFormData = new FormData(document.getElementById('transmission_voltage_limitsForm'));
        const voltageProps = Object.fromEntries(voltageFormData);
        transmission_json['voltage_limits'] = voltageProps;
        return postData("http://127.0.0.1:5000/urbs/transmission_csv_setup", transmission_json);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_Global() {
        let global_json = {};

        let global_checkboxes = ['assumelowq', 'excel', 'flexible', 'grid_curtailment', 'lp', 'retrofit', 'tsam', 'tsam_season', 'uncoordinated']

        const globalFormData = new FormData(document.getElementById('globalForm'));
        let globalProps = Object.fromEntries(globalFormData);

        global_checkboxes.forEach(checkbox => {
            if(!(checkbox in globalProps)) {
                globalProps[checkbox] = 0;
            }
            else {
                globalProps[checkbox] = 1;
            }
        }) 

        global_json = JSON.stringify(globalProps);
        return postData("http://127.0.0.1:5000/urbs/global_csv_setup", global_json);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_Commodity() {
        let commodity_json = JSON.stringify(maptool_urbs_commodity.CommodityObject.commodityPropertiesList);
        postData("http://127.0.0.1:5000/urbs/commodity_csv_setup", commodity_json);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_Processes() {
        let process_json = {};
        process_json['pro_prop'] = JSON.stringify(maptool_urbs_process.ProcessObject.pro_propList);
        process_json['pro_com_prop'] = JSON.stringify(maptool_urbs_process.ProcessObject.pro_com_propList);
        process_json['pro_conf'] = JSON.stringify({'columns': maptool_urbs_process.hot.getColHeader(),
                                                    'data': maptool_urbs_process.hot.getData()
                                                });
        console.log(maptool_urbs_process.ProcessObject.pro_com_propList);
        return postData("http://127.0.0.1:5000/urbs/process_csv_setup", process_json);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_Storage() {
        let storage_json = {};

        storage_json['sto_prop'] = JSON.stringify(maptool_urbs_storage.StorageObject.storagePropertiesList);
        storage_json['sto_conf'] = JSON.stringify({'columns': maptool_urbs_storage.hot.getColHeader(),
                                                    'data': maptool_urbs_storage.hot.getData()
                                                });

        console.log(maptool_urbs_storage.StorageObject.storagePropertiesList)
        return postData("http://127.0.0.1:5000/urbs/storage_csv_setup", storage_json);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_SupIm() {
        let supim_json = {};
        for(idx in maptool_urbs_buildings.BuildingsObject.busWithLoadList) {
            supim_json[maptool_urbs_buildings.BuildingsObject.busWithLoadList[idx].feature.properties.name] = maptool_urbs_supim.SupimObject.bus_supim[idx];
        }
        return postData("http://127.0.0.1:5000/urbs/supim_csv_setup", supim_json);
    }
    //DONE BACKEND_DONE
    function returnUrbsSetup_Timevareff() {
        let timevareff_json = {};

        for(idx in maptool_urbs_buildings.BuildingsObject.busWithLoadList) {
            timevareff_json[maptool_urbs_buildings.BuildingsObject.busWithLoadList[idx].feature.properties.name] = maptool_urbs_timevareff.TimevareffObject.bus_timevareff[idx];
            console.log(timevareff_json);
        }
        return postData("http://127.0.0.1:5000/urbs/timevareff_csv_setup", timevareff_json);
    }

    /**
     * function telling the backend to run the pandapower2urbs script once all relevant data has been returned
     * @returns Promise signifying that the data was correctly received and processed in the backend
     */
    function runPdp2Urbs() {
        console.log("Starting pdp2urbs");
        const promise = fetch("http://127.0.0.1:5000/urbs/pdp2Urbs");
        return promise;
    }

    /**
     * 
     * @param {string} url      address we send data to on the backend side
     * @param {json} jsonData   data we want to return to the backend
     * @returns Promise signifying that the data was correctly received and processed in the backend
     */
    async function postData(url, jsonData) {
        const promise = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-type': 'application/json'},
            body: JSON.stringify(jsonData)
        })

        return promise;
    }

    return {
        returnUrbsSetup: returnUrbsSetup
    }
}();