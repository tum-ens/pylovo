/**
 * retrieves the network the user is working on from the backend and calls all necessary functions to display it on the leaflet map
 * as well as filling the GUI list elements
 */
var maptool_urbs_res_setup = function (){
    let clicked;


    function SetupUrbsResultEditor() {    
        let fetchString = '/urbs_results/editableNetwork';
        fetch(fetchString)
        .then(function (response) {
            return response.json();
        }).then(function (ppdata) {
            console.log(ppdata)
            //create leaflet overlay of the network
            displayNetNew(ppdata);

            tabcontent = document.getElementsByClassName("feature-editor__buttons-tab__tablinks");
            for (i = 0; i < tabcontent.length; i++) {
                tabcontent[i].style.display = "inline-flex";
            }
            
            populateUrbsResultsLoadBusLists();
            maptool_net_display.populateLists('line');
            maptool_net_display.populateLists('trafo');
            maptool_net_display.populateLists('ext_grid');
        });
    }


    /**
     * aggregate function calling the actual functions that place the feature sgeojsons on the leaflet map
     * @param {geojson dict} ppdata 
     */
    function displayNetNew(ppdata) {
        addGeoJSONtoMap(true, ppdata['trafo'], 'trafo');
        //console.log('added all trafos');
        addGeoJSONtoMap(true, ppdata['line'], 'line');
        //console.log("added all lines");
        addGeoJSONtoMap(false, ppdata['ext_grid'], 'ext_grid');
        //console.log('added all external grids');
        addGeoJSONtoMap(false, ppdata['bus'], 'bus');
        //console.log('added all buses');
    }

/**
 * function that adds a FeatureCollection to the leaflet map
 * we set styles and onclick functions here and save references to each added feature in the NetworkObject
 * lines (lines, trafos) and circlemarkers (busses, ext_grids) need to be handled differently because lines do not have the pointToLayer function
 * @param {boolean} isLines 
 * @param {FeatureCollection geoJSON} input_geoJSON 
 * @param {string} featureName 
 */
    function addGeoJSONtoMap(isLines, input_geoJSON, featureName) {
        let newGeoJson
        if (isLines) {
            newGeoJson = L.geoJSON(input_geoJSON, {
                snapIgnore:true,
                onEachFeature: function(feature, layer) {
                    //Atm popups are only there for debug
                    maptool_net_display.createPopup(feature, layer);
                    maptool_network_gen.NetworkObject[featureName + 'List'].push(layer);
                    layer.on('click', function(e) {
                        clickOnMarker(e.target, featureName);
                    })
                },
                style: maptool_network_gen.NetworkObject[featureName + 'Styles'][1]
            }).addTo(map);
        }
        else {
            newGeoJson = L.geoJSON(input_geoJSON, {
                onEachFeature: function(feature, layer) {
                    maptool_net_display.createPopup(feature, layer);
                    maptool_network_gen.NetworkObject[featureName + 'List'].push(layer);
                },
                pointToLayer: function (feature, latlng) {
                    var marker = L.circleMarker(latlng, maptool_network_gen.NetworkObject.nonEditableStyles[0]);
                    if(featureName == 'bus') {
                        if (Object.keys(feature.properties.load).length > 0) {
                            marker.setStyle(maptool_network_gen.NetworkObject.busStyles[1]);
                            marker.on('click', function(e) {
                                clickOnMarker(e.target, featureName);
                            });
                        }
                    }
                    return marker;
                }
            }).addTo(map);
        }
        //makes sure the map centers on the newly displayed network
        map.fitBounds(newGeoJson.getBounds());
    }

    /**
     * @param {string} htmlListName name of the html select element that is supposed to be filled with the busses with attached loads      
     */
    function populateUrbsResultsLoadBusLists() {
        var x = document.getElementById("busSelect");
        let networkList = maptool_network_gen.NetworkObject['busList'];
        //Since indices need not be assigned in a linear fashion, we make sure they are ordered in our list display
        networkList = networkList.sort(function (a, b) {
            return parseInt(a.feature.properties.index) - parseInt(b.feature.properties.index);
        })
        for (idx in networkList) {
            if(Object.keys(networkList[idx].feature.properties.load).length > 0 || networkList[idx].feature.properties.name == 'main_busbar' || networkList[idx].feature.properties.name == 'Trafostation_OS') {
                var option = document.createElement("option");
                option.text = networkList[idx].feature.properties.index + ": " + networkList[idx].feature.properties.name;
                option.value = idx;
                x.add(option);
            }
        }
    }

    /**
     * gets called when one of the tablink buttons in the GUI gets pressed and opens the relevant feature list, while hiding all other GUI elements
     * editors are closed, lists are hidden, buttons are set to inactive
     * @param {event} e 
     * @param {string} listName 
     */
    function openEditableNetworkList(e, listName) {
        tabcontent = document.getElementsByClassName("feature-editor__featurelist-tab");
        for (i = 0; i < tabcontent.length; i++) {
        tabcontent[i].style.display = "none";
        }

        editorcontent = document.getElementsByClassName('feature-editor__selected-feature-results');

        for (i = 0; i < editorcontent.length; i++) {
            editorcontent[i].style.display = "none";
        }

        tablinks = document.getElementsByClassName("feature-editor__buttons-tab__tablinks");
        for (i = 0; i < tablinks.length; i++) {
        tablinks[i].className = tablinks[i].className.replace(" active", "");
        }

        document.getElementById(listName).style.display = "inline-block";
        e.currentTarget.className += " active";

        let editor = document.getElementById(listName + "Editor")
        //if a list element had been selected previously and the tab had been closed without another feature editor being opened elsewhere, we reopen the editor window of the 
        //currently selected feature
        if(listName != 'std_types') {
            if (document.getElementById(listName + 'Select').selectedIndex != -1) {
                editor.style.display = "inline-block";
            }
        }
    }

    /**
     * onchange method for the feature lists in the GUI window
     * picks the corresponding map object for the selected list element to execute the clickonmarker function on
     * @param {html select object} sel reference to the select element that just changed
     * @param {string} listName key for NetworkObject list
     */
    function fillSelectedEditableNetworkFeatureEditor(sel, listName) {
        let idx = parseInt(sel.options[sel.selectedIndex].value);    
        let selectedObject = maptool_network_gen.NetworkObject[listName + 'List'][idx];
        
        clickOnMarker(selectedObject, listName, 0);
    }

    /**
     * resets the style of the previously selected feature
     * @param {html element} target 
     * @param {string} feature 
     */
    function resetStyle(target, feature) {
        let zoomLevel = 14;
        if(feature == 'bus' || feature == 'ext_grid') {
            map.setView(target.getLatLng(), Math.max(map.getZoom(), zoomLevel));
        }
        else {
            map.setView(target.getLatLngs()[0], Math.max(map.getZoom(), zoomLevel));
        }

        if(clicked) {
            let oldStyle = maptool_network_gen.NetworkObject[clicked[1] + 'Styles'];
            //makes sure the list that holds the previously selected feature deselects all options
            if(clicked[1] != feature) {
                document.getElementById(clicked[1] + 'Select').value = "";
            }
            clicked[0].setStyle(oldStyle[1]);
        }
        target.setStyle(maptool_network_gen.NetworkObject[feature + 'Styles'][0]);
        clicked = [target, feature];
    }

    /**
     * When clicking on a map element or making a selection from a list, 
     * we highlight the relevant element, open the Editor window and fill its input fields with the relevant values
     * @param {html element} target the object that has been interacted with on the map. Null in case we have clicked on a list element
     * @param {string} feature      key for accessing f.e. featurelist in the NetworkObject
     */
    function clickOnMarker(target, feature) {
        //resets previously selected marker to it's original display style
        if(target != null) {
            resetStyle(target, feature);
        }

        let featureList = maptool_network_gen.NetworkObject[feature + 'List'];
        let selectedList = document.getElementById(feature + "Select");
        let featureIndex = featureList.findIndex((entry) => entry === target);
        
        //since for example the GUI bus list does not hold all busses, there is no 1to1 connection between the GUI select and the NetworkObjet list
        //therefore we need to manually find the correct option in the GUI select via the options value field, which corresponds to the index of the map object 
        //in the NetworkObject list
        let newIndex = selectedList.selectedIndex;
        for(let i = 0; i < selectedList.options.length; i++) {
            if(selectedList.options[i].value == parseInt(featureIndex)) {
                newIndex = i;
                break;
            }
        }
        selectedList.selectedIndex = newIndex;
        
        //we display the feature list by triggering an onclick event for its tablink button
        let selectedButton = document.getElementById(feature + "ListButton");
        selectedButton.click();

        //we hide all open editors
        let editorcontent = document.getElementsByClassName('feature-editor__selected-feature-results');
        for (i = 0; i < editorcontent.length; i++) {
            editorcontent[i].style.display = "none";
        }
        console.log(document.getElementById(feature + 'Select').selectedIndex)
        let targetIndex = document.getElementById(feature + 'Select').options[document.getElementById(feature + 'Select').selectedIndex].value;
        let targetName = {};
        if (feature == 'bus') {
            targetName['bus'] = maptool_network_gen.NetworkObject[feature + 'List'][targetIndex].feature.properties.name;
        }
        if(feature == 'line') {
            target_idx_from = maptool_network_gen.NetworkObject[feature + 'List'][targetIndex].feature.properties.from_bus;
            target_idx_to = maptool_network_gen.NetworkObject[feature + 'List'][targetIndex].feature.properties.to_bus;
            targetName['from_bus'] = maptool_network_gen.NetworkObject['busList'][target_idx_from].feature.properties.name;
            targetName['to_bus'] = maptool_network_gen.NetworkObject['busList'][target_idx_to].feature.properties.name;

        }

        //we retrieve the plots associated with our selected feature from the backend
        getPlotOfFeature(feature, targetName);
        document.getElementById(feature + 'Editor').style.display = 'inline-block';
    }

    /**
     * sends the feature type and the name of the feature we want to generate a plot for to the backend
     * @param {string} feature      to distinguish what type of plot we want to generate in the backend
     * @param {string} targetName   key needed to access data for a specific site in the hdf5 file
     */
    function getPlotOfFeature(feature, targetName) {

        data_json = {"type": feature,"name": targetName}

        fetch("http://127.0.0.1:5000/urbs_results/plots", {
            method: 'POST',
            headers: {
                'Content-type': 'application/json'},
            body: JSON.stringify(data_json)
        }).then(function (response) {
            return response.json();
        }).then(function (plot_data) {
            console.log(plot_data);
            aggregatorDiv = document.getElementById(feature + 'Editor');
            for(entry in plot_data) {
                console.log(entry)
                
                plotDiv = document.getElementById(entry);
                if(plotDiv == null) {
                    plotDiv = document.createElement("div");
                    plotDiv.id = entry;
                    plotDiv.style = {'width:' : '100%'};
                    aggregatorDiv.appendChild(plotDiv)
                }

                x = plot_data[entry].replace(/\\"/g, '"');
                plotDiv.innerHTML = "";
                setInnerHTML(plotDiv, x)
            }
        }).catch((err) => console.error(err));
    }

    /**
     * makes sure inline script is executed once we add a plot to the GUI, which makes the plot visible and interactible
     * from  https://stackoverflow.com/questions/2592092/executing-script-elements-inserted-with-innerhtml
     * @param {html element} elm   html div element we want to add the plot to
     * @param {*} html             html code we want to add to the div 
     */
    function setInnerHTML(elm, html) {
        elm.innerHTML = html;
        
        Array.from(elm.querySelectorAll("script"))
          .forEach( oldScriptEl => {
            const newScriptEl = document.createElement("script");
            
            Array.from(oldScriptEl.attributes).forEach( attr => {
              newScriptEl.setAttribute(attr.name, attr.value) 
            });
            
            const scriptText = document.createTextNode(oldScriptEl.innerHTML);
            newScriptEl.appendChild(scriptText);
            
            oldScriptEl.parentNode.replaceChild(newScriptEl, oldScriptEl);
        });
      }

    /**
     * makes sure the network is properly displayed on window load
     */
    window.addEventListener("load", (event) => {

        if(window.location.pathname == '/urbs_results') {
            SetupUrbsResultEditor();
        }
    });
    
    return {
        openEditableNetworkList: openEditableNetworkList,
        fillSelectedEditableNetworkFeatureEditor: fillSelectedEditableNetworkFeatureEditor,
        clickOnMarker: clickOnMarker
    }
}();