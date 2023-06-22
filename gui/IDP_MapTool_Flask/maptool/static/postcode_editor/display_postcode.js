var maptool_display_postcode = function (){
    //leaflet map layer object that saves the last selected object to reset its style once a new one is selected
    let previousSelectedPreviewLayer;
    //array of arrays containing leaflet map layer object of a network and its kcid & bcid
    let netList = [];
    //leaflet map layer object for residential buildings
    let res_building_geojson;
    //leaflet map layer object for other buildings
    let oth_building_geojson;
    //array of all existing versions of a network
    let versions;
    
    //css styles for buildings
    let res_style = {
        fillColor: "#0065BD",
        color: "#0065BD",
        weight: 1,
        opacity: 1,
        fillOpacity: 0.5
    }
    
    let oth_style = {
        fillColor: "#E37222",
        color: "#E37222",
        weight: 1,
        opacity: 1,
        fillOpacity: 0.7
    }
    
    /**
     * We only ever want to have one shape at the same time for area selection,
     * so we delete all currently active shapes and change the Select Area/Generate Network button back to Select Area
     */
    map.on('pm:create', (e) => {
        console.log(e)
        var layers = L.PM.Utils.findLayers(map);
        layers.forEach((layer) =>{
            if(layer != e.layer) {
                layer.remove();
            }
            });
        btn = document.getElementById("selectPLZAreaButton");
        btn.disabled = false;
        btn.innerText="Select Area";
        btn.setAttribute('onclick',"maptool_display_postcode.getPostalCodeArea(this, 'plz-area')")
        });

    /**
     * onclick function for submit plz button
     * returns all versions of network associated with the passed id and generates the radiobuttons for the version select gui element
     */
    function selectVersionOfPostalCodeNetwork() {
        let plz = document.getElementById("PLZ").value;
        fetch("http://127.0.0.1:5000/postcode", {
                    method: 'POST',
                    headers: {
                        'Content-type': 'application/json'},
                    body: JSON.stringify(plz)
            }).then(function (response) {
                return response.json();
            }).then(function (versionData) {
                versions = versionData;
                document.getElementById("plzVersionPopupForm").style.display = "block";
    
                let versionRadioButtonsDiv = document.getElementById("versionRadioButtons");
                while (versionRadioButtonsDiv.firstChild) {
                    versionRadioButtonsDiv.removeChild(versionRadioButtonsDiv.lastChild);
                }
                
                for (version in versionData) {
                    let versionRadioButtonDiv = document.createElement("div");
                    versionRadioButtonDiv.classList.add("form_popup__version-select__radio-button");
                    let versionRadioButton = document.createElement("INPUT");
                    versionRadioButton.setAttribute("type", "radio");
                    versionRadioButton.name = "versionRadioButton";
                    versionRadioButton.id = versionData[version][0];
                    versionRadioButton.value = versionData[version][0];
                    versionRadioButtonDiv.append(versionRadioButton);
    
                    let versionLabel = document.createElement("LABEL");
                    versionLabel.htmlFor = versionData[version][0];
                    versionLabel.innerHTML = versionData[version][0];
                    versionRadioButtonDiv.append(versionLabel);    
                    versionRadioButtonDiv.append(document.createElement("br"))
                    versionRadioButtonsDiv.append(versionRadioButtonDiv);
                }
    
                let versionRadioButtonDiv = document.createElement("div");
                versionRadioButtonDiv.classList.add("form_popup__version-select__radio-button");
                let newVersionRadioButton = document.createElement("INPUT");
                newVersionRadioButton.setAttribute("type", "radio");
                newVersionRadioButton.name = "versionRadioButton";
                newVersionRadioButton.id = "newVersionRadioButton";
                newVersionRadioButton.value = "0.0";
                versionRadioButtonDiv.append(newVersionRadioButton);
    
                let newVersionLabel = document.createElement("LABEL");
                newVersionLabel.htmlFor = "newVersionRadioButton";
                newVersionLabel.innerHTML = "New Version";
                versionRadioButtonDiv.append(newVersionLabel);  
    
                let newVersionTextInput = document.createElement("INPUT");
                newVersionTextInput.setAttribute("type", "number");
                newVersionTextInput.name = "newVersionTextInput";
                newVersionTextInput.id = "newVersionTextInput";
                newVersionTextInput.placeholder = "new Version";
                versionRadioButtonsDiv.append(versionRadioButtonDiv);
                versionRadioButtonsDiv.append(newVersionTextInput);
    
            }).catch((err) => console.error(err));
    }
    
    /**
     * onclick function for the Choose Version button of the version select gui element
     * @returns nothing. Return statement exists simply to break out of function in case of faulty input
     */
    function chooseVersionOfPlzNetwork() {
    
        let versionElement = document.querySelector('input[name="versionRadioButton"]:checked')
        if(versionElement) {
            let version = versionElement.value;
            //if we choose the new version option
            if(version == "0.0") {
                let newVersionInput = document.getElementById("newVersionTextInput");
                //We check if the text input for the new version name contains a value at all
                if (newVersionInput.value) {
                    //we make sure the name has no overlap with currently existing versions
                    for (idx in versions) {
                        if (versions[idx][0] == String(newVersionInput.value)) {
                            document.getElementById("newVersionTextInput").style.outline = "red 5px solid";
                            return
                        }
                    }
                    version = newVersionInput.value;
                }
                else {
                    document.getElementById("newVersionTextInput").style.outline = "red 5px solid";
                    return
                }
            }
            //the chosen version is passed back to flask and then all networks associated with the version are fetched
            fetch("http://127.0.0.1:5000/postcode/plz/version", {
                method: 'POST',
                headers: {
                    'Content-type': 'application/json'},
                body: JSON.stringify(version)
            }).then(function (response) {
                console.log(response)
            }).then(function () {
                getPostalCodeArea(null, 'plz-number');
                closeForm("plzVersionPopupForm");
            }).catch((err) => console.error(err));
        } 
        else {
            alert("please select a version");
        }
    }
    
    /**
     * @param {HTML button element} btn 
     * @param {string} plz_type             Flag used to determine whether user wants to generate new networks from area selection or look at already existing ones
     */
    function getPostalCodeArea(btn, plz_type) {
        /**
         * if the user wants to look at preexisting networks we initially post the plz again and get the outline of our network area as a geojson file, 
         * which we display on the map
         * We then fetch all networks included in that area and display only the lines to avoid performance hits due to too many objects
         */
        if (plz_type == 'plz-number') {
            let plz = document.getElementById("PLZ").value;
            fetch("http://127.0.0.1:5000/postcode/plz", {
                        method: 'POST',
                        headers: {
                            'Content-type': 'application/json'},
                        body: JSON.stringify(plz)
                }).then(function (response) {
                    return response.json();
                }).then(function (postcodeData) {
                    let postcodeGeoJSON = L.geoJSON(postcodeData, {style:{ color: '#003359', dashArray: '5'}}).addTo(map);
                    map.fitBounds(postcodeGeoJSON.getBounds());
                    console.log('starting Postcode nets fetch');
                    fetch('/postcode/nets')
                    .then(function (response) {
                        return response.json();
                    }).then(function (postcodeNets) {
                        for(let i = 0; i < postcodeNets.length; i++) {
                            displayPreviewNet(postcodeNets[i][0], postcodeNets[i][1], JSON.parse(postcodeNets[i][2])["line"]);
                        }
                        console.log('added all nets in plz area');
                        populateNetList('network', netList)
                    });
                }).catch((err) => console.error(err));
        }
        /**
         * if the user wants to generate networks from a newly selected area shape, we initially return the shape the user has selected and receive a response
         * containing the building shapes contained in the selected area
         * We check res and oth buildings for emptiness and display them on the map
         * We also change the Select Area button to Generate Network button
         */
        if (plz_type == 'plz-area') {
            var layers = L.PM.Utils.findLayers(map);
            if(layers.length != 0) {
                var group = L.featureGroup();
                layers.forEach((layer)=>{
                    group.addLayer(layer);
                });
                shapes = group.toGeoJSON();
                fetch("http://127.0.0.1:5000/postcode/area", {
                    method: 'POST',
                    headers: {
                        'Content-type': 'application/json'},
                    body: JSON.stringify(shapes)
                }).then(function (response) {
                    return(response.json());
                }).then(function (building_data) {
                    group.remove();
                    var layers = L.PM.Utils.findLayers(map);
                    layers.forEach((layer) =>{
                            layer.remove();
                        });
                    if(building_data.res_buildings.features != null) {
                        res_building_geojson = L.geoJSON(building_data.res_buildings, {
                            style : res_style,
                            onEachFeature: function(feature, layer) {
                                feature.properties.type = 'res';
                                onEachFeature (feature, layer);
                            }
                        }).addTo(map);
                    }
                    if(building_data.oth_buildings.features != null) {
                        oth_building_geojson = L.geoJSON(building_data.oth_buildings, {
                            style : oth_style,
                            onEachFeature: function(feature, layer) {
                                feature.properties.type = 'oth'
                                onEachFeature (feature, layer);
                            }
                        }).addTo(map);
                    }
                    if(building_data.res_buildings.features != null || building_data.oth_buildings.features != null) {
                        btn.innerText="Generate Network";
                        btn.setAttribute('onclick','maptool_display_postcode.openAreaPopup()')
                    }
                }).catch((err) => console.error(err));
            }
        }
    }
    
    /**
     * onclick function for the Generate Network button
     * makes the area version input GUI visible and adds a listener to it that makes sure the form can only be submitted if the inputs are correct
     */
    function openAreaPopup() {
        let formDiv = document.getElementById("plzAreaPopupForm");
        formDiv.style.display = "block";
        const form = formDiv.children[0];
        form.addEventListener("change",() => {
            document.getElementById('submitBtn').disabled = !form.checkValidity()
            console.log("changed", form.checkValidity())
        });
    }
    
    /**
     * onclick function for the Generate Network button within the area version input GUI
     * at the moment it only returns the new id and gives an error warning, if the selected version already exists for a given ID
     * It also closes the GUI form
     */
    function returnSelectedBuildings() {
        let newID = document.getElementById("newNetIDInput").value;
        let newVersion = document.getElementById("newNetVersionInput").value;
    
        console.log(newID, newVersion);
        
        fetch("http://127.0.0.1:5000/postcode/area/new-net-id", {
            method: 'POST',
            headers: {
                'Content-type': 'application/json'},
            body: JSON.stringify({ID: newID, version: newVersion})
        }).then(function (response) {
            if(response.status == 400) {
                alert("Version " + newVersion +  " already exists for ID " + newID);
            }
            return response;
        }).catch((err) => console.error(err));
    
        closeForm("plzAreaPopupForm");
    }
    
    
    /**
     * 
     * @param {int} kcid                    the k cluster id of a network
     * @param {int} bcid                    the building cluster id of a network
     * @param {geoJSON dict} line_geoJSON   a dict containing all the lines of a network
     * 
     * adds all lines of a network to a new layer and displays it on the map
     * further defines several inner functions to handle click, mouseover and mouseout functionality and attaches them to the layer
     * We only display the lines of all networks for performance reasons, showing buses adds too many nodes.
     */
    function displayPreviewNet(kcid, bcid, line_geoJSON) {
        let linePreviewLayer = L.geoJSON(line_geoJSON, {
            style: maptool_network_gen.NetworkObject.lineStyles[1], 
        }).addTo(map);
    
        netList.push([kcid, bcid, linePreviewLayer]);
    
        linePreviewLayer.on('click', styleWhenClick)
        linePreviewLayer.on('mouseover', styleWhenMouseOver)
        linePreviewLayer.on('mouseout', styleWhenMouseOut)
        
        /**
         * resets styles for all lines within a previously selected network, if it exists, makes sure the correct network is highlighted in the list
         * and activates the Select Network Button if it has not been enabled yet
         */
        function styleWhenClick() {
            if(previousSelectedPreviewLayer) {
                previousSelectedPreviewLayer.eachLayer(function (layer) {
                    previousSelectedPreviewLayer.resetStyle(layer)
                })
            }
            linePreviewLayer.setStyle({ color: '#E37222', weight: 3})
            previousSelectedPreviewLayer = linePreviewLayer;
    
            map.fitBounds(linePreviewLayer.getBounds());
    
            let selectedNetwork = document.getElementById("networkSelect");
            let newIndex = netList.findIndex((entry) => entry[2] === linePreviewLayer);
            selectedNetwork.selectedIndex = newIndex;
    
            let selectNetworkButton = document.getElementById("selectNetworkButton");
            selectNetworkButton.disabled = false;
        }
        
        /**
         * changes color of network in GUI when the mouse hovers above it
         */
        function styleWhenMouseOver() {
            if(linePreviewLayer != previousSelectedPreviewLayer) {
                linePreviewLayer.setStyle({ color: '#005293', fillColor: '#005293' , weight: 3})
            }
        }
        /**
         * changes color of network in GUI back of default once the mouse no longer hovers above it
         */
        function styleWhenMouseOut() {
            if(linePreviewLayer != previousSelectedPreviewLayer) {
                linePreviewLayer.eachLayer(function (layer) {
                    linePreviewLayer.resetStyle(layer)
                })
            }
        }
    }
    
    /**
     * 
     * @param {string} listName key for the html select element we want to attach options to
     * @param {list} list       list containing the objects we want to create select options for
     * creates options for each network we have gotten from the backend
     */
    function populateNetList(listName, list) {
        let networkList = document.getElementsByClassName("list-selection");
        networkList[0].style.display = "inline-block";
        let x = document.getElementById(listName + "Select");
        
        //sets a minimum size for our list for the css purposes, to make sure the display of the list is not too short
        x.size = (list.length > 24) ? 24 : list.length;
        for (idx in list) {
            var option = document.createElement("option");
            option.text = "Network " + idx;
            option.value = idx;
            x.add(option);
        }
    }
    
    /**
     * @param {html select element} sel reference to the network select html element
     * onclick function for the network select html element
     * makes sure a network selected in the html select element is highlighted on the map by manually triggering a click event for it
     */
    function highlightSelectedPreviewLayer(sel) {
        let idx = parseInt(sel.options[sel.selectedIndex].value);
        let selectedObject = netList[idx][2];
        selectedObject.fireEvent('click');
    }
    
    /**
     * extracts kcid and bcid from the feature properties of the selected element in the netlist and sends it to the backend
     */
    function sendBackSelectedNetworkKcidBcid() {
        let selectedNetwork = document.getElementById("networkSelect");
        let kcid_bcid = [netList[selectedNetwork.selectedIndex][0], netList[selectedNetwork.selectedIndex][1]];
        console.log(selectedNetwork.selectedIndex, kcid_bcid);
    
        fetch("http://127.0.0.1:5000/postcode/nets", {
                    method: 'POST',
                    headers: {
                        'Content-type': 'application/json'},
                    body: JSON.stringify(kcid_bcid)
            }).then(function (response) {
                return response.json();
            }).catch((err) => console.error(err));
    }
    
    /**
     * @param {event} e 
     * mouseover function for buildings on the map
     */
    function highlightBuildingFeature(e) {
        var layer = e.target;
        let color = '#a14d12'
    
        if(e.target.feature.properties.type == 'res') {
            color = '#003359'
        }
    
        layer.setStyle({
            weight: 5,
            color: color,
            dashArray: '',
            fillOpacity: 0.7
        });
    
        layer.bringToFront();
    }
    /**
     * @param {event} e 
     * mouseout function for buildings on the map
     */
    function resetBuildingHighlight(e) {
        if(e.target.feature.properties.type == 'res') {
            res_building_geojson.resetStyle(e.target);
        }else {
            oth_building_geojson.resetStyle(e.target);
        }
    }
    
    /**
     * @param {event} e 
     * onclick function for buildings on the map
     */
    function zoomToBuildingFeature(e) {
        map.fitBounds(e.target.getBounds());
    }
    
    /**
     * @param {event} e 
     * aggregate onclick function for buildings on the map in case click events should have multiple effects
     */
    function displayBuildingEditOptions(e) {
        zoomToBuildingFeature(e);
    }
    
    /**
     * @param {dict} feature 
     * @param {leaflet layer object} layer 
     * is called when buildings are displayed on the map and defines different behaviours for each object on the map
     */
    function onEachFeature(feature, layer) {
        createBuildingPopup(feature, layer);
        layer.on({
            mouseover: highlightBuildingFeature,
            mouseout: resetBuildingHighlight,
            click: displayBuildingEditOptions
        });
    }
    
    /**
     * @param {dict} feature 
     * @param {leaflet layer object} layer 
     * is called for each building when they are placed on the map and attaches a popup to it, containing a button that allows the user to delete that building
     * the popup could contain other information as well
     */
    function createBuildingPopup(feature, layer) {
        var container = L.DomUtil.create('div');
        var button = L.DomUtil.create('button', 'button cancel', container);
        button.innerText = 'delete Building';
        button.onclick = function() {
            map.removeLayer(layer);
        }
    
        var popup = L.popup();
        popup.setContent(
            button
        );
        layer.bindPopup(popup);
    }
    
    function closeForm(id) {
        document.getElementById(id).style.display = 'none';
    }

    return {
        selectVersionOfPostalCodeNetwork: selectVersionOfPostalCodeNetwork,
        chooseVersionOfPlzNetwork:chooseVersionOfPlzNetwork,
        getPostalCodeArea: getPostalCodeArea,
        openAreaPopup: openAreaPopup,
        returnSelectedBuildings: returnSelectedBuildings,
        highlightSelectedPreviewLayer: highlightSelectedPreviewLayer,
        sendBackSelectedNetworkKcidBcid: sendBackSelectedNetworkKcidBcid,
        closeForm: closeForm
    }

}();
