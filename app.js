// --- STATE & CONFIG ---
let currentCity = 'istanbul';
let allVehicles = [];
let routeLayers = [];
let stationLayers = [];
let vehicleMarkers = new Map();
let activeFilters = {
  bus: true,
  ferry: true,
  metrobus: true,
  rail: true
};
let showRoutes = true;
let showStations = true;
let searchQuery = '';
let selectedVehicle = null;
let pollTimer = null;

// City Coordinates & Presets
const CITIES = {
  istanbul: {
    name: 'İstanbul',
    center: [41.015, 28.985],
    zoom: 13,
    metrobusLabel: 'Metrobüs',
    railLabel: 'Marmaray / Metro',
    presets: [
      { name: '🌉 Boğaziçi & Vapurlar', center: [41.025, 29.005], zoom: 14 },
      { name: '🛣️ Metrobüs (Mecidiyeköy)', center: [41.066, 28.99], zoom: 14 },
      { name: '🏛️ Tarihi Yarımada', center: [41.01, 28.97], zoom: 14 },
      { name: '🏙️ Kadıköy & Moda', center: [40.988, 29.025], zoom: 14 }
    ]
  },
  izmir: {
    name: 'İzmir',
    center: [38.43, 27.13],
    zoom: 13,
    metrobusLabel: 'Ekspres Otobüs',
    railLabel: 'İZBAN / Metro',
    presets: [
      { name: '🌊 İzmir Körfezi (Vapur)', center: [38.435, 27.12], zoom: 14 },
      { name: '🏛️ Konak Meydanı', center: [38.418, 27.128], zoom: 15 },
      { name: '🌴 Karşıyaka Sahil & Tram', center: [38.455, 27.11], zoom: 14 },
      { name: '🚆 Alsancak Gar & İZBAN', center: [38.439, 27.145], zoom: 15 }
    ]
  }
};

// Initialize Leaflet Map with Canvas Renderer for 60 FPS
const map = L.map('map', {
  center: CITIES.istanbul.center,
  zoom: CITIES.istanbul.zoom,
  preferCanvas: true,
  zoomControl: false
});

// Position zoom controls nicely
L.control.zoom({ position: 'bottomright' }).addTo(map);

// CARTO Dark Matter Tiles with API Key (CARTO requires ?key=)
const CARTO_API_KEY = 'cb1_3fqg_1_d721f84e7368d26560e12a09';
L.tileLayer(`https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=${CARTO_API_KEY}`, {
  attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OpenStreetMap',
  subdomains: 'abcd',
  maxZoom: 19
}).addTo(map);

// OpenRailwayMap Standard Layer (Worldwide OpenStreetMap Passenger Rail Infrastructure)
// Matches exact global railway geometries from trains.theojaffee.net
const railwayLayer = L.tileLayer('https://{s}.tiles.openrailwaymap.org/standard/{z}/{x}/{y}.png', {
  maxZoom: 19,
  opacity: 0.85,
  attribution: '&copy; <a href="https://www.openrailwaymap.org/">OpenRailwayMap</a>'
}).addTo(map);

// Canvas renderer for high-performance markers
const myRenderer = L.canvas({ padding: 0.5 });

// --- INITIALIZATION ---
document.addEventListener('DOMContentLoaded', () => {
  renderPresets();
  loadRoutes();
  fetchVehicles();
  
  // Refresh vehicle positions every 10 seconds
  pollTimer = setInterval(fetchVehicles, 10000);
});

// --- CITY SWITCHING ---
function switchCity(city) {
  if (city === currentCity) return;
  currentCity = city;

  // Update UI buttons
  document.getElementById('btn-istanbul').classList.toggle('active', city === 'istanbul');
  document.getElementById('btn-izmir').classList.toggle('active', city === 'izmir');
  document.getElementById('active-city-label').textContent = CITIES[city].name;
  document.getElementById('metrobus-label').textContent = CITIES[city].metrobusLabel;
  document.getElementById('rail-label').textContent = CITIES[city].railLabel;

  // Clear existing markers & layers
  clearVehicleMarkers();
  clearRouteLayers();
  closeDrawer();

  // Fly smoothly to new city
  map.flyTo(CITIES[city].center, CITIES[city].zoom, { duration: 1.5 });

  renderPresets();
  loadRoutes();
  fetchVehicles();
}

// --- PRESETS ---
function renderPresets() {
  const container = document.getElementById('quick-presets');
  container.innerHTML = '';
  CITIES[currentCity].presets.forEach(p => {
    const btn = document.createElement('button');
    btn.className = 'preset-pill';
    btn.textContent = p.name;
    btn.onclick = () => map.flyTo(p.center, p.zoom, { duration: 1.2 });
    container.appendChild(btn);
  });
}

