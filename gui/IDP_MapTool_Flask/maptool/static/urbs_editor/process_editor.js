//TODO: On process creation, make sure that ratio, ratio min are saved if we choose a new commodity

/**
 * pro_propList: dict containing all processes and their properties
 *               has structure  {process_name_1: 
 *                                  {
 *                                  property_1: value,
 *                                  property_2: value,
 *                                  ...
 *                                  },
 *                               process_name_2: 
 *                                  {
 *                                  ...
 *                                  }
 *                              }
 * pro_com_propList: dict containing all process commodities and their properties
 *                   has structure {process_name_1: 
 *                                       {
 *                                       in: 
 *                                          {
 *                                           commodity_name_1: 
 *                                               {
 *                                               property_1 : value, 
 *                                               ...
 *                                               }
 *                                           },
 *                                       out: 
 *                                           {
 *                                           commodity_name_2: 
 *                                               {
 *                                               property_2 : value, 
 *                                               ...
 *                                               }
 *                                           }
 *                                       },
 *                                   process_name_2 : 
 *                                       {
 *                                       ...
 *                                       }
 *                                   }
 */



var maptool_urbs_process = function() {
    let ProcessObject = {
        "pro_propList": {},
        "pro_com_propList": {},
        "pro_propTemplate": {},             //template dict for entries of pro_propList
        "pro_com_propTemplate": {},         //template dict for entries of pro_com_propList
        "pro_conf_default_vals": {}
    }

    let container = document.getElementById('process_confHOTContainer');
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
     * retrieves pre-existing process property data from the pandapower2urbs templates in the backend
     */
    function fetchProcessProfiles() {
        fetch('urbs/process_profiles')
        .then(function (response) {
            return response.json();
        }).then(function (process_data) {
            let processes = JSON.parse(process_data["pro_prop"]);
            let process_commodities = JSON.parse(process_data["pro_com_prop"]);
            ProcessObject.pro_conf_default_vals = maptool_urbs_setup.getUrbsPropertiesJSON()["process"]["pro_conf"];
            console.log(maptool_urbs_setup.getUrbsPropertiesJSON())
            createProcessJSONTemplates(processes, process_commodities);

            let i = 0;
            for (idx in processes['name']) {
                let processPropertyJSON = JSON.parse(JSON.stringify(ProcessObject.pro_propTemplate));
                for (feature_idx in processes) {
                    if (feature_idx != 'name') {
                        processPropertyJSON[feature_idx] = processes[feature_idx][i];
                    }
                }
                ProcessObject.pro_propList[processes['name'][idx]] = processPropertyJSON;
                ProcessObject.pro_com_propList[processes['name'][idx]] = {'In': {}, 'Out': {}};
                i++;
            }   

            for (idx in process_commodities['Commodity']) {
                let pro_comPropertyJSON = JSON.parse(JSON.stringify(ProcessObject.pro_com_propTemplate));
                pro_comPropertyJSON['ratio'] = process_commodities['ratio'][idx];
                pro_comPropertyJSON['ratio-min'] = process_commodities['ratio-min'][idx];
                let process = process_commodities['Process'][idx];
                let direction = process_commodities["Direction"][idx];
                ProcessObject.pro_com_propList[process][direction][process_commodities['Commodity'][idx]] = pro_comPropertyJSON;
            }
            populateProcessEditorList('pro_prop', Object.keys(ProcessObject.pro_propList));
            createPro_Conf_Editor();
        });
    }

    /**
     * creates dict entry templates to use if we want to add new processes or new process commodities
     * 
     * @param {dict} processes              dict with process feature:value key:value pairs
     * @param {dict} process_commodities    dict with pro_com feature:value key:value pairs
     */
    function createProcessJSONTemplates(processes, process_commodities) {
        let processPropertyJSONTemplate = {};
        let pro_com_propJSONTemplate = {};
        
        for (key in processes) {
            //name should not be an editable input value in the editor, since we use name as key in the pro_prop dict
            if(key != 'name') {
                processPropertyJSONTemplate[key] = '';
            }
        }

        for (key in process_commodities) {
            //direction and commodity should not be editable inputs in the editor, since we use them as keys in the pro_com_prop dict
            if(key != 'Direction' && key != 'Commodity' && key != 'Process') {
                pro_com_propJSONTemplate[key] = '';
            }
        }
        ProcessObject.pro_propTemplate = processPropertyJSONTemplate;
        ProcessObject.pro_com_propTemplate = pro_com_propJSONTemplate;
    }

    /**
     * we create options with text based on the keys of a dict and attach them to a html select
     * 
     * @param {string} htmlListName id of the html select element we want to add options to
     * @param {dict} listEntries    dict containing data for all options we want to add
     */
    function populateProcessEditorList(htmlListName, listEntries) {
        let processList = document.getElementById(htmlListName + "Select");
        for (entry in listEntries) {
            var option = document.createElement("option");
            option.text = listEntries[entry];
            processList.add(option);
        }
    }
    /**
     * called at runtime to make sure only the correct elements are displayed in the pro_com_prop list. All options whose names are keys in target_properties
     * are made visible, all others are hidden
     * The pro_com_propSelect element technically contains options for all commodities added to all processes, but we hide all options not added to the
     * currently selected process
     * 
     * @param {dict} target_properties 
     */
    function fillSecondaryEditorList(target_properties) {
        let secondaryFeatureSelect = document.getElementById('pro_com_propSelect');
        for (let i = 0; i < secondaryFeatureSelect.options.length; i++) {
            //In the GUI commodities have " in" or " out" added at the end, so we need to remove these parts of the string before checking if the commodity
            //name is included as a key
            console.log(target_properties["In"])
            console.log(secondaryFeatureSelect.options[i].value)
            if (Object.keys(target_properties["In"]).includes((secondaryFeatureSelect.options[i].value).slice(0, -3)) || 
                Object.keys(target_properties["Out"]).includes((secondaryFeatureSelect.options[i].value).slice(0, -4)) ) {
                secondaryFeatureSelect.options[i].hidden = false;
            }
            else {
                secondaryFeatureSelect.options[i].hidden = true;
            }
        }
    }

    /**
     * @param {bool} isCommodity determines whether the process creation dialogue or the pro_com_prop creation dialogue is opened
     */
    function openNewProcessForm(isCommodity) {
        let form = (isCommodity) ? document.getElementById("urbsProcessCommodityPopupForm") :  document.getElementById("urbsProcessPopupForm");
        form.style.display = "block";
    }

    /**
     * onclick button for the forms' cancel button, closes dialogue window and resets all input fields
     * 
     * @param {bool} isCommodity 
     */
    function closeNewProcessForm(isCommodity) {
        let form = (isCommodity) ? document.getElementById("urbsProcessCommodityPopupForm") :  document.getElementById("urbsProcessPopupForm");
        form.style.display = "none";
        document.getElementById('pro_propCommSelect').selectedIndex = 0;
        document.getElementById("newProcessTextInput").value = '';
        document.getElementById("newProcessCommTextInput").value = '';
        document.getElementById("newProcessCreateButton").disabled = true;
        document.getElementById("newProcessCommDiv").classList.add('hidden');
    }

    /**
     * onchange function for the input fields of the process creation popup form
     * makes sure "Create Process" button is disabled until all inputs are set correctly 
     * 
     * @param {HTML_select_element} sel the commodity select element
     */
    function processFormCommoditySelection(sel) {
        let processNameFlag = (document.getElementById("newProcessTextInput").value.length != 0);
        document.getElementById("newProcessCreateButton").disabled = true;

        if (sel.value == 'newCommodity') {
            document.getElementById("newProcessCreateButton").disabled = true;
            document.getElementById("newProcessCommDiv").classList.remove('hidden');
            if(document.getElementById("newProcessCommTextInput").value != '' && processNameFlag) {
                document.getElementById("newProcessCreateButton").disabled = false;
            }
        }
        else if(sel.value != 'none'){
            if (processNameFlag) {
                document.getElementById("newProcessCreateButton").disabled = false;
            }
            document.getElementById("newProcessCommDiv").classList.add('hidden');

        }
    }

    /**
     * onchange function for the input fields of the popup form for adding a commodity to a process
     * makes sure "Create Process" button is disabled until all inputs are set correctly 
     * 
     * @param {HTML_select_element} sel the commodity select element
     */
    function processAddCommoditySelection(sel) {
        
        if (sel.value == 'newCommodity') {
            document.getElementById("ProcessAddCreateButton").disabled = true;
            document.getElementById("ProcessAddCommDiv").classList.remove('hidden');
            if(document.getElementById("ProcessAddCommTextInput").value != '') {
                document.getElementById("ProcessAddCreateButton").disabled = false;
            }
        }
        else if(sel.value != 'none'){
            document.getElementById("ProcessAddCreateButton").disabled = false;
            document.getElementById("ProcessAddCommDiv").classList.add('hidden');

        }
    }

    /**
    * Function makes secondary feature window visible and fills all input fields with the saved values, if any exist.
    * At the moment the process editor is the only one with a secondary editor, namely the pro_com_prop editor
    * 
    * @param {HTML_select_element} sel      gets passed as "this" reference when the onchange method for the select element is called, needed to 
    *                                       retrieve the currently selected secondary feature
    * @param {string} secondaryFeatureName  key for relevant html elements 
    */
    function openSecondaryProcessEditor(sel, secondaryFeatureName) {
        document.getElementById(secondaryFeatureName + 'Editor').style.display='block';
        
        let key = document.getElementById('pro_propSelect').value;
    
        let inOrOutFlag = (sel.value.slice(-3) === ' In') ? true : false;

        let target_properties = maptool_urbs_process.ProcessObject.pro_com_propList[key][(inOrOutFlag) ? "In" : "Out"][(inOrOutFlag) ? sel.value.slice(0, -3) : sel.value.slice(0, -4)];
        let editor_form = document.getElementById(secondaryFeatureName + 'Form');
        let editor_elems = editor_form.children[0].children;

        console.log(editor_elems);

        for (let i = 0; i < editor_elems.length; i++) {
            if (editor_elems[i].nodeName == 'INPUT') {
                if(target_properties[editor_elems[i].name] != null) {
                    editor_elems[i].value = target_properties[editor_elems[i].name];
                    console.log(editor_elems[i].value, target_properties[editor_elems[i].name])
                }
                else {
                    editor_elems[i].value = '';
                }
            }
        }
    }

    /**
     * TODO: Split commodity attachment into own function
     * 
     * @param {bool} isCommodity 
     */
    function createNewProcessPropertyOrCommodity(isCommodity) {
        if(isCommodity) {
            let inOrOut = (document.getElementById('ProcessAddCommCheckboxIn').checked) ? "In" : "Out";
            //we either create a new commodity or simply attach an existing one to a process
            if (document.getElementById("pro_propAddCommSelect").value == 'newCommodity') {
                createNewProcessCommodity(document.getElementById("pro_propSelect").value, document.getElementById("ProcessAddCommTextInput").value, inOrOut);
            }
            else {
                ProcessObject.pro_com_propList[document.getElementById("pro_propSelect").value][inOrOut][document.getElementById("pro_propAddCommSelect").value] = JSON.parse(JSON.stringify(ProcessObject.pro_com_propTemplate));
                
                //we only add pro_com_prop options to the select once and then make them visible depending on if the associated process has one attached
                //if the option has not been created, we dp so here
                const select = document.querySelector("#pro_com_propSelect");
                const optionLabels = Array.from(select.options).map((opt) => opt.value);
                const hasOption = optionLabels.includes(document.getElementById("pro_propAddCommSelect").value + " " + inOrOut);
                
                if(!hasOption) {
                    let option = document.createElement("option");
                    option.text = document.getElementById("pro_propAddCommSelect").value + " " + inOrOut;
                    option.value = document.getElementById("pro_propAddCommSelect").value + " " + inOrOut;
                    document.getElementById('pro_com_propSelect').add(option);
                }
            }
        }
        //creates new process and either creates a new commodity to attach or attaches a preexisting commodity
        else {
            let inOrOut = (document.getElementById('newProcessCommCheckboxIn').checked) ? "In" : "Out";
            createNewProcessProperty(document.getElementById("newProcessTextInput").value);
            if (document.getElementById("pro_propCommSelect").value == 'newCommodity') {
                createNewProcessCommodity(document.getElementById("newProcessTextInput").value, document.getElementById("newProcessCommTextInput").value, inOrOut);
            }
            else {
                ProcessObject.pro_com_propList[document.getElementById("newProcessTextInput").value] = {"In": {}, "Out": {}};
                ProcessObject.pro_com_propList[document.getElementById("newProcessTextInput").value][inOrOut][document.getElementById("pro_propCommSelect").value] =JSON.parse(JSON.stringify(ProcessObject.pro_com_propTemplate));
                
                const select = document.querySelector("#pro_com_propSelect");
                const optionLabels = Array.from(select.options).map((opt) => opt.value);
                const hasOption = optionLabels.includes(document.getElementById("pro_propAddCommSelect").value + " " + inOrOut);
                
                if(!hasOption) {
                    let option = document.createElement("option");
                    option.text = document.getElementById("pro_propCommSelect").value + " " + inOrOut;
                    option.value = document.getElementById("pro_propCommSelect").value + " " + inOrOut;
                    document.getElementById('pro_com_propSelect').add(option);
                }
            }
        }
        closeNewProcessForm(isCommodity)
    }

    /**
     * creates a new process and adds it to the ProcessObject & html pro_prop select
     * 
     * @param {string} name 
     */
    function createNewProcessProperty(name) {
        console.log("new Process")
        let processList = document.getElementById("pro_propSelect");
        let option = document.createElement("option");
        option.text = name;
        processList.add(option);

        let processPropertyJSON = JSON.parse(JSON.stringify(ProcessObject.pro_propTemplate));
        ProcessObject.pro_propList[name] = processPropertyJSON;
        
        //we insert a new column into the commodity table
        hot.alter('insert_col', hot.countCols(), 1)
        hot.headers[hot.headers.length - 1] = name;

    }   


    /**
     * we must add a newly defined process commodity to the list of commodities, the process_config table and the editor window of the process the commodity is associated with
     * 
     * @param {string} pro_name 
     * @param {string} com_name 
     * @param {string} inOrOut 
     */
    function createNewProcessCommodity(pro_name, com_name, inOrOut) {
        console.log("new Process Commodity")

        //we grab the commodity list and add a new option. We also add a blank entry to the Commodity Object List
        let commodityList = document.getElementById("commoditySelect");
        let option = document.createElement("option");
        option.text = com_name;
        option.value = com_name;
        commodityList.add(option);

        maptool_urbs_commodity.addCommToProcessCreationFormList(com_name);
        maptool_urbs_commodity.addCommToStorageComList(com_name);

        maptool_urbs_commodity.CommodityObject.commodityPropertiesList[com_name] = JSON.parse(JSON.stringify(maptool_urbs_commodity.CommodityObject.commodityPropertiesTemplate));
        
        const select = document.querySelector("#pro_com_propSelect");
        const optionLabels = Array.from(select.options).map((opt) => opt.value);
        const hasOption = optionLabels.includes(com_name + " " + inOrOut);

        if (!hasOption) {
            let pro_com_propOption = document.createElement("option");
            pro_com_propOption.text = com_name + " " + inOrOut;
            pro_com_propOption.value = com_name + " " + inOrOut;

            document.getElementById('pro_com_propSelect').add(pro_com_propOption);
        }

        let pro_propCommOption = document.createElement("option");
        pro_propCommOption.text = com_name;
        pro_propCommOption.value = com_name;
        document.getElementById('pro_propCommSelect').add(pro_propCommOption);
        if (!Object.keys(ProcessObject.pro_com_propList).includes(pro_name)) {
            ProcessObject.pro_com_propList[pro_name] = {"In": {}, "Out": {}};
        }
        ProcessObject.pro_com_propList[pro_name][inOrOut][com_name] = JSON.parse(JSON.stringify(ProcessObject.pro_com_propTemplate));
    }

    /**
     * saves edited feature in the ProcessObject
     * 
     * @param {event_target_object} target 
     * @param {bool} isPro_com_prop 
     */
    function writeBackProcessFeatures(target, isPro_com_prop) {
        let idxInFeatureList = document.getElementById("pro_propSelect").selectedIndex;
        let keyInFeatureList = document.getElementById("pro_propSelect").options[idxInFeatureList].text;
        
        if(isPro_com_prop) {
            let pro_com_propKey = document.getElementById("pro_com_propSelect").value;
            let inOrOutFlag = (pro_com_propKey.slice(-3) === ' In') ? true : false;

            let selectedElement = ProcessObject.pro_com_propList[keyInFeatureList][(inOrOutFlag) ? "In" : "Out"][(inOrOutFlag) ? pro_com_propKey.slice(0, -3) : pro_com_propKey.slice(0, -4)];
            selectedElement[target.name] = target.value;
        }
        else {
            let selectedElement = ProcessObject.pro_propList[keyInFeatureList];
            selectedElement[target.name] = target.value;
        }
    }

    /**
     * sets up pro_conf handsontable table in the GUI
     */
    function createPro_Conf_Editor() {
        var data = [];
        var headers = ['urbs_name'];
        var placeholders = []
        var trafo_placeholders = []
        var busbar_placeholders = []

        for (processName in ProcessObject.pro_propList) {
            headers.push(processName);
            if(ProcessObject.pro_conf_default_vals[processName] != undefined) {
                trafo_placeholders.push(ProcessObject.pro_conf_default_vals[processName].default_val_trafo);
                busbar_placeholders.push(ProcessObject.pro_conf_default_vals[processName].default_val_main_bus);
                placeholders.push(ProcessObject.pro_conf_default_vals[processName].default_val_other);
            }
            else {
                trafo_placeholders.push('');
                busbar_placeholders.push('');
                placeholders.push('');
            }


        }
        data.push(["Trafostation_OS"].concat(trafo_placeholders));
        data.push(["main_busbar"].concat(busbar_placeholders));
        for (bus in maptool_urbs_buildings.BuildingsObject.busWithLoadList) {
            data.push([maptool_urbs_buildings.BuildingsObject.busWithLoadList[bus].feature.properties.name].concat(placeholders));
        }
        hot.loadData(data);
        hot.headers = headers;

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
              colHeaders: headers 
        })
    }
    
    return {
        ProcessObject: ProcessObject,
        hot: hot,
        fetchProcessProfiles: fetchProcessProfiles,
        populateProcessEditorList: populateProcessEditorList,
        fillSecondaryEditorList: fillSecondaryEditorList,
        openNewProcessForm: openNewProcessForm,
        closeNewProcessForm: closeNewProcessForm,
        processFormCommoditySelection: processFormCommoditySelection,
        processAddCommoditySelection: processAddCommoditySelection,
        createNewProcessPropertyOrCommodity: createNewProcessPropertyOrCommodity,
        writeBackProcessFeatures: writeBackProcessFeatures,
        openSecondaryProcessEditor: openSecondaryProcessEditor,
        createPro_Conf_Editor: createPro_Conf_Editor
    }
}();
