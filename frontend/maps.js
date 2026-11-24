/* Google renders independent CityLens records. Places coordinates never enter here. */
(() => {
  'use strict';
  let loading;
  async function loadGoogle(key) {
    if (window.google?.maps?.Map) return;
    if (!loading) loading=new Promise((resolve,reject)=>{
      const callback='citylensGoogleReady';
      const timer=setTimeout(()=>reject(new Error('Google Maps did not load. Check the browser key and its referrer/API restrictions.')),12000);
      window[callback]=()=>{clearTimeout(timer);resolve();};
      const script=document.createElement('script');
      script.src='https://maps.googleapis.com/maps/api/js?'+new URLSearchParams({key,callback,libraries:'marker',loading:'async',v:'weekly'});
      script.async=true;script.onerror=()=>{clearTimeout(timer);reject(new Error('Google Maps could not load.'));};
      document.head.append(script);
    });
    return loading;
  }

  class GoogleExplorerMap {
    constructor(target,config) {
      this.reports=[];this.analysis=[];this.scope=[];
      this.map=new google.maps.Map(target,{center:{lat:32.88,lng:-117.20},zoom:10,
        mapId:config.google_maps_map_id || 'DEMO_MAP_ID',streetViewControl:false,mapTypeControl:false,fullscreenControl:true});
    }
    on(event,callback){this.map.addListener(event==='moveend'?'idle':event,callback);}
    getZoom(){return this.map.getZoom();}
    getBounds(){
      const bounds=this.map.getBounds();
      if (!bounds) return {getWest:()=>-117.6,getEast:()=>-116.8,getSouth:()=>32.5,getNorth:()=>33.2};
      return {getWest:()=>bounds.getSouthWest().lng(),getSouth:()=>bounds.getSouthWest().lat(),
        getEast:()=>bounds.getNorthEast().lng(),getNorth:()=>bounds.getNorthEast().lat()};
    }
    clear(objects){for(const object of objects){if(object.setMap)object.setMap(null);else object.map=null;}objects.length=0;}
    marker(position,text,kind,click){
      const node=document.createElement('div');node.className='google-report-marker '+kind;node.textContent=text;
      const marker=new google.maps.marker.AdvancedMarkerElement({map:this.map,position,content:node,title:kind==='cluster'?`${text} nearby source reports`:text || 'Source report'});
      if(click)marker.addListener('click',click);
      return marker;
    }
    drawReports(result,onDetail){
      this.clear(this.reports);
      for(const item of result.items){
        const position={lat:item.lat,lng:item.lng};
        const grouped=result.mode==='clusters';
        this.reports.push(this.marker(position,grouped?String(item.count):'',grouped?'cluster':'point',()=>{
          if(grouped){this.map.panTo(position);this.map.setZoom(Math.min(19,this.getZoom()+2));}
          else onDetail(item.id);
        }));
      }
    }
    /* Explorer analysis area: separate from Agent overlays; never refits the map. */
    drawScope(bounds){
      this.clear(this.scope);
      if(!bounds)return;
      this.scope.push(new google.maps.Rectangle({map:this.map,bounds,clickable:false,strokeColor:'#df785e',
        strokeOpacity:.9,strokeWeight:2,fillOpacity:0}));
    }
    drawAnalysis(visualization){
      this.clear(this.analysis);
      if(!visualization)return;
      const focus=new google.maps.LatLngBounds();let hasBounds=false;
      if(visualization.mode==='radius'){
        for(const circle of visualization.circles || []){
          const position={lat:circle.latitude,lng:circle.longitude};
          const shape=new google.maps.Circle({map:this.map,center:position,radius:circle.radius_meters,
            strokeColor:'#226c76',strokeWeight:2,fillColor:'#226c76',fillOpacity:.09});
          this.analysis.push(shape,this.marker(position,circle.location_id,'analysis',null));
          focus.union(shape.getBounds());hasBounds=true;
        }
      }else if(visualization.mode==='viewport' && visualization.bounds){
        const b=visualization.bounds;
        const shape=new google.maps.Rectangle({map:this.map,bounds:b,strokeColor:'#226c76',strokeWeight:2,fillColor:'#226c76',fillOpacity:.06});
        this.analysis.push(shape);focus.extend({lat:b.south,lng:b.west});focus.extend({lat:b.north,lng:b.east});hasBounds=true;
      }
      if(hasBounds)this.map.fitBounds(focus,36);
    }
  }

  /* Open-map fallback with the same interface, using the vendored Leaflet build. */
  let leafletLoading;
  function loadLeaflet(){
    if(window.L?.map)return Promise.resolve();
    if(!leafletLoading)leafletLoading=new Promise((resolve,reject)=>{
      const css=document.createElement('link');css.rel='stylesheet';css.href='/assets/vendor/leaflet.css';document.head.append(css);
      const script=document.createElement('script');script.src='/assets/vendor/leaflet.js';
      script.onload=()=>resolve();script.onerror=()=>reject(new Error('The open map could not load.'));document.head.append(script);
    });
    return leafletLoading;
  }
  class LeafletExplorerMap {
    constructor(target){
      this.provider='open_map';this.reports=L.layerGroup();this.analysis=L.layerGroup();this.scope=L.layerGroup();
      this.map=L.map(target,{preferCanvas:true}).setView([32.88,-117.20],11);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
        attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(this.map);
      this.reports.addTo(this.map);this.analysis.addTo(this.map);this.scope.addTo(this.map);
    }
    on(event,callback){this.map.on(event,callback);}
    getZoom(){return this.map.getZoom();}
    getBounds(){return this.map.getBounds();}
    drawReports(result,onDetail){
      this.reports.clearLayers();
      for(const item of result.items){
        if(result.mode==='clusters'){
          // Count is coerced to a number before entering the icon HTML.
          const count=Number(item.count).toLocaleString('en-US');
          const marker=L.marker([item.lat,item.lng],{title:`${count} nearby source reports`,
            icon:L.divIcon({className:'leaflet-report-cluster',html:count,iconSize:[34,34]})});
          marker.on('click',()=>this.map.setView([item.lat,item.lng],Math.min(19,this.getZoom()+2)));
          this.reports.addLayer(marker);
        }else{
          const point=L.circleMarker([item.lat,item.lng],{radius:5,color:'#ffffff',weight:1.5,fillColor:'#d26d3d',fillOpacity:.9});
          point.on('click',()=>onDetail(item.id));this.reports.addLayer(point);
        }
      }
    }
    drawScope(bounds){
      this.scope.clearLayers();
      if(!bounds)return;
      this.scope.addLayer(L.rectangle([[bounds.south,bounds.west],[bounds.north,bounds.east]],
        {color:'#df785e',weight:2,dashArray:'6 5',fill:false,interactive:false}));
    }
    drawAnalysis(visualization){
      this.analysis.clearLayers();
      if(!visualization)return;
      let bounds=null;
      if(visualization.mode==='radius'){
        for(const circle of visualization.circles || []){
          const shape=L.circle([circle.latitude,circle.longitude],{radius:circle.radius_meters,color:'#226c76',weight:2,fillOpacity:.09});
          shape.bindTooltip('Location '+circle.location_id,{permanent:false});
          this.analysis.addLayer(shape);
          bounds=bounds?bounds.extend(shape.getBounds()):shape.getBounds();
        }
      }else if(visualization.mode==='viewport' && visualization.bounds){
        const b=visualization.bounds;
        const shape=L.rectangle([[b.south,b.west],[b.north,b.east]],{color:'#226c76',weight:2,fillOpacity:.06});
        this.analysis.addLayer(shape);bounds=shape.getBounds();
      }
      if(bounds)this.map.fitBounds(bounds,{padding:[36,36]});
    }
  }

  window.CityLensMap={async create(target,config){
    if(config.google_maps_browser_api_key){
      try{await loadGoogle(config.google_maps_browser_api_key);const map=new GoogleExplorerMap(target,config);map.provider='google';return map;}
      catch(error){console.warn(error.message+' Falling back to the open map.');}
    }
    try{await loadLeaflet();return new LeafletExplorerMap(target);}
    catch(_){target.textContent='The map could not load. Explorer records and investigations remain available.';target.classList.add('map-placeholder');return null;}
  }};
})();
