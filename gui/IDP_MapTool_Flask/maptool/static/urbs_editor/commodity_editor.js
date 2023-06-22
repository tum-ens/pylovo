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
    function fetchProfiles() {
        fetch('urbs/commodity_profiles')
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

    function createBuySellPriceEditor() {

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
     * @param {string} name of the new commodity
     */
    //TODO: There are a few lists that still need to be included here
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
     * adds the newly created commodity to the commodity editor list, the pro_conf table and the CommodityObject
     * @param {string} com_name 
     */
    function createNewCommodity(com_name) {
                //we grab the commodity list and add a new option. All values are blank at the start. We also add a blank entry to the Commodity Object List
                let commodityList = document.getElementById("commoditySelect");
                let option = document.createElement("option");
                option.text = com_name;
                option.value = com_name;
                commodityList.add(option);
                CommodityObject.commodityPropertiesList[com_name] = JSON.parse(JSON.stringify(CommodityObject.commodityPropertiesTemplate));
                
                //we insert a new column into the commodity table
                maptool_urbs_process.hot.alter('insert_col', maptool_urbs_process.hot.countCols(), 1)
                maptool_urbs_process.hot.headers[maptool_urbs_process.hot.headers.length - 1] = com_name;
        
                //TODO: Replace with addCommToProcessCreationFormList function
                let pro_propCommOption = document.createElement("option");
                pro_propCommOption.text = com_name;
                pro_propCommOption.value = com_name;
                document.getElementById('pro_propCommSelect').add(pro_propCommOption);

                let pro_propAddCommOption = document.createElement("option");
                pro_propAddCommOption.text = com_name;
                pro_propAddCommOption.value = com_name;
                document.getElementById('pro_propAddCommSelect').add(pro_propAddCommOption);

                closeNewCommodityForm();
    }
    /**
     * onchange function for all commodity editor input fields 
     * writes changed value back to the relevant entry in the CommodityObject
     * @param {event target object} target 
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
        fetchProfiles: fetchProfiles,
        createBuySellPriceEditor: createBuySellPriceEditor,
        openNewCommodityForm: openNewCommodityForm,
        closeNewCommodityForm: closeNewCommodityForm,
        commodityFormCheckValidInput: commodityFormCheckValidInput,
        createNewCommodity: createNewCommodity,
        writeBackCommodityFeatures: writeBackCommodityFeatures,
    }

}();