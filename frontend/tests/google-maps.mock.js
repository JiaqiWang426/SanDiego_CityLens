/* Test-only Maps SDK shim. It verifies UI wiring, not Google's actual SDK. */
(() => {
  class Bounds{
    constructor(){this.west=Infinity;this.east=-Infinity;this.south=Infinity;this.north=-Infinity;}
    extend(point){this.west=Math.min(this.west,point.lng);this.east=Math.max(this.east,point.lng);this.south=Math.min(this.south,point.lat);this.north=Math.max(this.north,point.lat);return this;}
    union(bounds){this.extend({lng:bounds.west,lat:bounds.south});this.extend({lng:bounds.east,lat:bounds.north});return this;}
    getSouthWest(){return {lng:()=>this.west,lat:()=>this.south};}getNorthEast(){return {lng:()=>this.east,lat:()=>this.north};}
  }
  class Map{
    constructor(target,options){this.target=target;this.options=options;this.zoom=options.zoom;this.listeners={};this.bounds=new Bounds().extend({lat:32.75,lng:-117.4}).extend({lat:33,lng:-117.05});target.dataset.mapProvider='google-maps-mock';target.style.position='relative';target.style.backgroundColor='#e4ebe2';target.style.backgroundImage='linear-gradient(35deg,transparent 46%,#fcfcf6 46%,#fcfcf6 49%,transparent 49%),linear-gradient(115deg,transparent 47%,#fcfcf6 47%,#fcfcf6 50%,transparent 50%)';target.style.backgroundSize='75px 90px';const label=document.createElement('small');label.textContent='Google Maps · test fixture';label.style.cssText='position:absolute;bottom:2px;right:5px;color:#678;font:8px sans-serif';target.append(label);}
    addListener(name,callback){this.listeners[name]=callback;if(name==='idle')setTimeout(callback,5);return {remove(){}};}
    getBounds(){return this.bounds;}getZoom(){return this.zoom;}setZoom(z){this.zoom=z;setTimeout(()=>this.listeners.idle?.(),5);}panTo(){}fitBounds(b){this.bounds=b;setTimeout(()=>this.listeners.idle?.(),5);}
  }
  class Circle{
    constructor(options){this.options=options;this.element=document.createElement('div');this.element.style.cssText='position:absolute;border:2px solid #22727b;border-radius:50%;width:85px;height:85px;left:35%;top:30%;background:#22727b17';options.map?.target.append(this.element);}
    getBounds(){const c=this.options.center,d=this.options.radius/111000;return new Bounds().extend({lat:c.lat-d,lng:c.lng-d}).extend({lat:c.lat+d,lng:c.lng+d});}
    setMap(){this.element.remove();}
  }
  class Rectangle{
    constructor(options){this.options=options;this.element=document.createElement('div');this.element.style.cssText='position:absolute;border:2px solid #22727b;left:12%;top:15%;width:72%;height:64%;background:#22727b0a';options.map?.target.append(this.element);}
    setMap(){this.element.remove();}
  }
  class Marker{
    constructor(options){this.options=options;this.content=options.content;this._map=options.map;this.content.style.cssText='position:absolute;left:49%;top:47%;z-index:2';options.map?.target.append(this.content);}
    set map(value){if(!value)this.content.remove();this._map=value;}get map(){return this._map;}
    addListener(name,callback){this.content.addEventListener(name,callback);}
  }
  window.google={maps:{Map,LatLngBounds:Bounds,Circle,Rectangle,marker:{AdvancedMarkerElement:Marker}}};
  const callback=new URL(document.currentScript.src).searchParams.get('callback');
  if(callback)window[callback]();
})();
