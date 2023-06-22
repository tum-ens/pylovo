var maptool_urbs_setup = function() {
    let UrbsPropertiesJSON = {};
    
    //help variable that stores a reference to the last selected leaflet circlemarker
    //Needed so the marker style can be readjusted once another marker is selected
    let clicked;

    //function that fetches the urbsPropertyJSON file stored in the backend. The file defines all inputs for all features as well as tooltips, types and default values
    function GetUrbsSetupProperties () {
        return fetch('urbs/urbs_setup_properties')
            .then(function (response) {
                return response.json();
            }).then(function (urbs_setup_properties) { 
                UrbsPropertiesJSON = urbs_setup_properties;
            });
    }
    
    /**
     * main aggregate function for editor generation: All the different preparatory functions for each editor component are called here
     * after the network data is retrieved from Flask
     */
    function SetupUrbsEditor() {    
        let fetchString = '/urbs/editableNetwork';
        
        fetch(fetchString)
        .then(function (response) {
            return response.json();
        }).then(function (ppdata) {
            
            var layers = L.PM.Utils.findLayers(map);
            layers.forEach((layer) =>{
                    layer.remove();
            });
            
            //create leaflet overlay of the network
            displayUrbsEditorNet(ppdata);

            //if an editor segment requires or wants to offer pregenerated profiles, they are fetched here
            //each editor component has their own fetch method, since different components have different ways of preparing their data
            //in each editor component, the editor list is also filled
            //TODO: These methods may be generalized into a single generic function
            maptool_urbs_demand.fetchDemandProfiles().then((res) => {
                populateUrbsEditorLoadBusLists('demand', 'busWithLoad');
            })
            maptool_urbs_trans.fetchTransmissionProfiles().then((res) =>{
                maptool_urbs_trans.populateTransmissionEditorList(UrbsPropertiesJSON);
                maptool_urbs_trans.prepareCableDataList(UrbsPropertiesJSON['transmission'], 'cable_data')
                populateUrbsEditor('transmission_cable_data', UrbsPropertiesJSON['transmission']['cable_data'], 'maptool_urbs_trans.writeBackTransmissionFeatures(this)');
                populateUrbsEditor('transmission_trafo_data', UrbsPropertiesJSON['transmission']['trafo_data'],'maptool_urbs_trans.writeBackTransmissionFeatures(this)');
                populateUrbsEditor('transmission_voltage_limits', UrbsPropertiesJSON['transmission']['voltage_limits'],'');
                maptool_urbs_trans.fillTrafoDataEditorIdSelect();
            })
            maptool_urbs_commodity.fetchProfiles();
            maptool_urbs_process.fetchProcessProfiles();
            maptool_urbs_storage.fetchProfiles().then ((res) =>{
                maptool_urbs_storage.fillStorageEditorCommodityList(Object.keys(maptool_urbs_commodity.CommodityObject.commodityPropertiesList))
            });
            maptool_urbs_supim.fetchSupimProfiles().then((res) => {
                populateUrbsEditorLoadBusLists('supim', 'busWithLoad');
            })
            maptool_urbs_timevareff.fetchFeatureProfiles().then((res) => {
                populateUrbsEditorLoadBusLists('timevareff', 'busWithLoad');
            })

            //several components set their parameters per bus. Here the respective feature lists are filled with busses that fulfill the criteria
            //to be editable, in this case meaning busses with one or more loads attached
            //TODO: the second parameter is a holdover from when editor list datastructures worked differently and should be removed, since it's not needed
            populateUrbsEditorLoadBusLists('buildings', 'busWithLoad');            
            maptool_urbs_buildings.prepareBuildingsObject(UrbsPropertiesJSON);

            //Here the content of each editor window is created. Each editor window is filled at runtime based on the predefined inputs in the UrbsPropertiesJSON
            //Changes to the JSON allow quick changes to editor makeup by adding or removing input fields
            populateUrbsEditor('buildings', UrbsPropertiesJSON['_buildings']['from_user_input'], 'maptool_urbs_buildings.writeBackEditedBuildingFeatures(this)');
            populateUrbsEditor('commodity', UrbsPropertiesJSON['commodity'],'maptool_urbs_commodity.writeBackCommodityFeatures(this)');
            maptool_urbs_commodity.createBuySellPriceEditor();
            populateUrbsEditor('global', UrbsPropertiesJSON['global'],'');
            populateUrbsEditor('pro_prop', UrbsPropertiesJSON['process']['pro_prop'],'maptool_urbs_process.writeBackProcessFeatures(this, false)');
            populateUrbsEditor('pro_com_prop', UrbsPropertiesJSON['process']['pro_com_prop'],'maptool_urbs_process.writeBackProcessFeatures(this, true)');
            populateEditableNetworkEditorSecondaryFeature('pro_prop', 'pro_com_prop')
            populateUrbsEditor('storage', UrbsPropertiesJSON['storage']['sto_prop'],'');

            //ensures that the feature buttons are made visible once the network visualization has concluded and the editors been filed
            tabcontent = document.getElementsByClassName("feature-editor__buttons-tab__tablinks");
            for (i = 0; i < tabcontent.length; i++) {
                tabcontent[i].style.display = "inline-flex";
            }
            
            //quick and dirty debug method: Should be removed. urbs editor tablinks should get their own css class instead
            for (i = 0; i < tabcontent.length; i++) {
                tabcontent[i].style.padding = "30px 15px";
            }

            //demand editor is set as initially opened editor here
            //all other editors that need to be open on page load to make sure that their echarts are loaded correctly are closed
            document.getElementById('demand').style.display = 'inline-block';
            document.getElementById('demandSelect').selectedIndex = 0;
            document.getElementById('demandEditor').style.display = 'none';
            document.getElementById('supimEditor').style.display = 'none';
            document.getElementById('timevareffEditor').style.display = 'none';
        });
    }
    
    //aggregate function for displaying the full network
    function displayUrbsEditorNet(ppdata) {
        addGeoJSONtoUrbsEditorMap(true, ppdata['line'], 'line');
        addGeoJSONtoUrbsEditorMap(false, ppdata['ext_grid'], 'ext_grid');
        addGeoJSONtoUrbsEditorMap(false, ppdata['bus'], 'bus');
        addGeoJSONtoUrbsEditorMap(true, ppdata['trafo'], 'trafo');
    }
    
    /**
     * creates a new geojson layer for leaflet map
     * distinguishes between line (for lines and trafos) and circle marker (for busses and ext_grids) formats
     * because line geojsons do not have the pointToLayer option
     * @param {bool}                isLines 
     * @param {GeoJSON Object}      input_geoJSON 
     * @param {string}              featureName 
     */
    function addGeoJSONtoUrbsEditorMap(isLines, input_geoJSON, featureName) {
        let newGeoJson;
        if (isLines) {
            newGeoJson = L.geoJSON(input_geoJSON, {
                snapIgnore:true,                                                  
                style: maptool_network_gen.NetworkObject.nonEditableStyles[0]   //should probably be set elsewhere
            }).addTo(map);
        }
        else {
            newGeoJson = L.geoJSON(input_geoJSON, {
                onEachFeature: function(feature, layer) {
                    if (featureName == 'bus') {
                        //we need all busses to be displayed, but we only want busses with loads to appear in our editor lists
                        if (Object.keys(feature.properties.load).length > 0) { 
                            maptool_urbs_buildings.BuildingsObject['busWithLoadList'].push(layer);  
                        }
                    }
                },
                pointToLayer: function (feature, latlng) {
                    var marker = L.circleMarker(latlng, maptool_network_gen.NetworkObject.nonEditableStyles[0]);
                    if(featureName == 'bus') {
                        //only busses with attached loads get an onclick listener
                        if (Object.keys(feature.properties.load).length > 0) {
                            marker.setStyle(maptool_network_gen.NetworkObject.busStyles[1]);
                            marker.on('click', function(e) {
                                //we fill all editors with attached buslists with the selected bus, even if they're not visible to make sure they
                                //display the right element when switching between them via the tablinks
                                maptool_urbs_demand.fillSelectedFeatureDemandEditor(e.target);
                                maptool_urbs_buildings.fillSelectedFeatureBuildingEditor(e.target);
                                maptool_urbs_supim.fillSelectedFeatureSupimEditor(e.target);
                            });
                        }
                    }
                    return marker;
                }
            }).addTo(map);
        }
        map.fitBounds(newGeoJson.getBounds());
    }
    
    //TODO: The networkListName parameter is useless, since all info is only stored in busWithLoads
    /**
     * @param {string} htmlListName name of the html select element that is supposed to be filled with the busses with attached loads      
     */
    function populateUrbsEditorLoadBusLists(htmlListName, networkListName) {
        var x = document.getElementById(htmlListName + "Select");
        let networkList = maptool_urbs_buildings.BuildingsObject[networkListName + 'List'];
        //Since indices need not be assigned in a linear fashion, we make sure they are ordered in our list display
        networkList = networkList.sort(function (a, b) {
            return parseInt(a.feature.properties.index) - parseInt(b.feature.properties.index);
        })
        for (idx in networkList) {
            var option = document.createElement("option");
            option.text = networkList[idx].feature.properties.index;
            x.add(option);
        }
    }

    /**
     * @param {string}      feature             name of the feature (i.e demand, process etc) for which input fields are to be generated
     * @param {dict}        propertiesToAdd     properties for which inputs are created, as well as tooltips, types and default values 
     *                                          loaded from the UrbsPropertiesJSON file fetched from the backend
     * @param {function}    writebackFunction   since the storage objects for each feature may not be the same, we can pass a custom function for each feature that 
     *                                          saves inputs on changes to the html element
     * 
     * We create the form and div elements holding input fields and associated labels from scratch right after page load and attach them to the corresponding static
     * editor div
     */
    function populateUrbsEditor(feature, propertiesToAdd, writebackFunction) {
        let form = document.getElementById(feature + 'Form');
        let formDiv = document.createElement('DIV');
        formDiv.id = feature + 'FormDiv';
        formDiv.classList.add('feature-editor__selected-feature-editor__div');
        for (property in propertiesToAdd) {
            let input = document.createElement("input");
            input.setAttribute('onchange', writebackFunction);
            
            //Default types are defined in the UrbsPropertiesJSON and input field types are set accordingly
            if(propertiesToAdd[property]['type'] == 'boolean') {
                input.type="checkbox";
            }
            else if(propertiesToAdd[property]['type'] == 'float' || propertiesToAdd[property]['type'] == 'int') {
                input.type="text";
                input.pattern = "^inf$|\-?\d+\.?\d+|\-?\d+";
            }
            //at the moment list options are passed only in UrbsPropertiesJSON
            //TODO: for some elements (e.g. trafos) these might also be extracted from the previous network-edit step
            else if(propertiesToAdd[property]['type'] == 'list') {
                input = document.createElement("select");
                input.classList.add('feature-editor__selected-feature-editor__stdtype-feature-select')
                input.setAttribute('onchange', writebackFunction);
                for (option in propertiesToAdd[property]['list_options']) {
                    let listOption = document.createElement("option");
                    listOption.text = propertiesToAdd[property]['list_options'][option];
                    listOption.value = propertiesToAdd[property]['list_options'][option];
                    input.add(listOption);
                }
            }
            //everything that has no corresponding special input type gets handled as text input
            else {
                input.type="text"; 
            }
            if(propertiesToAdd[property]["default_val"] != "") {
                if(propertiesToAdd[property]['type'] == 'boolean') {
                    if (propertiesToAdd[property]["default_val"] == "true") {
                        input.checked = true;
                    } 
                    else {
                        input.removeAttribute('checked');
                        console.log(property)
                    } 

                }
                else {
                    input.value = propertiesToAdd[property]["default_val"];
                }
            }
            //each input is uniquely addressable by setting the UrbsPropertiesJSON key as id
            input.id = property;
            input.name = property;

            let label = document.createElement("label");
            label.htmlFor = property;
            label.innerHTML = property;
            //due to css interactions we need to adjust the classes for boolean input labels specifically and place the checkbox into the label
            if(propertiesToAdd[property]['type'] == 'boolean') {
                label.appendChild(input)
                label.classList.add('urbs-checkbox')
            }
            else {
                formDiv.appendChild(input)
            }
            //labels are added below their corresponding so that we can use css to highlight labels of currently selected inputs
            //this only works if the label is placed below the input in the div. We use css offsets to make sure the input is visible below the label in the browser
            formDiv.appendChild(label)
        }
        form.appendChild(formDiv)
    }

    /**
     * @param {string} primaryFeatureName       Key for the primary feature editor to which the secondary feature list is getting attached to
     * @param {string} secondaryFeatureName     Key for the secondary feature  
     */
    function populateEditableNetworkEditorSecondaryFeature(primaryFeatureName, secondaryFeatureName) {
        let editor_form = document.getElementById(primaryFeatureName + 'Form');

        //the secondary feature list is placed into its own div
        let formDiv = document.createElement('DIV');
        formDiv.id = secondaryFeatureName + 'FormDiv';
        formDiv.classList.add('feature-editor__selected-feature-editor__div');

        formDiv.style.display = 'block'
        let label = document.createElement("label");
        label.innerHTML = secondaryFeatureName.toUpperCase();
        label.classList.add('secondary-feature-label');

        formDiv.append(label);
        //The new div contains only a select element that displays all the secondary features attached to the primary feature
        let featureSelect = document.createElement('SELECT');
        featureSelect.id = secondaryFeatureName + 'Select';
        featureSelect.setAttribute('onchange', 'maptool_urbs_setup.openSecondaryEditor(this, "' + secondaryFeatureName + '")')
        featureSelect.classList.add('feature-editor__featurelist-tab__feature-select');
        featureSelect.multiple = true;

        formDiv.append(featureSelect)
        editor_form.appendChild(formDiv);

        //we want to be able to add secondary features in the GUI
        let featureCreateButton = document.createElement('BUTTON');
        featureCreateButton.type = 'button';
        featureCreateButton.classList.add('button');
        featureCreateButton.classList.add('feature-editor__selected-feature-editor__delete-button');
        featureCreateButton.innerHTML = 'Add ' + secondaryFeatureName.toUpperCase();
        featureCreateButton.setAttribute('onclick', 'maptool_urbs_process.openNewProcessForm(true)') 
        editor_form.appendChild(featureCreateButton);
    }


   /**
    * Function makes secondary feature window visible and fills all input fields with the saved values, if any exist
    * At the moment the process editor is the only one with a secondary editor, namely the pro_com_prop editor
    * @param {html select element} sel      gets passed as "this" reference when the onchange method for the select element is called, needed to 
    *                                       retrieve the currently selected secondary feature
    * @param {string} secondaryFeatureName  key for relevant html elements 
    */
    //TODO: currently hardcoded for process. Either generalize or put into process_editor.js
    function openSecondaryEditor(sel, secondaryFeatureName) {
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
    * Function gets called when one of the tablink buttons in the GUI gets pressed and opens the relevant feature list, while hiding all other GUI elements
    * @param {event} e              object for the onclick event of the clicked tablink button, necassary to change the button to active
    * @param {string} listName      key to access the relevant list tab html element by id
    * @param {boolean} hasEditor    some features (like global) do not have a separate editor which means that the section of the code that opens previously open editors 
    *                               doesn't apply to them. This boolean acts as a flag to ensure that portion is not called
    */
    function openUrbsEditorList(e, listName, hasEditor) {
        //hides all lists
        tabcontent = document.getElementsByClassName("feature-editor__featurelist-tab");
        for (i = 0; i < tabcontent.length; i++) {
          tabcontent[i].style.display = "none";
        }   
        
        //hides all editors
        editorcontent = document.getElementsByClassName('feature-editor__selected-feature-editor');
        for (i = 0; i < editorcontent.length; i++) {
            editorcontent[i].style.display = "none";
        }

        //hides all secondary editors
        let secondaryUrbsSetupEditors = document.getElementsByClassName("feature-editor__selected-feature-editor__secondary-editor");
        for (let i = 0; i < secondaryUrbsSetupEditors.length; i++) {
            secondaryUrbsSetupEditors[i].style.display='none';
        }
        
        //sets all tablink buttons to inactive
        tablinks = document.getElementsByClassName("feature-editor__buttons-tab__tablinks");
        for (i = 0; i < tablinks.length; i++) {
          tablinks[i].className = tablinks[i].className.replace(" active", "");
        }
        
        //changes the selected button class and opens the relevant list
        document.getElementById(listName).style.display = "inline-block";
        e.currentTarget.className += " active";
    
        let editor = document.getElementById(listName + "Editor")
        /*
        if a list element had been selected previously and the tab had been closed without another feature editor being opened elsewhere, 
        we reopen the editor window of the currently selected feature
        */
        if(listName != 'processes' && listName != 'storage_conf' && hasEditor) {
            if (document.getElementById(listName + 'Select').selectedIndex != -1) {
            
                if(listName == 'transmission') {
                    let currentList = document.getElementById(listName+ 'Select')
                    editor = document.getElementById(listName + '_' +  currentList.value + "Editor")
                }
                editor.style.display = "inline-block";
            }
        }
        //makes sure the csv editors are displayed properly
        if(listName == 'process_conf') {
            maptool_urbs_process.hot.render();
        }
        if(listName == 'storage_conf') {
            maptool_urbs_storage.hot.render();
        }
    }
    
    /**
     * function that switches bus styles back to default once they are deselected when the user clicks on another node on the map or another element in the list
     * @param {event target object} target the leaflet object whose onclick method has been triggered via click on the map or selection via the list 
     */
    function resetLoadBusStyle(target) {
        let zoomLevel = 14;
        map.setView(target.getLatLng(), Math.max(map.getZoom(), zoomLevel));
    
        if(clicked) {
            let oldStyle = maptool_network_gen.NetworkObject['busStyles'];
            //makes sure the list that holds the previously selected feature deselects all options
            clicked.setStyle(oldStyle[1]);
        }
        target.setStyle(maptool_network_gen.NetworkObject['busStyles'][0]);
        clicked = target;
        
        //we want to make sure the correct option is selected in all lists containing busses
        highlightSelectedElementInList(target, "demandSelect");
        highlightSelectedElementInList(target, "buildingsSelect");
        highlightSelectedElementInList(target, "supimSelect");

    }
    /*
    target (Event target object):   the leaflet object whose onclick method has been triggered via click on the map or selection via the list
    selectId (String):              id of the relevant html element
    Method that changes the selectedIndex of a given select element to the option corresponding to the onclick event target
    */
    function highlightSelectedElementInList(target, selectId) {
        let featureList = maptool_urbs_buildings.BuildingsObject['busWithLoadList'];
        let selectedList = document.getElementById(selectId);
        let newIndex = featureList.findIndex((entry) => entry === target);
        selectedList.selectedIndex = newIndex;
    }
    

    /*
    sel (Html select element):  gets passed as "this" reference when the onchange method for the select element is called, needed to
                                retrieve the currently selected secondary feature
    featureName (String):       key for corresponding html element and call of correct editor fill method
    An aggregate function that calls the relevant editor fill method for each feature and makes sure other editor windows are closed
    */
    function fillSelectedEditor(sel, featureName) {
        //all open primary and secondary feature editors are closed when a new editor window is opened
        let urbsSetupEditors = document.getElementsByClassName("feature-editor__selected-feature-editor");
        for (let i = 0; i < urbsSetupEditors.length; i++) {
            urbsSetupEditors[i].style.display='none';
        }

        let secondaryUrbsSetupEditors = document.getElementsByClassName("feature-editor__selected-feature-editor__secondary-editor");
        for (let i = 0; i < secondaryUrbsSetupEditors.length; i++) {
            secondaryUrbsSetupEditors[i].style.display='none';
        }

        if(featureName == 'demand') {
            maptool_urbs_demand.fillSelectedFeatureDemandEditor(maptool_urbs_buildings.BuildingsObject['busWithLoadList'][sel.selectedIndex]);
            document.getElementById('demandEditor').style.display='inline-block';
        }
        if(featureName == 'buildings') {
            maptool_urbs_buildings.fillSelectedFeatureBuildingEditor(maptool_urbs_buildings.BuildingsObject['busWithLoadList'][sel.selectedIndex]);
            document.getElementById('buildingsEditor').style.display='inline-block';
        }
        if(featureName == 'transmission') {
            trans_editor = sel.value
            document.getElementById('transmission_cable_dataEditor').style.display='none';
            document.getElementById('transmission_trafo_dataEditor').style.display='none';
            document.getElementById('transmission_voltage_limitsEditor').style.display='none';
    
            document.getElementById('transmission_' + trans_editor + 'Editor').style.display='inline-block';
        }
        if(featureName == 'global') {
            document.getElementById(featureName + 'Editor').style.display='inline-block';
        }
        if(featureName == 'pro_prop') {
            document.getElementById(featureName + 'Editor').style.display='inline-block';
            fillSelectedFeatureEditorFields(maptool_urbs_process.ProcessObject.pro_propList[sel.value], featureName);
            console.log(sel.value);
            maptool_urbs_process.fillSecondaryEditorList(maptool_urbs_process.ProcessObject.pro_com_propList[sel.value]);
        }
        if(featureName == 'commodity') {
            document.getElementById(featureName + 'Editor').style.display='inline-block';
            fillSelectedFeatureEditorFields(maptool_urbs_commodity.CommodityObject['commodityPropertiesList'][sel.value], featureName);
            if (maptool_urbs_commodity.CommodityObject['commodityPropertiesList'][sel.value].type == 'Sell' 
                || maptool_urbs_commodity.CommodityObject['commodityPropertiesList'][sel.value].type == 'Buy') {
                document.getElementById('BuySellPriceButton').style.display='inline-block';     
            } 
            else {
                document.getElementById('BuySellPriceButton').style.display='none';        
            }
        }
        if(featureName == 'storage') {
            document.getElementById(featureName + 'Editor').style.display='inline-block';
            fillSelectedFeatureEditorFields(maptool_urbs_storage.StorageObject['storagePropertiesList'][sel.value], featureName);
        }
        if(featureName == 'supim') {
            maptool_urbs_supim.fillSelectedFeatureSupimEditor(maptool_urbs_buildings.BuildingsObject['busWithLoadList'][sel.selectedIndex]);
            document.getElementById('supimEditor').style.display='inline-block';
        }
        if(featureName == 'timevareff') {
            maptool_urbs_timevareff.fillSelectedFeatureTimevareffEditor(maptool_urbs_buildings.BuildingsObject['busWithLoadList'][sel.selectedIndex]);
            document.getElementById('timevareffEditor').style.display='inline-block';
            console.log("he")
        }
    }

    /* 
    target (Dict):          Object that contains all current input values for a single feature
    featureName (String):   key used to access the html elements 
    Function that fills each input field of a single feature once the editor window is opened
    */
    function fillSelectedFeatureEditorFields(target, featureName) {
        let editor_form = document.getElementById(featureName + 'Form');
        let editor_divs = editor_form.children;

        for (let i = 0; i < editor_divs.length; i++) {
            let editor_elems = editor_form.children[i].children;
            for (let i = 0; i < editor_elems.length; i++) {
                if (editor_elems[i].nodeName == 'INPUT' || editor_elems[i].nodeName == 'SELECT') {
                    if(target[editor_elems[i].name] != null) {
                        editor_elems[i].value = target[editor_elems[i].name];
                    }
                    else {
                        editor_elems[i].value = '';
                    }
                }
            }
        }
    }

    function getUrbsPropertiesJSON() {
        return UrbsPropertiesJSON;
    }

    //we retrieve the UrbsPropertiesJSON and create the editor and network visualization on page load of the urbs editor window
    window.addEventListener("load", (event) => {
        if(window.location.pathname == '/urbs') {
            GetUrbsSetupProperties().then(res =>  {
                SetupUrbsEditor();
            });
        }
      });

      return {
        getUrbsPropertiesJSON: getUrbsPropertiesJSON,
        openUrbsEditorList: openUrbsEditorList,
        resetLoadBusStyle: resetLoadBusStyle,
        fillSelectedEditor: fillSelectedEditor,
        openSecondaryEditor: openSecondaryEditor
      }
}();