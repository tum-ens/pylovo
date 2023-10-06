//TODO: Handle upload of BSP, provide template form
//TODO: If none uploaded, create basic BSP before returning

var maptool_urbs_commodity = function () {
    
    let CommodityObject = {
        "commodityPropertiesList": {},
        "commodityPropertiesTemplate": {}
    }

    /**
     * retrieves commodity feature templates from the backend and generates a dict for each commodity, holding default values for all inputs
     */
    function fetchCommodityProfiles() {
        return fetch('urbs/commodity_profiles')
        .then(function (response) {
            return response.json();
        }).then(function (data) {
            let commodity = JSON.parse(data["com_prop"])
            let propertyJSONTemplate = {};
            //we generate a template for our commodity dicts
            //names are left out because we use them as keys by which to access the commodity data
            for (idx in commodity) {
                if(idx != 'name') {
                    propertyJSONTemplate[idx] = '';
                }
            }
            //we save the template for use when we want to add new commodities
            CommodityObject.commodityPropertiesTemplate = propertyJSONTemplate;

            let i = 0;
            for (idx in commodity['name']) {
                let propertyJSON = JSON.parse(JSON.stringify(propertyJSONTemplate));
                for (feature_idx in commodity) {
                    if (feature_idx != 'name') {
                        propertyJSON[feature_idx] = commodity[feature_idx][i];
                    }
                }
                let name = commodity['name'][idx];
                CommodityObject.commodityPropertiesList[name] = propertyJSON;
                addCommToProcessCreationFormList(name)
                i++;
            }
            maptool_urbs_process.populateProcessEditorList('commodity', Object.keys(CommodityObject.commodityPropertiesList));
        });
    }

    /**
     * opens the GUI form for creating a new commodity
     */
    function openNewCommodityForm() {
        document.getElementById('urbsNewCommodityPopupForm').style.display = "block";
    }

    /**
     * closes the GUI form for creating a new commodity and resets its input values
     */
    function closeNewCommodityForm() {
        let form = document.getElementById("urbsNewCommodityPopupForm");
        form.style.display = "none";
        document.getElementById("newCommTextInput").value = '';
    }


    /**
     * makes sure we cannot create a commodity with now name and
     * enables or disables the confirm button in the new commodity form
     * 
     * @param {string} comm_name text contained in the input field for a new commodity name
     */
    function commodityFormCheckValidInput(comm_name) {
        if(comm_name != '') {
            document.getElementById("newCommCreateButton").disabled = false;
        }
        else {
            document.getElementById("newCommCreateButton").disabled = true;
        }

    }
    /**
     * Once a new commodity has been created, this function adds it to the GUI forms for creating new commodities and adding commodities to processes 
     * 
     * @param {string} name of the new commodity
     */
    function addCommToProcessCreationFormList(name) {
        let commSelect = document.getElementById("pro_propCommSelect");
        let commAddSelect = document.getElementById("pro_propAddCommSelect");

        let newOption = document.createElement("option");
        let newAddOption = document.createElement("option");

        newOption.value = name;
        newOption.text = name;
        newAddOption.value = name;
        newAddOption.text = name;
        commSelect.appendChild(newOption);
        commAddSelect.appendChild(newAddOption);
    }

    /**
     * Once a new commodity has been created, this function adds it to the GUI form of the storage editor
     * @param {string} name the name of the commodity
     */
    function addCommToStorageComList(name) {
        let commSelect = document.getElementById('storageFormDiv').querySelector('#commodity');
        let newOption = document.createElement("option");
        newOption.value = name;
        newOption.text = name;

        commSelect.appendChild(newOption);
        console.log(commSelect);
    }

    /**
     * adds the newly created commodity to the commodity editor list, the pro_conf table and the CommodityObject
     * 
     * @param {string} com_name the name of the commodity
     */
    function createNewCommodity(com_name) {
                //we grab the commodity list and add a new option. All values are blank at the start. We also add a blank entry to the Commodity Object List
                let commodityList = document.getElementById("commoditySelect");
                let option = document.createElement("option");
                option.text = com_name;
                option.value = com_name;
                commodityList.add(option);
                CommodityObject.commodityPropertiesList[com_name] = JSON.parse(JSON.stringify(CommodityObject.commodityPropertiesTemplate));
                
        
                addCommToProcessCreationFormList(com_name);
                addCommToStorageComList(com_name);

                closeNewCommodityForm();
    }
    /**
     * onchange function for all commodity editor input fields 
     * writes changed value back to the relevant entry in the CommodityObject
     * 
     * @param {event_target_object} target the html element whose onchange event called the function
     */
    function writeBackCommodityFeatures(target) {
        console.log(target);
        let idxInFeatureList = document.getElementById("commoditySelect").value;
        let selectedElement = CommodityObject.commodityPropertiesList[idxInFeatureList];
        selectedElement[target.name] = target.value;

        if(target.nodeName == 'SELECT') {
            if(target.value == 'Buy' || target.value == 'Sell') {
                document.getElementById('BuySellPriceButton').style.display = 'inline-block';     
            }
            else {
                document.getElementById('BuySellPriceButton').style.display='none';     
            }
        }
    }

    return {
        CommodityObject: CommodityObject,
        fetchCommodityProfiles: fetchCommodityProfiles,
        addCommToProcessCreationFormList: addCommToProcessCreationFormList,
        addCommToStorageComList: addCommToStorageComList,
        openNewCommodityForm: openNewCommodityForm,
        closeNewCommodityForm: closeNewCommodityForm,
        commodityFormCheckValidInput: commodityFormCheckValidInput,
        createNewCommodity: createNewCommodity,
        writeBackCommodityFeatures: writeBackCommodityFeatures,
    }

}();