(() => {
  "use strict";

  // Compatibility layer for the Land Intelligence map. The demo GeoJSON
  // contains parcel geometry but older payloads may omit geography fields.
  // The UI's District -> Taluka -> Village filter needs those fields on
  // every GeoJSON feature, so enrich the response without changing the
  // synthetic/authoritative data semantics.
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

      const enriched = {
        ...payload,
        features: payload.features.map(feature => {
          const props = { ...(feature.properties || {}) };
          const parcel = props.parcel_id;
          const geography = geographyByParcel[parcel] || {};
          return { ...feature, properties: { ...geography, ...props } };
        })
      };

      return new Response(JSON.stringify(enriched), {
        status: response.status,
        statusText: response.statusText,
        headers: new Headers(response.headers)
      });
    } catch (_) {
      return response;
    }
  };

  // Keep a reference to the Leaflet instance so we can reliably invalidate
  // its size after the page/layout has settled.
  if (window.L && typeof window.L.map === "function") {
    const originalMap = window.L.map.bind(window.L);
    window.L.map = (...args) => {
      const map = originalMap(...args);
      window.__landIntelligenceMap = map;
      const invalidate = () => {
        try { map.invalidateSize({ pan: false, debounceMoveend: true }); } catch (_) {}
      };
      setTimeout(invalidate, 0);
      setTimeout(invalidate, 250);
      setTimeout(invalidate, 750);
      return map;
    };
  }

  window.addEventListener("load", () => {
    const invalidate = () => {
      try { window.__landIntelligenceMap?.invalidateSize({ pan: false, debounceMoveend: true }); } catch (_) {}
    };
    setTimeout(invalidate, 0);
    setTimeout(invalidate, 500);
  });
})();
