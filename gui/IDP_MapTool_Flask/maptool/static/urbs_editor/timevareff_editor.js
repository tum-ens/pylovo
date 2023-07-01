var maptool_urbs_timevareff = function() {
    TimevareffObject = {
        "charging_station" : {},
        "heatpump_air" : {},
        "heatpump_air_heizstrom" : {},
        "bus_timevareff" : []
    }

    
    function fetchFeatureProfiles() {
        return fetch('urbs/timevareff_profiles')
        .then(function (response) {
            return response.json();
        }).then(function (data) {
            TimevareffObject.charging_station = JSON.parse(data['charging_station']);
            TimevareffObject.heatpump_air = JSON.parse(data['heatpump_air']);
            TimevareffObject.heatpump_air_heizstrom = JSON.parse(data['heatpump_air_heizstrom']);

            let i = 0;
            for (featureDict in TimevareffObject) { 
                if(featureDict != 'bus_timevareff') {
                    delete TimevareffObject[featureDict]['t'];
                    populatTimevareffEditor(TimevareffObject[featureDict], featureDict, i);
                    i++;
                }
            }

            let listLength = maptool_urbs_buildings.BuildingsObject['busWithLoadList'].length;
            TimevareffObject.bus_timevareff = new Array(listLength);

            let lengths = [Object.keys(TimevareffObject.charging_station).length, 
              Object.keys(TimevareffObject.heatpump_air).length,
              Object.keys(TimevareffObject.heatpump_air_heizstrom).length,
]

            for (let i = 0; i < listLength; i++) {
              TimevareffObject.bus_timevareff[i] = lengths.map((i => length => '0'.repeat(length - 1))(0));
              //TimevareffObject.bus_timevareff[i] = new Array(3).fill('0'.repeat(Object.keys(TimevareffObject.charging_station).length));
            }
        });
    }

    function fillSelectedFeatureTimevareffEditor(target) {
      let sel = document.getElementById('timevareffSelect');
      maptool_urbs_setup.resetLoadBusStyle(target);
      let dict_index = 0;
      for (table in TimevareffObject) {
          if(table != 'bus_timevareff') {
              let checkboxDiv = document.getElementById(table + "Panel");
              let chars = TimevareffObject.bus_timevareff[sel.selectedIndex][dict_index];
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

    function check_uncheck_demand(checkbox, demand_type, key, demandIndex) {
        let listElem = document.getElementById('timevareffSelect').selectedIndex;
        let chars = TimevareffObject.bus_timevareff[listElem][demandIndex].split('')
        if (checkbox.checked) {
            let data = TimevareffObject[demand_type][key];
    
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
        TimevareffObject.bus_timevareff[listElem][demandIndex] = chars.join('');
    }

    function populatTimevareffEditor (data, name, index) {
        let testDiv = document.getElementById(name + "Panel")
        for (key in data) {
            let checkbox = document.createElement('INPUT');
            checkbox.setAttribute("type", "checkbox");
            checkbox.setAttribute("name", "checkbox_" + key);
            checkbox.setAttribute("onclick", "maptool_urbs_timevareff.check_uncheck_demand(this, '" + name + "', " + key + ", '" + index + "')");
            
            let label = document.createElement('LABEL')
            label.appendChild(checkbox);
            label.insertAdjacentHTML("beforeend", key)
            label.for = "checkbox_" + key;
    
            testDiv.appendChild(label);
        }
    }
    let charts = [];

    let graphs = document.getElementsByClassName("feature-editor__selected-feature-editor__timevareff__graph");
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
        TimevareffObject: TimevareffObject,
        fetchFeatureProfiles: fetchFeatureProfiles,
        check_uncheck_demand: check_uncheck_demand,
        fillSelectedFeatureTimevareffEditor: fillSelectedFeatureTimevareffEditor
    };

}();