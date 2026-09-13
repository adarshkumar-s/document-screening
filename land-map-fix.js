(() => {
  "use strict";

  const geographyByParcel = {
    "DEMO-103-A": { district: "Demo District", taluka: "Demo Taluka", village: "Demo Village" },
    "DEMO-103-B": { district: "Demo District", taluka: "Demo Taluka", village: "Demo Village" },
    "DEMO-104": { district: "Demo District", taluka: "Demo Taluka", village: "Demo Village" },
    "DEMO-105": { district: "Demo District", taluka: "Demo Taluka", village: "Demo Village" }
  };

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    try {
      const requestUrl = typeof args[0] === "string" ? args[0] : args[0]?.url || "";
      if (!requestUrl.includes("/api/demo-land/geojson")) return response;
      const payload = await response.clone().json();
      if (!payload || !Array.isArray(payload.features)) return response;
      return new Response(JSON.stringify({
        ...payload,
        features: payload.features.map(feature => {
          const props = { ...(feature.properties || {}) };
          return { ...feature, properties: { ...(geographyByParcel[props.parcel_id] || {}), ...props } };
        })
      }), { status: response.status, statusText: response.statusText, headers: new Headers(response.headers) });
    } catch (_) { return response; }
  };

  // The OSM public tile service is best-effort. Do not let a slow/blocked
  // basemap make the project-owned parcel map appear to hang.
  if (window.L && typeof window.L.tileLayer === "function") {
    const originalTileLayer = window.L.tileLayer.bind(window.L);
    window.L.tileLayer = (url, options = {}) => {
      const layer = originalTileLayer(url, {
        ...options,
        updateWhenIdle: true,
        updateWhenZooming: false,
        keepBuffer: 0,
        maxNativeZoom: Math.min(options.maxNativeZoom ?? 16, 16),
        maxZoom: Math.min(options.maxZoom ?? 17, 17)
      });
      if (String(url).includes("tile.openstreetmap.org")) {
        let settled = false;
        layer.once("load", () => { settled = true; });
        layer.once("tileerror", () => {
          const status = document.getElementById("mapStatus");
          if (status) status.textContent = "Project map ready · basemap unavailable";
        });
        setTimeout(() => {
          if (settled || !layer._map) return;
          try { layer.remove(); } catch (_) {}
          const status = document.getElementById("mapStatus");
          const notice = document.getElementById("mapNotice");
          if (status) status.textContent = "Project map ready · basemap timed out";
          if (notice) {
            notice.textContent = "OpenStreetMap tiles are taking too long. Parcel geometry and evidence are ready to use.";
            notice.classList.remove("hidden");
          }
        }, 4500);
      }
      return layer;
    };
  }

  if (window.L && typeof window.L.map === "function") {
    const originalMap = window.L.map.bind(window.L);
    window.L.map = (...args) => {
      const map = originalMap(...args);
      window.__landIntelligenceMap = map;
      const invalidate = () => { try { map.invalidateSize({ pan: false, debounceMoveend: true }); } catch (_) {} };
      setTimeout(invalidate, 0);
      setTimeout(invalidate, 250);
      setTimeout(invalidate, 750);
      return map;
    };
  }
})();
