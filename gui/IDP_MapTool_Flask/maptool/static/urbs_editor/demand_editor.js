var maptool_urbs_demand = function () {
  let DemandObject = {
    "demand_electricity" : {},
    "demand_electricity_reactive" : {},
    "demand_mobility" : {},
    "demand_space_heat" : {},
    "demand_water_heat" : {},
    "bus_demands" : []
  }

  function fetchDemandProfiles() {
    return fetch('urbs/demand_profiles')
    .then(function (response) {
        return response.json();
    }).then(function (demand_data) {
        DemandObject.demand_electricity = JSON.parse(demand_data['demand_electricity']);
        DemandObject.demand_electricity_reactive = JSON.parse(demand_data['demand_electricity_reactive']);
        DemandObject.demand_mobility = JSON.parse(demand_data['demand_mobility']);
        DemandObject.demand_space_heat = JSON.parse(demand_data['demand_space_heat']);
        DemandObject.demand_water_heat = JSON.parse(demand_data['demand_water_heat']);
        
        let i = 0;
        for (demandTable in DemandObject) { 
            if(demandTable != 'bus_demands') {
                delete maptool_urbs_demand.DemandObject[demandTable]['t'];
                populateDemandEditor(DemandObject[demandTable], demandTable, i);
                i++;
            }
        }

        let listLength = maptool_urbs_buildings.BuildingsObject['busWithLoadList'].length;
        DemandObject.bus_demands = new Array(listLength);

        let lengths = [Object.keys(DemandObject.demand_electricity).length, 
                  Object.keys(DemandObject.demand_electricity_reactive).length,
                  Object.keys(DemandObject.demand_mobility).length,
                  Object.keys(DemandObject.demand_space_heat).length,
                  Object.keys(DemandObject.demand_water_heat).length]

        for (let i = 0; i < listLength; i++) {
            DemandObject.bus_demands[i] = lengths.map((i => length => '0'.repeat(length - 1))(0));
            //DemandObject.bus_demands[i] = new Array(5).fill('0'.repeat(Object.keys(DemandObject.demand_electricity).length - 1));
        }
    })
  }

  
  function fillSelectedFeatureDemandEditor(target) {
      let sel = document.getElementById('demandSelect');
      maptool_urbs_setup.resetLoadBusStyle(target)
      let demandIndex = 0;
      for (demandTable in DemandObject) {
          if(demandTable != 'bus_demands') {
              let checkboxDiv = document.getElementById(demandTable + "Panel");
              let chars = DemandObject.bus_demands[sel.selectedIndex][demandIndex]
              for (let i = 0;  i < checkboxDiv.children.length; i++) {
                  if (chars[i] == '1') {
                      checkboxDiv.children[i].firstChild.checked = true;
                      check_uncheck_demand(checkboxDiv.children[i].firstChild, demandTable, i, demandIndex);
                  }
                  else {
                      let wasTrue = checkboxDiv.children[i].firstChild.checked;
                      checkboxDiv.children[i].firstChild.checked = false;
                      if(wasTrue) {
                          check_uncheck_demand(checkboxDiv.children[i].firstChild, demandTable, i, demandIndex);
                      }
                  }
              }
              demandIndex++
          }
      }
  }
  
  let charts = [];
  
  function populateDemandEditor (demand_data, demandName, demandIndex) {
      let testDiv = document.getElementById(demandName + "Panel")
      for (let key = 0; key < Object.keys(demand_data).length - 1; key++) {
        let checkbox = document.createElement('INPUT');
        checkbox.setAttribute("type", "checkbox");
        checkbox.setAttribute("name", "checkbox_" + key);
        checkbox.setAttribute("onclick", "maptool_urbs_demand.check_uncheck_demand(this, '" + demandName + "', " + key + ", '" + demandIndex + "')");
        
        let label = document.createElement('LABEL')
        label.appendChild(checkbox);
        label.insertAdjacentHTML("beforeend", key)
        label.for = "checkbox_" + key;

        testDiv.appendChild(label);
      }
  }
  
  
  function check_uncheck_demand(checkbox, demand_type, key, demandIndex) {
      let listElem = document.getElementById('demandSelect').selectedIndex;
      let chars = DemandObject.bus_demands[listElem][demandIndex].split('')
      if (checkbox.checked) {
          let data = DemandObject[demand_type][key];
  
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
      DemandObject.bus_demands[listElem][demandIndex] = chars.join('');
  }
  
  var acc = document.getElementsByClassName("accordion");
  var i;
  
  for (i = 0; i < acc.length; i++) {
    acc[i].addEventListener("click", function() {
      /* Toggle between adding and removing the "active" class,
      to highlight the button that controls the panel */
      this.classList.toggle("active");
      /* Toggle between hiding and showing the active panel */
      var panel = this.nextElementSibling;
      if (panel.style.display === "block") {
        panel.style.display = "none";
      } else {
        panel.style.display = "block";
      }
    });
  }
  
  let graphs = document.getElementsByClassName("feature-editor__selected-feature-editor__demand__graph");
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
    DemandObject : DemandObject,
    fetchDemandProfiles: fetchDemandProfiles,
    fillSelectedFeatureDemandEditor: fillSelectedFeatureDemandEditor, 
    populateDemandEditor: populateDemandEditor,
    check_uncheck_demand, check_uncheck_demand
  }
}();