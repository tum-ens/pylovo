var maptool_urbs_buildings = function () {
    
    let BuildingsObject = {
        "busWithLoadList": [],
        "buildingsPropertiesList": []
    }
    
    /**
     * called during setup of the urbs editor window
     * Fills the buildingsPropertiesList with dicts containing rudimentary information about each building
     * Properties of a building are determined by the UrbsPropertiesJSON and set to null initially, while names and coordinates are extracted from the corresponding busses
     * @param {json dict} UrbsPropertiesJSON contains information about all inputs for all urbs setup features
     */
    function prepareBuildingsObject(UrbsPropertiesJSON) {
        let propertiesToAdd = UrbsPropertiesJSON['_buildings']['from_user_input'];
        for (bus in BuildingsObject.busWithLoadList) {
            let coordinates = BuildingsObject.busWithLoadList[bus].feature.geometry.coordinates
            let name = BuildingsObject.busWithLoadList[bus].feature.properties.name
            let buildingJSON = {"name": name,"lat": coordinates[0], "lon": coordinates[1]};
            for (property in propertiesToAdd) {
                buildingJSON[property] = ''
            }
            BuildingsObject.buildingsPropertiesList.push(buildingJSON);
        }
    }
    
    /**
     * onclick method for bus circlemarkers on the map and onchange method of the buildings list element
     * resets the style of the previously selected bus and fills all editor input fields with the correct values
     * @param {event target object} target 
     */
    function fillSelectedFeatureBuildingEditor(target) {
        maptool_urbs_setup.resetLoadBusStyle(target)
    
        let editor_form = document.getElementById('buildingsForm');
        let editor_divs = editor_form.children;
        
        let selectedBuildingIndex = document.getElementById('buildingsSelect').selectedIndex;
        let selectedBuilding = BuildingsObject.buildingsPropertiesList[selectedBuildingIndex];
    
        for (let i = 0; i < editor_divs.length; i++) {
            let editor_elems = editor_form.children[i].children;
            for (let i = 0; i < editor_elems.length; i++) {
                if (editor_elems[i].nodeName == 'INPUT') {
                    if(selectedBuilding[editor_elems[i].name] != null) {
                        editor_elems[i].value = selectedBuilding[editor_elems[i].name];
                    }
                    else {
                        editor_elems[i].value = '';
                    }
                }
            }
        }
    }

    /**
     * onchange function for all building editor input fields 
     * writes changed value back to the relevant entry in the BuildingsObject
     * @param {event target object} target 
     */
    function writeBackEditedBuildingFeatures(target) {
        let idxInFeatureList = document.getElementById("buildingsSelect").selectedIndex;
        let selectedElement = BuildingsObject.buildingsPropertiesList[idxInFeatureList];
        console.log(selectedElement[target.name], target.value);
        selectedElement[target.name] = target.value;
        console.log(selectedElement[target.name], target.value);
    }

    return {
        BuildingsObject: BuildingsObject,
        prepareBuildingsObject: prepareBuildingsObject,
        fillSelectedFeatureBuildingEditor: fillSelectedFeatureBuildingEditor,
        writeBackEditedBuildingFeatures: writeBackEditedBuildingFeatures
    }
}();