// --- FETCH VEHICLES ---
async function fetchVehicles() {
  const countEl = document.getElementById('live-count-text');
  try {
    const res = await fetch(`/api/vehicles?city=${currentCity}`);
    if (!res.ok) throw new Error('API request failed');
    const data = await res.json();
    allVehicles = data.vehicles || [];

    // Update Counts
    document.getElementById('count-bus').textContent = (data.counts.buses || 0).toLocaleString();
    document.getElementById('count-ferry').textContent = (data.counts.ferries || 0).toLocaleString();
    document.getElementById('count-metrobus').textContent = (data.counts.metrobus || 0).toLocaleString();
    document.getElementById('count-rail').textContent = (data.counts.rail || 0).toLocaleString();
    countEl.textContent = `${data.total_count.toLocaleString()} Canlı Araç`;

    renderVehicles();
  } catch (err) {
    console.error('Fetch error:', err);
    countEl.textContent = 'Bağlantı Bekleniyor...';
  }
}

// --- RENDER VEHICLES ON MAP ---
function renderVehicles() {
  const q = searchQuery.toLowerCase().trim();
  const currentIds = new Set();

  allVehicles.forEach(v => {
    // Filter by mode
    if (!activeFilters[v.type] && !(v.type === 'metro' && activeFilters.rail) && !(v.type === 'tram' && activeFilters.rail)) {
      return;
    }

    // Filter by search query
    if (q) {
      const match = (v.line && v.line.toLowerCase().includes(q)) ||
                    (v.plate && v.plate.toLowerCase().includes(q)) ||
                    (v.door && v.door.toLowerCase().includes(q)) ||
                    (v.operator && v.operator.toLowerCase().includes(q));
      if (!match) return;
    }

    currentIds.add(v.id);
    const latlng = [v.lat, v.lon];

    if (vehicleMarkers.has(v.id)) {
      // Smoothly update existing marker position
      const marker = vehicleMarkers.get(v.id);
      marker.setLatLng(latlng);
      marker.vehicleData = v;
    } else {
      // Create new marker
      const marker = createMarkerForVehicle(v);
      marker.vehicleData = v;
      marker.addTo(map);
      marker.on('click', () => openDrawer(v));
      vehicleMarkers.set(v.id, marker);
    }
  });

  // Remove markers no longer in dataset or filtered out
  for (const [id, marker] of vehicleMarkers.entries()) {
    if (!currentIds.has(id)) {
      map.removeLayer(marker);
      vehicleMarkers.delete(id);
    }
  }
}

// --- CREATE MARKERS BY TYPE ---
function createMarkerForVehicle(v) {
  const latlng = [v.lat, v.lon];

  if (v.type === 'ferry') {
    // Rich SVG Boat Marker for Ferries
    const icon = L.divIcon({
      className: 'custom-ferry-marker',
      html: `
        <div style="background: #06b6d4; color: white; width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 14px rgba(6, 182, 212, 0.8); border: 2px solid #fff; font-size: 15px;">
          🚢
        </div>
      `,
      iconSize: [30, 30],
      iconAnchor: [15, 15]
    });
    return L.marker(latlng, { icon });
  } else if (v.type === 'rail' || v.type === 'metro') {
    // Rail / Metro Marker
    const icon = L.divIcon({
      className: 'custom-rail-marker',
      html: `
        <div style="background: #a855f7; color: white; width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 12px rgba(168, 85, 247, 0.8); border: 2px solid #fff; font-size: 13px;">
          🚆
        </div>
      `,
      iconSize: [26, 26],
      iconAnchor: [13, 13]
    });
    return L.marker(latlng, { icon });
  } else if (v.type === 'metrobus') {
    // High-performance bright Red Circle Marker for Metrobüs
    return L.circleMarker(latlng, {
      renderer: myRenderer,
      radius: 6,
      fillColor: '#f43f5e',
      fillOpacity: 0.95,
      color: '#ffffff',
      weight: 1.5
    });
  } else {
    // High-performance Amber Circle Marker for Normal Buses (Canvas 60 FPS)
    return L.circleMarker(latlng, {
      renderer: myRenderer,
      radius: 4.5,
      fillColor: '#fbbf24',
      fillOpacity: 0.85,
      color: '#d97706',
      weight: 1
    });
  }
}

