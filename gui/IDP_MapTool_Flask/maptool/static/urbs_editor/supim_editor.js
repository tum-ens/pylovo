var maptool_urbs_supim = function() {
  let SupimObject = {
      "solar" : {},
      'bus_supim': {}
  } 

  /**
   * retrieves supim profiles
   * 
   * @returns Promise to make sure functions during setup are called in order
   */
  function fetchSupimProfiles() {
      return fetch('urbs/supim_profiles')
      .then(function (response) {
          return response.json();
      }).then(function (supim_data) {
          SupimObject.solar = JSON.parse(supim_data['solar']);
          
        let i = 0;
        for (supimTable in SupimObject) { 
            if(supimTable != 'bus_supim') {
                delete SupimObject[supimTable]['t'];
                populateSupimEditor(SupimObject[supimTable], supimTable, i);
                i++;
            }
        }

        let listLength = maptool_urbs_buildings.BuildingsObject['busWithLoadList'].length;
        SupimObject.bus_supim = new Array(listLength);
        //checkbox values are stored in an array as strings of 0 and 1
        //each array entry contains one string and represents all checkboxes for one profile
        for (let i = 0; i < listLength; i++) {
            SupimObject.bus_supim[i] = new Array(1).fill('0'.repeat(Object.keys(SupimObject.solar).length));
        }
    })
  }

  /**
   * generates html elements for the checkboxes
   * 
   * @param {list} data  list containing all possible time series of a profile, could be swapped out with just length value tbh
   * @param {string} name key for getting the correct html div element and setting the checkbox onclick function
   * @param {int} index secondary key for setting the checkbox onclick function
   */
  function populateSupimEditor (data, name, index) {
      let testDiv = document.getElementById(name + "Panel")
      for (key in data) {
          let checkbox = document.createElement('INPUT');
          checkbox.setAttribute("type", "checkbox");
          checkbox.setAttribute("name", "checkbox_" + key);
          checkbox.setAttribute("onclick", "maptool_urbs_supim.check_uncheck_supim(this, '" + name + "', " + key + ", '" + index + "')");
          
          let label = document.createElement('LABEL')
          label.appendChild(checkbox);
          label.insertAdjacentHTML("beforeend", key)
          label.for = "checkbox_" + key;
  
          testDiv.appendChild(label);
      }
  }

  /**
   * when the user selects an element in the ui in the supim tab, this function sets all checkboxes for that element to the right value
   * 
   * @param {event_target_object} target html object whose onchange event triggered the function call
   */
  function fillSelectedFeatureSupimEditor(target) {
      let sel = document.getElementById('supimSelect');
      maptool_urbs_setup.resetLoadBusStyle(target)
      let dict_index = 0;
      for (table in SupimObject) {
          if(table != 'bus_supim') {
              let checkboxDiv = document.getElementById(table + "Panel");
              let chars = SupimObject.bus_supim[sel.selectedIndex][dict_index]
              for (let i = 0;  i < checkboxDiv.children.length; i++) {
                  if (chars[i] == '1') {
                      checkboxDiv.children[i].firstChild.checked = true;
                      check_uncheck_supim(checkboxDiv.children[i].firstChild, table, i, dict_index);
                  }
                  else {
                      let wasTrue = checkboxDiv.children[i].firstChild.checked;
                      checkboxDiv.children[i].firstChild.checked = false;
                      if(wasTrue) {
                        check_uncheck_supim(checkboxDiv.children[i].firstChild, table, i, dict_index);
                      }
                  }
              }
              dict_index++
          }
      }
  }

  let charts = [];
  
  /**
   * removes or adds a graph to the demand chart and marks whether the checkbox is set in the DemandObject
   * 
   * @param {HTML_element} checkbox checkbox html element whose onchange event triggered the function calls
   * @param {string} demand_type which demand category the checkbox belongs to
   * @param {int} key the index of the checkbox in the list
   * @param {int} demandIndex the index of the demand category 
   */
  function check_uncheck_supim(checkbox, demand_type, key, demandIndex) {
      let listElem = document.getElementById('supimSelect').selectedIndex;
      let chars = SupimObject.bus_supim[listElem][demandIndex].split('')
      if (checkbox.checked) {
          let data = SupimObject[demand_type][key];
  
          let newgraph = {
          name: '' + key,
          type: 'line',
          stack: 'Total',
          areaStyle: {},
          emphasis: {
              focus: 'series'
          },
          data: Object.values(data)
          };
          let option = charts[demandIndex].getOption();
          option.series.push(newgraph);
          option.legend[0].data.push(newgraph.name);
          charts[demandIndex].setOption(option);
  
          chars[key] = '1';
  
      }
      else {
          let option = charts[demandIndex].getOption();
          let index = option.series.findIndex(data => data.name === 'Test' + checkbox.name);
          option.series.splice(index, 1);
  
          index = option.legend[0].data.findIndex(name => name === 'Test' + checkbox.name);
          option.legend[0].data.splice(index, 1);
  
          charts[demandIndex].setOption(option, true);
          chars[key] = '0';
      }
      SupimObject.bus_supim[listElem][demandIndex] = chars.join('');
  }
  
  let graphs = document.getElementsByClassName("feature-editor__selected-feature-editor__supim__graph");
  for (let i = 0; i < graphs.length; i++) {
      var myChart = echarts.init(graphs[i]);
      // Specify the configuration items and data for the chart
      var option = {
          tooltip: {
            trigger: 'axis',
            axisPointer: {
              type: 'cross',
              label: {
                backgroundColor: '#6a7985'
              }
            }
          },
          legend: {
            data: []
          },
          toolbox: {
            feature: {
              saveAsImage: {}
            }
          },
          grid: {
            left: '3%',
            right: '4%',
            bottom: '3%',
            containLabel: true
          },
          xAxis: [
            {
              type: 'category',
              boundaryGap: false,
              data: Array.from(Array(8760).keys())
          }
          ],
          yAxis: [
            {
              type: 'value'
            }
          ],
          series: [{showSymbol: false}]
        };
  
      // Display the chart using the configuration items and data just specified.
      myChart.setOption(option);
      charts.push(myChart);
  }


  return {
      SupimObject: SupimObject,
      fetchSupimProfiles: fetchSupimProfiles, 
      check_uncheck_supim: check_uncheck_supim,
      fillSelectedFeatureSupimEditor: fillSelectedFeatureSupimEditor
  };
}();