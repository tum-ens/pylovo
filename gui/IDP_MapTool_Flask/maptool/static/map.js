var map;

const tileProvider = 'http://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'
const  mapOptions = {
    center: [49.717189494, 11.0392827],
    zoom: 11,
    pmIgnore: false
    }
    
if (map == undefined) {
    map = new L.map('map', mapOptions);
    L.tileLayer(tileProvider, {
        maxZoom: 19,
        attribution: '&copy; <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(map);
}

if (window.location.pathname == '/networks') {
    var legend = L.control({position: 'bottomleft'});
    legend.onAdd = function (map) {
    let colors = ['#0065BD', '#007deb', '#42bd4a', '#42bd4a', '#e6b029'];
    var div = L.DomUtil.create('div', 'info legend'),
    labels = ['<strong>Features</strong>'],
    categories = ['Bus','Line','Trafo','Trafo3w','Ext. Grid'];

    for (var i = 0; i < categories.length; i++) {

            div.innerHTML += 
            labels.push(
                '<i class="dot" style="background:' + colors[i] + '"></i> ' + (categories[i] ? categories[i] : '+'));

        }
        div.innerHTML = labels.join('<br>');
    return div;
    };
    legend.addTo(map);
}

//We remove all preexisting options execpt quad, circle and polygon (might only use polygon for ease tbh)
if (window.location.pathname == '/') {
    map.pm.addControls({  
        position: 'topleft',  
        drawPolyline: false,  
        drawMarker: false,
        drawCircleMarker: false,
        drawText: false,
        cutPolygon: false
    });  
}

//on clicking on an element, we display information of the selected node in our sidebar for editing
map.on('popupopen', function(e) {
    //map.closePopup();
    //var marker = e.popup._source;
    //console.log(marker.features.properties);
  });

  document.onkeydown = function(evt) {
    evt = evt || window.event;
    var isEscape = false;
    if ("key" in evt) {
        isEscape = (evt.key === "Escape" || evt.key === "Esc");
    } else {
        isEscape = (evt.keyCode === 27);
    }
    if (isEscape) {
        map.pm.disableDraw();
    }
};