function clearVehicleMarkers() {
  vehicleMarkers.forEach(marker => map.removeLayer(marker));
  vehicleMarkers.clear();
}

// --- ROUTES & STATIONS LAYERS ---
async function loadRoutes() {
  try {
    const res = await fetch('/static/routes.json');
    const data = await res.json();
    const cityData = data[currentCity];
    if (!cityData) return;

    // Draw Routes
    if (cityData.routes) {
      cityData.routes.forEach(r => {
        // Swap [lon, lat] to [lat, lon] for Leaflet
        const latlngs = r.coordinates.map(c => [c[1], c[0]]);
        const polyline = L.polyline(latlngs, {
          color: r.color,
          weight: r.type === 'ferry' ? 3 : 4,
          dashArray: r.type === 'ferry' ? '6, 8' : null,
          opacity: 0.75
        });

        polyline.bindTooltip(`<b>${r.name}</b>`, { sticky: true });
        routeLayers.push(polyline);
        if (showRoutes) polyline.addTo(map);
      });
    }

    // Draw Stations / Piers
    if (cityData.stations) {
      cityData.stations.forEach(s => {
        const isPier = s.type === 'pier';
        const icon = L.divIcon({
          className: 'station-marker',
          html: `
            <div style="background: ${isPier ? '#0284c7' : '#334155'}; color: white; width: 22px; height: 22px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 11px; border: 1.5px solid #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.5);">
              ${isPier ? '⚓' : '🚉'}
            </div>
          `,
          iconSize: [22, 22],
          iconAnchor: [11, 11]
        });

        const marker = L.marker([s.lat, s.lon], { icon });
        marker.bindTooltip(`<b>${s.name}</b><br><span style="font-size:11px;color:#94a3b8;">${isPier ? 'Vapur İskelesi' : 'İstasyon'}</span>`, { direction: 'top' });
        stationLayers.push(marker);
        if (showStations) marker.addTo(map);
      });
    }
  } catch (err) {
    console.error('Failed to load routes:', err);
  }
}

function clearRouteLayers() {
  routeLayers.forEach(l => map.removeLayer(l));
  stationLayers.forEach(l => map.removeLayer(l));
  routeLayers = [];
  stationLayers = [];
}

function toggleRailwayLayer(enabled) {
  if (enabled) {
    railwayLayer.addTo(map);
  } else {
    map.removeLayer(railwayLayer);
  }
}

function toggleRoutesLayer(enabled) {
  showRoutes = enabled;
  routeLayers.forEach(l => enabled ? l.addTo(map) : map.removeLayer(l));
}

function toggleStationsLayer(enabled) {
  showStations = enabled;
  stationLayers.forEach(l => enabled ? l.addTo(map) : map.removeLayer(l));
}

// --- FILTERS & SEARCH ---
function toggleFilter(type) {
  activeFilters[type] = !activeFilters[type];
  document.getElementById(`filter-${type}`).classList.toggle('active', activeFilters[type]);
  renderVehicles();
}

function handleSearch(val) {
  searchQuery = val;
  renderVehicles();
}

// --- VEHICLE DETAIL DRAWER ---
function openDrawer(v) {
  selectedVehicle = v;
  const drawer = document.getElementById('detail-drawer');

  const icons = {
    bus: '🚌',
    metrobus: '🔴',
    ferry: '🚢',
    rail: '🚆',
    metro: '🚇'
  };

  document.getElementById('drawer-icon').textContent = icons[v.type] || '🚌';
  document.getElementById('drawer-line').textContent = v.line || 'Hat Bilgisi';
  document.getElementById('drawer-operator').textContent = v.operator || 'Operatör';
  document.getElementById('drawer-plate').textContent = v.plate || 'Bilinmiyor';
  document.getElementById('drawer-door').textContent = v.door || '—';
  document.getElementById('drawer-speed').textContent = `${Math.round(v.speed || 0)} km/h`;
  document.getElementById('drawer-time').textContent = v.time || 'Canlı';

  drawer.classList.add('open');
}

function closeDrawer() {
  document.getElementById('detail-drawer').classList.remove('open');
  selectedVehicle = null;
}

function focusOnVehicle() {
  if (selectedVehicle) {
    map.flyTo([selectedVehicle.lat, selectedVehicle.lon], 16, { duration: 1.0 });
  }
}

// --- INFO MODAL HANDLERS ---
function openInfoModal() {
  const modal = document.getElementById('info-modal');
  if (modal) modal.classList.add('open');
}

function closeInfoModal() {
  const modal = document.getElementById('info-modal');
  if (modal) modal.classList.remove('open');
}
