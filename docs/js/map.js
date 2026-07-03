(() => {
  /** Same palette order as the Apps Script; assigned alphabetically for stable colors. */
  const ASSIGNEE_COLOR_PALETTE = [
    "Blue",
    "Red",
    "Green",
    "Orange",
    "Purple",
    "Teal",
    "Pink",
    "Yellow",
    "Peach",
    "Maroon",
  ];

  const COLOR_HEX = {
    Blue: "#2563eb",
    Red: "#dc2626",
    Green: "#16a34a",
    Orange: "#ea580c",
    Purple: "#9333ea",
    Teal: "#0d9488",
    Pink: "#db2777",
    Yellow: "#ca8a04",
    Peach: "#fb923c",
    Maroon: "#9f1239",
    Grey: "#6b7280",
  };

  const UNASSIGNED_COLOR = "Grey";

  // Stable shape per facility subtype.
  const SUBTYPE_SHAPES = {
    "Adult Family Home": "circle",
    "Assisted Living Facility": "square",
    "Enhanced Services Facility": "diamond",
    "Certified Residential Service and Supports Provider": "triangle",
    "Group Training Home": "pentagon",
    "Nursing Facility": "star",
    "Nursing Home": "star",
    "Intermediate Care Facility": "hexagon",
    "ICF/IID": "hexagon",
  };

  const SHAPE_ORDER = [
    "Adult Family Home",
    "Assisted Living Facility",
    "Enhanced Services Facility",
    "Certified Residential Service and Supports Provider",
    "Group Training Home",
    "Nursing Facility",
    "Intermediate Care Facility",
  ];

  const map = L.map("map", {
    zoomControl: true,
    preferCanvas: true,
  }).setView([47.5, -120.5], 7);

  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(map);

  // Keep pins separate at normal zoom; only bubble-cluster when zoomed far out.
  const clusterGroup = L.markerClusterGroup({
    showCoverageOnHover: false,
    maxClusterRadius: 24,
    disableClusteringAtZoom: 7,
    spiderfyOnMaxZoom: true,
    removeOutsideVisibleBounds: true,
  });
  map.addLayer(clusterGroup);

  const statusEl = document.getElementById("status");
  const detailsEl = document.getElementById("details");
  const assigneeLegendEl = document.getElementById("assignee-legend");
  const subtypeLegendEl = document.getElementById("subtype-legend");

  let selectedMarker = null;
  /** assignee name -> palette color name (built on each data load) */
  let assigneeColorMap = new Map();

  function normalizeAssignee(value) {
    const name = String(value ?? "").trim();
    return name || "Unassigned";
  }

  /**
   * Build a stable assignee → color map from the loaded features.
   * Assignees are sorted alphabetically so the same pool always gets the same colors.
   * Unassigned is always Grey.
   */
  function buildAssigneeColorMap(features) {
    const names = new Set();
    features.forEach((feature) => {
      const assignee = normalizeAssignee(feature.properties?.assignee);
      if (assignee !== "Unassigned") {
        names.add(assignee);
      }
    });

    const sorted = [...names].sort((a, b) => a.localeCompare(b));
    const map = new Map();
    sorted.forEach((name, index) => {
      map.set(
        name,
        ASSIGNEE_COLOR_PALETTE[index % ASSIGNEE_COLOR_PALETTE.length]
      );
    });
    map.set("Unassigned", UNASSIGNED_COLOR);
    return map;
  }

  function colorNameForAssignee(assignee) {
    const name = normalizeAssignee(assignee);
    return assigneeColorMap.get(name) || UNASSIGNED_COLOR;
  }

  function colorHexForAssignee(assignee) {
    return COLOR_HEX[colorNameForAssignee(assignee)] || COLOR_HEX.Grey;
  }

  function shapeFor(subtype) {
    return SUBTYPE_SHAPES[subtype] || "circle";
  }

  function shapePath(shape) {
    switch (shape) {
      case "square":
        return "M4 4 H20 V20 H4 Z";
      case "diamond":
        return "M12 2 L22 12 L12 22 L2 12 Z";
      case "triangle":
        return "M12 3 L21 20 H3 Z";
      case "pentagon":
        return "M12 2 L21 9 L18 21 H6 L3 9 Z";
      case "hexagon":
        return "M8 3 H16 L21 12 L16 21 H8 L3 12 Z";
      case "star":
        return "M12 2 L14.9 8.6 L22 9.3 L16.8 14 L18.2 21 L12 17.5 L5.8 21 L7.2 14 L2 9.3 L9.1 8.6 Z";
      case "circle":
      default:
        return "M12 3 A9 9 0 1 1 11.999 3 Z";
    }
  }

  function markerSvg(assignee, subtype, selected) {
    const fill = colorHexForAssignee(assignee);
    const stroke = selected ? "#ffffff" : "rgba(15, 23, 42, 0.85)";
    const strokeWidth = selected ? 2.4 : 1.4;
    return `
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
        <path d="${shapePath(shapeFor(subtype))}" fill="${fill}" stroke="${stroke}" stroke-width="${strokeWidth}" />
      </svg>
    `;
  }

  function markerIcon(props, selected) {
    return L.divIcon({
      className: `marker-icon${selected ? " is-selected" : ""}`,
      html: markerSvg(props.assignee, props.subtype, selected),
      iconSize: [22, 22],
      iconAnchor: [11, 11],
      popupAnchor: [0, -10],
    });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function normalizeHttpUrl(value) {
    const text = String(value ?? "").trim();
    if (!text) {
      return null;
    }

    const candidates = [text];
    if (!/^https?:\/\//i.test(text)) {
      candidates.push(`https://${text}`);
    }

    for (const candidate of candidates) {
      try {
        const url = new URL(candidate);
        if (url.protocol === "http:" || url.protocol === "https:") {
          return url.href;
        }
      } catch {
        // try next candidate
      }
    }

    return null;
  }

  function linkOrText(value) {
    const text = String(value ?? "").trim();
    if (!text) {
      return "—";
    }

    const href = normalizeHttpUrl(text);
    if (!href) {
      return escapeHtml(text);
    }

    return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
  }

  function mapsUrl(address) {
    return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(address)}`;
  }

  function displayValue(value) {
    const text = String(value ?? "").trim();
    return text || "—";
  }

  function renderDetails(props) {
    const address = (props.address || "").trim();

    const addressHtml = address
      ? `<a href="${escapeHtml(mapsUrl(address))}" target="_blank" rel="noopener noreferrer">${escapeHtml(address)}</a>`
      : "—";

    detailsEl.innerHTML = `
      <h3 class="details-title">${escapeHtml(props.name || "Facility")}</h3>
      <dl class="details-list">
        <div>
          <dt>Name</dt>
          <dd>${escapeHtml(displayValue(props.name))}</dd>
        </div>
        <div>
          <dt>Type</dt>
          <dd>${escapeHtml(displayValue(props.type))}</dd>
        </div>
        <div>
          <dt>SubType</dt>
          <dd>${escapeHtml(displayValue(props.subtype))}</dd>
        </div>
        <div>
          <dt>Address</dt>
          <dd>${addressHtml}</dd>
        </div>
        <div>
          <dt>County</dt>
          <dd>${escapeHtml(displayValue(props.county))}</dd>
        </div>
        <div>
          <dt>Assignee</dt>
          <dd>${escapeHtml(displayValue(props.assignee))}</dd>
        </div>
        <div>
          <dt>Reports Location</dt>
          <dd>${linkOrText(props.reports_location)}</dd>
        </div>
        <div>
          <dt>Service Disclosure</dt>
          <dd>${linkOrText(props.service_disclosure)}</dd>
        </div>
        <div>
          <dt>Beds</dt>
          <dd>${escapeHtml(displayValue(props.beds))}</dd>
        </div>
        <div>
          <dt>Specialties</dt>
          <dd>${escapeHtml(displayValue(props.specialties))}</dd>
        </div>
        <div>
          <dt>Facility POC</dt>
          <dd>${escapeHtml(displayValue(props.facility_poc))}</dd>
        </div>
      </dl>
    `;
  }

  function clearSelection() {
    if (selectedMarker) {
      selectedMarker.setIcon(markerIcon(selectedMarker.feature.properties, false));
      selectedMarker = null;
    }
  }

  function selectMarker(marker) {
    clearSelection();
    selectedMarker = marker;
    marker.setIcon(markerIcon(marker.feature.properties, true));
    renderDetails(marker.feature.properties);
  }

  function legendShapeSvg(shape) {
    return `
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="${shapePath(shape)}" fill="#94a3b8" stroke="#e5e7eb" stroke-width="1.4" />
      </svg>
    `;
  }

  function renderLegends(features) {
    const subtypes = new Set();

    features.forEach((feature) => {
      const props = feature.properties || {};
      if (props.subtype) {
        subtypes.add(props.subtype);
      }
    });

    const assigneeItems = [...assigneeColorMap.entries()].sort((a, b) => {
      if (a[0] === "Unassigned") return 1;
      if (b[0] === "Unassigned") return -1;
      return a[0].localeCompare(b[0]);
    });

    assigneeLegendEl.innerHTML = assigneeItems
      .map(([name, colorName]) => {
        const hex = COLOR_HEX[colorName] || COLOR_HEX.Grey;
        return `
          <div class="legend-item">
            <span class="legend-swatch" style="background:${hex}"></span>
            <span>${escapeHtml(name)} <span style="opacity:.7">(${escapeHtml(colorName)})</span></span>
          </div>
        `;
      })
      .join("");

    const subtypeItems = [
      ...SHAPE_ORDER.filter((name) => subtypes.has(name)),
      ...[...subtypes].filter((name) => !SHAPE_ORDER.includes(name)).sort(),
    ];

    subtypeLegendEl.innerHTML = subtypeItems
      .map((name) => {
        const shape = shapeFor(name);
        return `
          <div class="legend-item">
            <span class="legend-shape">${legendShapeSvg(shape)}</span>
            <span>${escapeHtml(name)}</span>
          </div>
        `;
      })
      .join("");
  }

  const REFRESH_MS = 5 * 60 * 1000;
  let hasFittedBounds = false;
  let selectedName = null;

  async function loadFacilities({ fitBounds = false, quiet = false } = {}) {
    if (!quiet) {
      statusEl.textContent = "Loading facilities…";
    }

    // Bust browser/CDN caches: unique query string + no-store.
    const response = await fetch(
      `data/facilities.geojson?t=${Date.now()}`,
      {
        cache: "no-store",
        headers: {
          "Cache-Control": "no-cache",
          Pragma: "no-cache",
        },
      }
    );
    if (!response.ok) {
      throw new Error(`Could not load facilities.geojson (${response.status})`);
    }

    const geojson = await response.json();
    const features = geojson.features || [];
    assigneeColorMap = buildAssigneeColorMap(features);

    if (selectedMarker) {
      selectedName = selectedMarker.feature.properties.name || null;
    }

    clusterGroup.clearLayers();
    selectedMarker = null;

    let restoredMarker = null;
    const layer = L.geoJSON(geojson, {
      pointToLayer(feature, latlng) {
        const marker = L.marker(latlng, {
          icon: markerIcon(feature.properties || {}, false),
          riseOnHover: true,
        });
        marker.feature = feature;
        marker.on("click", () => selectMarker(marker));
        if (
          selectedName &&
          feature.properties &&
          feature.properties.name === selectedName
        ) {
          restoredMarker = marker;
        }
        return marker;
      },
    });

    clusterGroup.addLayer(layer);
    renderLegends(features);

    if (restoredMarker) {
      selectMarker(restoredMarker);
    } else if (selectedName) {
      selectedName = null;
      detailsEl.innerHTML =
        '<p class="details-empty">Click a facility pin to see details.</p>';
    }

    if ((fitBounds || !hasFittedBounds) && features.length > 0) {
      map.fitBounds(clusterGroup.getBounds().pad(0.08));
      hasFittedBounds = true;
    }

    const updatedAt = new Date().toLocaleTimeString();
    statusEl.textContent = `${features.length.toLocaleString()} facilities · updated ${updatedAt}`;
  }

  map.on("click", () => {
    clearSelection();
    selectedName = null;
    detailsEl.innerHTML =
      '<p class="details-empty">Click a facility pin to see details.</p>';
  });

  function loadFacilitiesOrReport(options) {
    return loadFacilities(options).catch((error) => {
      console.error(error);
      if (!options || !options.quiet) {
        statusEl.textContent = "Failed to load data";
        detailsEl.innerHTML = `<p class="details-empty">${escapeHtml(error.message)}</p>`;
      }
    });
  }

  loadFacilitiesOrReport({ fitBounds: true });
  setInterval(() => {
    loadFacilitiesOrReport({ quiet: true });
  }, REFRESH_MS);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      loadFacilitiesOrReport({ quiet: true });
    }
  });
})();
