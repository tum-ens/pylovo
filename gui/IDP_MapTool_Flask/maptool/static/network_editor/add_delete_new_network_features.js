var maptool_add_delete = function() {
    //array that contains all features connected to a bus that's about to be deleted
    let featuresToDeleteList = [];

    /**
     * onclick function for the add feature buttons in the GUI
     * switches leaflet map mode to draw and makes sure we place down the correct marker type
     * @param {string} feature name of the network feature type we want to create
     */
    function addFeature(feature) {
        let style = maptool_network_gen.NetworkObject[feature + 'Styles'][1];
        let type = '';
        if(feature == 'bus' || feature =='ext_grid') {
            type = 'Point'
            map.pm.enableDraw('CircleMarker', {
                snappable: false, 
                snapDistance: 20,
                requireSnapToFinish : (feature == 'ext_grid'), //busses can be placed anywhere, ext_grids need to be placed on a bus
                continueDrawing: false,
                pathOptions: style,
                snapIgnore: (feature == 'ext_grid'),
    
              })
        }
        else {
            type = 'LineString';
            map.pm.enableDraw('Line', {
                snappable: true,
                snapDistance: 20,
                requireSnapToFinish: true, //lines need to form a connection between two busses
                pathOptions: style,
                snapIgnore: true,
              })
        }
    }
    
    /**
     * closes feature deletion popup window and resets style of all highlighted features in the map view
     */
    function closeForm() {
        for (feature in featuresToDeleteList) {
            featuresToDeleteList[feature][0].setStyle(featuresToDeleteList[feature][0].defaultOptions);
        }
        featuresToDeleteList = [];
        document.getElementById("popupForm").style.display = "none";
      }
    
    //TODO: This is awful. Change this
    //TODO: Change featuresToDeleteList to dict. Why did I even make this a list in the first place? 
    /**
     * if you try to delete a bus, the function tries to find all connected features (lines, ext_grids, trafos) and marks them as about to be deleted as well 
     * @param {string} featureName name of the feature type to delete
     * @param {string array} featureLists contains feature names of all features that may be connected to the deletable feature
     */
    function prepareFeatureDelete(featureName, featureLists) {
        if(featureName == 'bus') {
            //opens the delete popup window that allows the user to back out of deleting the bus
            document.getElementById("popupForm").style.display = "block";
            let featureSelect = document.getElementById(featureName + 'Select');
    
            featuresToDeleteList.push([maptool_network_gen.NetworkObject['busList'][featureSelect.selectedIndex], 'bus', featureSelect.selectedIndex]);
            for (featureType in featureLists) {
                for (idx in maptool_network_gen.NetworkObject[featureLists[featureType] + 'List']) {
                    if (featureSelect.options[featureSelect.selectedIndex].text ==  maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx].feature.properties.from_bus) {
                        featuresToDeleteList.push( [maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx], featureLists[featureType], idx]);
                    }
                    if (featureSelect.options[featureSelect.selectedIndex].text ==  maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx].feature.properties.to_bus) {
                        featuresToDeleteList.push( [maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx], featureLists[featureType], idx]);
                    }
                    if(featureSelect.options[featureSelect.selectedIndex].text ==  maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx].feature.properties.bus) {
                        featuresToDeleteList.push( [maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx], featureLists[featureType], idx]);
                    }
                    if(featureSelect.options[featureSelect.selectedIndex].text ==  maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx].feature.properties.hv_bus) {
                        featuresToDeleteList.push( [maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx], featureLists[featureType], idx]);
                    }   
                    if(featureSelect.options[featureSelect.selectedIndex].text ==  maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx].feature.properties.lv_bus) {
                        featuresToDeleteList.push( [maptool_network_gen.NetworkObject[featureLists[featureType] + 'List'][idx], featureLists[featureType], idx]);
                    }
                    
                }
            }
            //highlight all features that are about to be deleted
            for (feature in featuresToDeleteList) {
                featuresToDeleteList[feature][0].setStyle({fillColor: 'red', color: 'red'});
            }
        }
    }
    
    /**
     * removes all features attached to a deletable bus from the map, the network object and the feature list gui
     */
    function deleteConnectedFeatures() {
        let lineCount = 0;
        let trafoCount = 0;
        let ext_gridCount = 0;
        for (feature in featuresToDeleteList) {
            let featureName = featuresToDeleteList[feature][1];
            let featureIndex = featuresToDeleteList[feature][2] - lineCount * (featureName == 'line') - trafoCount * (featureName == 'trafo') - ext_gridCount * (featureName == 'ext_grid')
            let featureSelect = document.getElementById(featureName + 'Select');
            map.removeLayer(maptool_network_gen.NetworkObject[featureName + 'List'][featureIndex]);
            maptool_network_gen.NetworkObject[featureName + 'List'].splice(featureIndex, 1);
            featureSelect.remove(featureIndex);
    
            lineCount += (featureName == 'line');
            trafoCount += (featureName == 'trafo');
            ext_gridCount += (featureName == 'ext_grid');
        } 
    
        featuresToDeleteList = [];
        document.getElementById('busEditor').style.display = 'none';
        document.getElementById("popupForm").style.display = "none";
    }
    
    /**
     * delete function used for features that do not have other features attached to them aka lines, trafos, ext_grids
     * @param {string} featureName key for the type of the feature we want to delete
     */
    function deleteFeature(featureName) {
        let featureSelect = document.getElementById(featureName + 'Select');
        if (featureSelect.selectedIndex != -1) {
            map.removeLayer(maptool_network_gen.NetworkObject[featureName + 'List'][featureSelect.selectedIndex]);
            maptool_network_gen.NetworkObject[featureName + 'List'].splice(featureSelect.selectedIndex, 1);
            featureSelect.remove(featureSelect.selectedIndex);
            document.getElementById(featureName + 'Editor').style.display = 'none';
        }
    
    }
    
    let snapped = false;   
    let snappedFeature;
    let snappedFeatures = [];
    
    /**
     * event listeners ensuring that new features on the map are snapped correctly when we draw them
     */
    map.on('pm:drawstart', ({ workingLayer }) => {
        workingLayer.on('pm:snap', (e) => {
            //we need to get the name of the feature we are snapping to so we can fill in the to_bus, from_bus, hv, lv etc features that require bus names later
            if (e.shape == 'Line' && e.layerInteractedWith.feature != undefined) {
                snappedFeature = e.layerInteractedWith.feature.properties.index;
                snapped = true;
            }
            else {
                snappedFeature = e.layerInteractedWith._parentCopy.feature.properties.index
            }
        });
        
        workingLayer.on('pm:unsnap', (e) => {
            snapped = false; 
            snappedFeature = undefined;   
        });
        
        //if we are drawing a line and the first vertex we place is not on a snapped feature, we immediately remove that vertex
        //otherwise we save the snapped feature for later
        workingLayer.on('pm:vertexadded', (e) => {
            if(map.pm.Draw.Line.enabled() && e.layer.getLatLngs().length == 1) {
                if(!snapped) {
                    map.pm.Draw.Line._removeLastVertex(); 
                }
                else {
                    snappedFeatures.push(snappedFeature);
                }
            }
        });
      });
    
    /**
     *  event listeners ensuring that new features on the map are added to the correct NetworkObject list,
     *  given the correct styling and initialized with the correct values (e.g. name, index, connected busses)
     */
    map.on('pm:create', (e) => {
        let featureName = '';
        let featureType = '';
    
        if(e.shape == 'Line') {
            featureType = 'LineString'
            snappedFeatures.push(snappedFeature);
    
            if ( e.marker.options.color == maptool_network_gen.NetworkObject.lineStyles[1].color) {
                featureName = 'line';
            }
            else if ( e.marker.options.color == maptool_network_gen.NetworkObject.trafoStyles[1].color) {
                featureName = 'trafo';
            }
        }
        else if (e.shape == 'CircleMarker') {
            featureType = 'Point'
            if ( e.marker.options.color == maptool_network_gen.NetworkObject.busStyles[1].color) {
                featureName = 'bus';
            }
            else if ( e.marker.options.color == maptool_network_gen.NetworkObject.ext_gridStyles[1].color) {
                featureName = 'ext_grid';
            }
        }
    
        let featureList = maptool_network_gen.NetworkObject[featureName + 'List'];
        let featureProperties = {}
        for (property in featureList[featureList.length - 1].feature.properties) {
            if (property == "load" || property == "sgen" || property == "switch") {
                featureProperties[property] = {};
            }
            else {
                featureProperties[property] = null;
            }
        }  
    
        if (featureName == 'line') {
            featureProperties['from_bus'] = snappedFeatures[0];
            featureProperties['to_bus'] = snappedFeatures[1];
            featureProperties['std_type'] = "NYY 4x16 SE";
    
            let coords = e.marker.getLatLngs();
            let length = 0;
            for (let i = 0; i < coords.length - 1; i++) {
                length += coords[i].distanceTo(coords[i + 1]);
            }
            
            featureProperties['length_km'] = length/1000;
        }
        if (featureName == 'trafo') {
            featureProperties['hv_bus'] = snappedFeatures[0];
            featureProperties['lv_bus'] = snappedFeatures[1];
            featureProperties['std_type'] = "0.25 MVA 20/0.4 kV";

        }
        if(featureName == 'ext_grid') {
            featureProperties['bus'] = snappedFeature;
        }
    
        snappedFeatures = [];
    
        let featureCoords = [];
    
        if (featureType == 'Point') {
            featureCoords = [e.marker._latlng.lng, e.marker._latlng.lat];
        }
        else {
            for (point in e.marker._latlngs) {
                temp =  [e.marker._latlngs[point].lng, e.marker._latlngs[point].lat];
                featureCoords.push(temp);
            }
        }
    
        featureProperties['index'] = parseInt(featureList[featureList.length - 1].feature.properties['index']) + 1;
        featureProperties["name"] = "New " + featureName + " " + featureProperties['index'];

        let featureGeoJSON = {"type" : "FeatureCollection", "features": []};    
        let featureToAdd = {"type": "Feature", 
                            "geometry": {"coordinates": featureCoords, "type": featureType}, 
                            "properties": featureProperties
                            }; 
        featureGeoJSON.features.push(featureToAdd);
    
        if (featureType == 'Point') {
            L.geoJSON(featureGeoJSON, {
                onEachFeature: function(feature, layer) {
                    maptool_net_display.createPopup(feature, layer);
                    maptool_network_gen.NetworkObject[featureName + 'List'].push(layer);
                },
                pointToLayer: function (feature, latlng) {
                    var marker = L.circleMarker(latlng, maptool_network_gen.NetworkObject[featureName + 'Styles'][1]);
                    marker.on('click', function(e) {
                        maptool_net_display.clickOnMarker(e.target, featureName, 0);
                    });
                    return marker;
                }
            }).addTo(map);
        }
    
        else {
            L.geoJSON(featureGeoJSON, {
                onEachFeature: function(feature, layer) {
                    maptool_net_display.createPopup(feature, layer);
                    maptool_network_gen.NetworkObject[featureName + 'List'].push(layer);
                    layer.on('click', function(e) {
                        maptool_net_display.clickOnMarker(e.target, featureName, 0);
                    })
                },
                style: maptool_network_gen.NetworkObject[featureName + 'Styles'][1]       
            }).addTo(map);
        }
    
        let featureSelect = document.getElementById(featureName + "Select");
        var option = document.createElement("option");
        option.text = featureToAdd.properties.index;
        featureSelect.add(option);
    
        let selectedObject = maptool_network_gen.NetworkObject[featureName + 'List'][featureSelect.options.length - 1];
        //console.log(selectedObject);
        maptool_net_display.clickOnMarker(selectedObject, featureName, 1);
    
        e.marker.remove();
      });

      return {
        addFeature: addFeature,
        closeForm: closeForm,
        prepareFeatureDelete: prepareFeatureDelete,
        deleteConnectedFeatures: deleteConnectedFeatures,
        deleteFeature: deleteFeature
      }
}();