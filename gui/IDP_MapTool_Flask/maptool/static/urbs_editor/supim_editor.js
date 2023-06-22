var maptool_urbs_supim = function() {
  let SupimObject = {
      "solar" : {},
      'bus_supim': {}
  } 

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
        for (let i = 0; i < listLength; i++) {
            SupimObject.bus_supim[i] = new Array(1).fill('0'.repeat(Object.keys(SupimObject.solar).length));
        }
    })
  }

  function populateSupimEditor (data, name, index) {
      let testDiv = document.getElementById(name + "Panel")
      for (key in data) {
          let checkbox = document.createElement('INPUT');
          checkbox.setAttribute("type", "checkbox");
          checkbox.setAttribute("name", "checkbox_" + key);
          checkbox.setAttribute("onclick", "maptool_urbs_supim.check_uncheck_demand(this, '" + name + "', " + key + ", '" + index + "')");
          
          let label = document.createElement('LABEL')
          label.appendChild(checkbox);
          label.insertAdjacentHTML("beforeend", key)
          label.for = "checkbox_" + key;
  
          testDiv.appendChild(label);
      }
  }

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
                      check_uncheck_demand(checkboxDiv.children[i].firstChild, table, i, dict_index);
                  }
                  else {
                      let wasTrue = checkboxDiv.children[i].firstChild.checked;
                      checkboxDiv.children[i].firstChild.checked = false;
                      if(wasTrue) {
                          check_uncheck_demand(checkboxDiv.children[i].firstChild, table, i, dict_index);
                      }
                  }
              }
              dict_index++
          }
      }
  }

  let charts = [];

  function check_uncheck_demand(checkbox, demand_type, key, demandIndex) {
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
          series: []
        };
  
      // Display the chart using the configuration items and data just specified.
      myChart.setOption(option);
      charts.push(myChart);
  }


  return {
      SupimObject: SupimObject,
      fetchSupimProfiles: fetchSupimProfiles, 
      check_uncheck_demand: check_uncheck_demand,
      fillSelectedFeatureSupimEditor: fillSelectedFeatureSupimEditor
  };
}();