var maptool_return_net = function() {
    function returnEditedNet() {
        fetch("http://127.0.0.1:5000/networks/urbs_results", {
                    method: 'POST',
                    headers: {
                        'Content-type': 'application/json'},
                    body: JSON.stringify(extractNetworFeatures())
            }).then(function (response) {
                return response.json();
            }).catch((err) => console.error(err));
    
        console.log("returned Network");
    }
    
    /**
     * goes through every list in the network object and extracts features and their properties from the saved map objects
     * @returns dict containing all data of the network to be processed in the backend
     */
    function extractNetworFeatures() {
        let featureLists = ["busList", "lineList", "trafoList", "trafo3wList", "ext_gridList"];
        let stdFeatureLists = ["line_stdList", "trafo_stdList", "trafo3w_stdList"]
        let returnObject = {}
    
        featureLists.forEach(function(mapObjectListName) {
            let mapObjectList = maptool_network_gen.NetworkObject[mapObjectListName];
            let featurePropertiesList = [];
            let featureCoordsList = [];
            mapObjectList.forEach(function(mapObjectListElem) {
                featurePropertiesList.push(mapObjectListElem.feature.properties)
                featureCoordsList.push(mapObjectListElem.feature.geometry.coordinates)
            });
            returnObject[mapObjectListName] = featurePropertiesList;
            returnObject[mapObjectListName + "Coords"] = featureCoordsList;
        });
    
        stdFeatureLists.forEach(function(std_typeListName){
            let std_typeObjectList = maptool_network_gen.NetworkObject[std_typeListName];
            returnObject[std_typeListName] = std_typeObjectList;
        });
    
        return returnObject
    }

    return {
        returnEditedNet: returnEditedNet,
        
    }
}();