var maptool_network_gen = function (){
    let line_std_properties = {};
    let trafo_std_properties = {};
    let trafo3w_std_properties = {};

    let line_properties = {};

    let ext_grid_properties = {};

    let bus_properties = {};  
    let load_features = {};
    let sgen_features = {};
    let switch_features = {};  

    let trafo_properties = {};
    let NetworkObject = {
        'busList' : [],
        'lineList' : [],
        'trafoList' : [],
        'trafo3wList' : [],
        'ext_gridList' : [],

        'line_stdList' : [],
        'trafo_stdList' : [],
        'trafo3w_stdList' : [],
        
        'loadList': [],
        'sgenList': [],
        'switchList': [],

        'busStyles': [{  radius: 5,
                fillColor: "#d67900",
                color: "#4e2204",
                weight: 1,
                opacity: 1,
                fillOpacity: 1
            }
            ,
            {   radius: 3,
                fillColor: "#0065BD",
                color: "#012b8c",
                weight: 1,
                opacity: 1,
                fillOpacity: 1
            }],
        'lineStyles': [{ color: "#ff0000",
                weight: 2,
                opacity: 1
            }
            ,
            {   color: "#007deb",
                weight: 2,
                opacity: 1
            }],
        'ext_gridStyles': [{  radius: 15,
                fillColor: "#f55353",
                color: "#cf2d3b",
                weight: 1,
                opacity: 1,
                fillOpacity: 1
            }
            ,
            {   radius: 14,
                fillColor: "#f5da53",
                color: "#e6b029",
                weight: 1,
                opacity: 1,
                fillOpacity: 1
            }],
        'trafoStyles': [{color: "#ff0000",
                weight: 7,
                opacity: 1
            }
            ,
            {   color: "#42bd4a",
                weight: 5,
                opacity: 1
            }],
        'nonEditableStyles' : [{color: "#363636",
                fillColor: "#363636",
                weight: 2,
                radius: 3,
                opacity: 1,
                fillOpacity: 1
            }]     
    }

/**
 * gets called when the window is first loaded, retrieves preprocessed Geojson data from the backend
 * and calls necessary functions to display network on the map and fill editor windows
 */
    function GetPandapowerAndWriteGeoJSONNet() {    
        //we fetch the properties json file detailing all inputs, their data types, default values etc for all features
        fetch('/networks/networkProperties')       
        .then(function (response) {
            return response.json();
        }).then(function(properties) {
            line_std_properties = properties['std_type']['line']
            trafo_std_properties = properties['std_type']['trafo']
            trafo3w_std_properties = properties['std_type']['trafo3w']

            line_properties = properties['line']

            ext_grid_properties = properties['ext_grid']

            bus_properties = properties['bus']
            load_features = properties['load']
            sgen_features = properties['sgen']
            switch_features = properties['switch']

            trafo_properties = properties['trafo']
            trafo3w_properties = properties['trafo3w']
        });
    
        fetch('/networks/editableNetwork')
        .then(function (response) {
            return response.json();
        }).then(function (ppdata) {
            
            var layers = L.PM.Utils.findLayers(map);
            layers.forEach((layer) =>{
                    layer.remove();
            });

            displayNetNew(ppdata);
            changeTrafoBusNames(NetworkObject.busList, NetworkObject.trafoList)

            tabcontent = document.getElementsByClassName("feature-editor__buttons-tab__tablinks");
            for (i = 0; i < tabcontent.length; i++) {
                tabcontent[i].style.display = "inline-flex";
            }
            
            extractStdTypes(JSON.parse(ppdata["std_types"]));
            maptool_net_display.fillStdTypeList();

            maptool_net_display.populateLists('bus');
            maptool_net_display.populateLists('line');
            maptool_net_display.populateLists('trafo');
            maptool_net_display.populateLists('ext_grid');

            maptool_net_display.populateEditableNetworkEditor('bus', bus_properties, null, null);
            maptool_net_display.populateEditableNetworkEditorSecondaryFeature('bus','load');
            maptool_net_display.populateEditableNetworkEditorSecondaryFeature('bus','sgen');
            maptool_net_display.populateEditableNetworkEditorSecondaryFeature('bus','switch');
            maptool_net_display.populateEditableNetworkEditor('load', load_features, null, null);
            maptool_net_display.populateEditableNetworkEditor('sgen', sgen_features, null, null);
            maptool_net_display.populateEditableNetworkEditor('switch', switch_features, null, null);

            maptool_net_display.populateEditableNetworkEditor('line', line_properties, NetworkObject.line_stdList, line_std_properties);
            maptool_net_display.populateEditableNetworkEditor('trafo', trafo_properties, NetworkObject.trafo_stdList, trafo_std_properties);
            maptool_net_display.populateEditableNetworkEditor('ext_grid', ext_grid_properties, null, null);
            maptool_net_display.populateEditableNetworkEditor('line_std_types', line_std_properties, null, null);
            maptool_net_display.populateEditableNetworkEditor('trafo_std_types', trafo_std_properties, null, null);
            maptool_net_display.populateEditableNetworkEditor('trafo3w_std_types', trafo3w_std_properties, null, null);
        });
    }

    //The busses connected to the trafo need to have specific names to work with urbs later
    function changeTrafoBusNames(busList, trafoList) {
        trafoList.forEach(trafo => {
            busList.forEach(bus => {
                if (bus.feature.properties.index == trafo.feature.properties.lv_bus) {
                    bus.feature.properties.name = "main_busbar";
                }
                if (bus.feature.properties.index == trafo.feature.properties.hv_bus) {
                    bus.feature.properties.name = "Trafostation_OS";

                }
            })
        })
    }

    /**
     * small aggregate function for all std types
     * @param {dict} ppdata dict retrieved from the backend
     */
    function extractStdTypes(ppdata) {
        NetworkObject.line_stdList= ppdata['line'];
        NetworkObject.trafo_stdList = ppdata['trafo'];
        NetworkObject.trafo3w_stdList = ppdata['trafo3w'];
    }
    
    /**
     * aggregate function calling the actual functions that place the feature geojsons on the leaflet map
     * @param {dict} ppdata dict retrieved from the backend
     */
    function displayNetNew(ppdata) {
        addGeoJSONtoMap(true, ppdata['line'], 'line');
        //console.log("added all lines");

        addGeoJSONtoMap(false, ppdata['ext_grid'], 'ext_grid');
        //console.log('added all external grids');

        addGeoJSONtoMap(false, ppdata['bus'], 'bus');
        //console.log('added all buses');

        addGeoJSONtoMap(true, ppdata['trafo'], 'trafo');
        //console.log('added all trafos');
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
                    maptool_net_display.createPopup(feature, layer);
                    NetworkObject[featureName + 'List'].push(layer);
                    layer.on('click', function(e) {
                        maptool_net_display.clickOnMarker(e.target, featureName, 0);
                    })
                },
                style: NetworkObject[featureName + 'Styles'][1]
            }).addTo(map);
        }
        else {
            newGeoJson = L.geoJSON(input_geoJSON, {
                onEachFeature: function(feature, layer) {
                    maptool_net_display.createPopup(feature, layer);
                    NetworkObject[featureName + 'List'].push(layer);
                },
                pointToLayer: function (feature, latlng) {
                    var marker = L.circleMarker(latlng, NetworkObject[featureName + 'Styles'][1]);
                    marker.on('click', function(e) {
                        maptool_net_display.clickOnMarker(e.target, featureName, 0);
                    });
                    return marker;
                }
            }).addTo(map);
        }
        map.fitBounds(newGeoJson.getBounds());
    }

    window.addEventListener("load", (event) => {
        if(window.location.pathname == '/networks') {
            GetPandapowerAndWriteGeoJSONNet();
        }
    });

    return {
        NetworkObject:NetworkObject,
        displayNetNew: displayNetNew
    }
}();