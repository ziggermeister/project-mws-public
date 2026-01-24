/**
 * ============================================================
 * MWS vNext Policy Contract (Apps Script / V8)
 * ============================================================
 * Purpose:
 *  - Validate vNext policy schema + logical consistency
 *  - Enforce invariants deterministically (fail-fast capable)
 *  - Emit derived views for future allocator scripts
 *
 * Assumptions (vNext):
 *  policy.assets.tickers: { "VTI": { lifecycle:{stage,...}, mappings:{ exposure_l2:{}, signal_l2:{} }, ... }, ... }
 *  policy.taxonomy.sleeves.level1: { "equities": {...}, ... }
 *  policy.taxonomy.sleeves.level2: { "crypto": { parent_l1:"alternatives", ... }, ... }
 *  policy.portfolio.denominators: { exposure:"...", momentum:"...", formulas:{...} }
 */

/** ---------- Public entrypoints ---------- **/

/**
 * Load + validate policy (and optionally tracker), returning a deterministic contract bundle.
 *
 * @param {string} policyFileId Google Drive file ID for policy JSON
 * @param {string=} trackerFileId Optional Drive file ID for tracker JSON (authoritative fetch universe)
 * @param {{hardFail?:boolean}=} opts
 * @returns {{
 *   ok: boolean,
 *   errors: string[],
 *   warnings: string[],
 *   policy: Object,
 *   tracker: (Object|null),
 *   derived: Object
 * }}
 */
function mwsLoadValidateAndProject(policyFileId, trackerFileId, opts) {
  opts = opts || {};
  var hardFail = (opts.hardFail === true);

  var policy = mwsLoadJsonFromDrive_(policyFileId, "policy");
  var tracker = trackerFileId ? mwsLoadJsonFromDrive_(trackerFileId, "tracker") : null;

  var vr = mwsValidatePolicyVNext_(policy, tracker);

  var derived = {};
  if (vr.ok) {
    derived = mwsProjectPolicyViewsVNext_(policy, tracker);
  }

  var bundle = {
    ok: vr.ok,
    errors: vr.errors,
    warnings: vr.warnings,
    policy: policy,
    tracker: tracker,
    derived: derived
  };

  if (hardFail && !bundle.ok) {
    throw new Error("MWS CONTRACT HARD_FAIL:\n" + bundle.errors.join("\n"));
  }

  return bundle;
}

/**
 * Convenience: print a human-readable contract report to logs.
 */
function mwsPrintContractReport(bundle) {
  console.log("=== MWS vNext Contract Report ===");
  console.log("OK: " + bundle.ok);
  if (bundle.errors && bundle.errors.length) {
    console.log("--- ERRORS (" + bundle.errors.length + ") ---");
    bundle.errors.forEach(function(e){ console.log("ERROR: " + e); });
  }
  if (bundle.warnings && bundle.warnings.length) {
    console.log("--- WARNINGS (" + bundle.warnings.length + ") ---");
    bundle.warnings.forEach(function(w){ console.log("WARN: " + w); });
  }
  if (bundle.derived && bundle.derived.universe) {
    console.log("--- DERIVED ---");
    console.log("Universe size: " + bundle.derived.universe.all.length);
    console.log("Eligible (momentum): " + bundle.derived.eligibility.momentumEligible.length);
    console.log("Stages: " + JSON.stringify(bundle.derived.universe.byStage, null, 0));
  }
}

/** ---------- Validator ---------- **/

function mwsValidatePolicyVNext_(policy, tracker) {
  var errors = [];
  var warnings = [];

  // ---- basic shape ----
  if (!policy || typeof policy !== "object") {
    return { ok:false, errors:["Policy is null/invalid JSON object."], warnings:warnings };
  }

  // Required top-level blocks
  if (!policy.meta) errors.push("Missing policy.meta");
  if (!policy.assets || !policy.assets.tickers) errors.push("Missing policy.assets.tickers");
  if (!policy.taxonomy || !policy.taxonomy.sleeves) errors.push("Missing policy.taxonomy.sleeves");
  if (!policy.taxonomy || !policy.taxonomy.sleeves.level1) errors.push("Missing policy.taxonomy.sleeves.level1");
  if (!policy.taxonomy || !policy.taxonomy.sleeves.level2) errors.push("Missing policy.taxonomy.sleeves.level2");
  if (!policy.portfolio || !policy.portfolio.denominators) warnings.push("Missing policy.portfolio.denominators (recommended).");

  if (errors.length) return { ok:false, errors:errors, warnings:warnings };

  // ---- normalize keys ----
  var tickers = policy.assets.tickers || {};
  var tList = Object.keys(tickers).map(mwsNormTicker_);
  var tickerKeyMismatch = Object.keys(tickers).filter(function(k){ return mwsNormTicker_(k) !== k; });
  if (tickerKeyMismatch.length) {
    warnings.push("Ticker keys should be uppercase. Non-canonical keys: " + tickerKeyMismatch.join(", "));
  }

  // ---- sleeve taxonomy consistency ----
  var level1 = policy.taxonomy.sleeves.level1 || {};
  var level2 = policy.taxonomy.sleeves.level2 || {};
  var l1Keys = Object.keys(level1);
  var l2Keys = Object.keys(level2);

  if (!l1Keys.length) errors.push("taxonomy.sleeves.level1 is empty.");
  if (!l2Keys.length) errors.push("taxonomy.sleeves.level2 is empty.");

  // Each L2 must map to exactly one L1
  l2Keys.forEach(function(l2){
    var node = level2[l2] || {};
    var parent = node.parent_l1;
    if (!parent) errors.push("Level2 sleeve '" + l2 + "' missing parent_l1.");
    else if (!level1[parent]) errors.push("Level2 sleeve '" + l2 + "' references unknown parent_l1 '" + parent + "'.");
  });

  // ---- lifecycle stages ----
  var allowedStages = {"inducted":true,"activated":true,"overlay":true,"reference":true,"disabled":true};

  // Rule you agreed: {"inducted","activated","overlay"} must have lifecycle.entered_stage_date
  var mustHaveEnteredDate = {"inducted":true,"activated":true,"overlay":true};

  // also enforce: those tickers must have exposure_l2 + signal_l2 mappings
  // (You can relax this later if desired, but this matches your “actionable future allocator” goal.)
  tList.forEach(function(T){
    var obj = tickers[T] || tickers[T.toLowerCase()] || null;
    if (!obj) return;

    var lc = obj.lifecycle || {};
    var stage = String(lc.stage || "").trim().toLowerCase();
    if (!stage) errors.push("Ticker '" + T + "' missing lifecycle.stage");
    else if (!allowedStages[stage]) errors.push("Ticker '" + T + "' has invalid lifecycle.stage='" + stage + "'");

    if (mustHaveEnteredDate[stage]) {
      var ed = String(lc.entered_stage_date || "").trim();
      if (!mwsIsYMD_(ed)) errors.push("Ticker '" + T + "' stage '" + stage + "' missing/invalid lifecycle.entered_stage_date (YYYY-MM-DD).");
    }

    // Mappings checks for inductable/active/overlay
    if (stage === "inducted" || stage === "activated" || stage === "overlay") {
      var maps = obj.mappings || {};
      if (!maps.exposure_l2 || typeof maps.exposure_l2 !== "object") errors.push("Ticker '" + T + "' missing mappings.exposure_l2");
      if (!maps.signal_l2   || typeof maps.signal_l2   !== "object") errors.push("Ticker '" + T + "' missing mappings.signal_l2");
    }
  });

  // ---- mapping integrity (sum-to-1, valid L2s) ----
  tList.forEach(function(T){
    var obj = tickers[T] || tickers[T.toLowerCase()] || null;
    if (!obj) return;
    var stage = String((obj.lifecycle||{}).stage||"").trim().toLowerCase();

    // Enforce multi-sleeve weights sum to 1.0 for exposure_l2 + signal_l2
    var maps = obj.mappings || {};
    if (maps.exposure_l2) {
      mwsValidateWeightMap_("Ticker '"+T+"' exposure_l2", maps.exposure_l2, level2, errors, warnings);
    }
    if (maps.signal_l2) {
      mwsValidateWeightMap_("Ticker '"+T+"' signal_l2", maps.signal_l2, level2, errors, warnings);
    }

    // For reference/disabled we do NOT require mappings (but if provided, validate anyway)
    if ((stage === "reference" || stage === "disabled") && maps && (maps.exposure_l2 || maps.signal_l2)) {
      // already validated above; just informational
    }
  });

  // ---- denominators sanity (recommended) ----
  if (policy.portfolio && policy.portfolio.denominators) {
    var den = policy.portfolio.denominators || {};
    if (!den.exposure) warnings.push("portfolio.denominators.exposure missing.");
    if (!den.momentum) warnings.push("portfolio.denominators.momentum missing.");
    if (den.formulas && typeof den.formulas !== "object") errors.push("portfolio.denominators.formulas must be an object if present.");
  }

  // ---- tracker drift checks (if tracker provided) ----
  if (tracker) {
    var trackerUniverse = mwsExtractTrackerTickers_(tracker); // normalized uppercase
    var policyTickers = new Set(tList);

    // policy ⊆ tracker (hard invariant you want)
    var policyNotInTracker = tList.filter(function(t){ return trackerUniverse.indexOf(t) === -1; });
    if (policyNotInTracker.length) {
      errors.push("policy.assets.tickers contains tickers not present in tracker universe: " + policyNotInTracker.join(", "));
    }

    // tracker extra tickers are allowed; warn for visibility
    var trackerExtra = trackerUniverse.filter(function(t){ return !policyTickers.has(t); });
    if (trackerExtra.length) {
      warnings.push("Tracker contains tickers not declared in policy.assets.tickers (allowed for fetch/reference): " + trackerExtra.join(", "));
    }
  } else {
    warnings.push("No tracker provided: cannot validate policy ⊆ tracker invariant or drift.");
  }

  // ---- unambiguous mapping: each L2 -> exactly one L1 already enforced ----
  // Additional: ensure no ticker has L2 that is unreachable (i.e., not in level2 map)
  // handled in weight map validation.

  var ok = errors.length === 0;
  return { ok: ok, errors: errors, warnings: warnings };
}

function mwsValidateWeightMap_(label, weightMap, level2Map, errors, warnings) {
  var keys = Object.keys(weightMap || {});
  if (!keys.length) {
    errors.push(label + " has empty mapping (must sum to 1.0 across one or more L2 sleeves).");
    return;
  }

  var sum = 0;
  keys.forEach(function(l2){
    if (!level2Map[l2]) errors.push(label + " references unknown L2 sleeve '" + l2 + "'");
    var w = Number(weightMap[l2]);
    if (!isFinite(w)) errors.push(label + " has non-numeric weight for '" + l2 + "': " + weightMap[l2]);
    if (w < 0) errors.push(label + " has negative weight for '" + l2 + "': " + w);
    sum += (isFinite(w) ? w : 0);
  });

  // strict sum-to-1 with small tolerance
  var tol = 1e-6;
  if (Math.abs(sum - 1.0) > tol) {
    errors.push(label + " weights must sum to 1.0; found " + sum.toFixed(6));
  }
}

/** ---------- Derived Views (deterministic projections) ---------- **/

function mwsProjectPolicyViewsVNext_(policy, tracker) {
  var tickers = policy.assets.tickers || {};
  var tList = Object.keys(tickers).map(mwsNormTicker_).sort();

  var level2 = policy.taxonomy.sleeves.level2 || {};
  var level1 = policy.taxonomy.sleeves.level1 || {};

  // Universe by stage
  var byStage = { inducted:[], activated:[], overlay:[], reference:[], disabled:[] };
  tList.forEach(function(T){
    var stage = String((tickers[T].lifecycle||{}).stage||"").trim().toLowerCase();
    if (!byStage[stage]) byStage[stage] = [];
    byStage[stage].push(T);
  });

  // Eligibility sets (future allocator ready)
  // Momentum-eligible = inducted + activated (optionally exclude overlay) — you can tune this later.
  var momentumEligible = []
    .concat(byStage.inducted || [])
    .concat(byStage.activated || []);

  // Held tickers are script/runtime concern; keep contract clean:
  // but we still provide a deterministic “policy eligibility” view.
  var overlays = byStage.overlay || [];
  var references = byStage.reference || [];

  // Mapping: ticker -> (exposure_l1 rollup, exposure_l2, signal_l2, primary L1)
  var tickerViews = {};
  tList.forEach(function(T){
    var obj = tickers[T] || {};
    var maps = obj.mappings || {};
    var expL2 = maps.exposure_l2 || null;
    var sigL2 = maps.signal_l2 || null;

    var expL1 = expL2 ? mwsRollupL2ToL1_(expL2, level2) : null;
    var sigL1 = sigL2 ? mwsRollupL2ToL1_(sigL2, level2) : null;

    tickerViews[T] = {
      stage: String((obj.lifecycle||{}).stage||"").trim().toLowerCase(),
      entered_stage_date: (obj.lifecycle||{}).entered_stage_date || null,
      benchmark_proxy: (obj.lifecycle||{}).benchmark_proxy || null,

      exposure_l2: expL2,
      exposure_l1: expL1,

      signal_l2: sigL2,
      signal_l1: sigL1
    };
  });

  // Optional: tracker universe (if provided)
  var trackerUniverse = tracker ? mwsExtractTrackerTickers_(tracker) : null;

  // Denominators (exposure vs momentum)
  var den = (policy.portfolio && policy.portfolio.denominators) ? policy.portfolio.denominators : null;

  return {
    universe: {
      all: tList,
      byStage: byStage,
      trackerUniverse: trackerUniverse
    },
    eligibility: {
      momentumEligible: momentumEligible,
      overlays: overlays,
      references: references
    },
    taxonomy: {
      level1: Object.keys(level1).sort(),
      level2_to_level1: mwsBuildL2ToL1Map_(level2)
    },
    tickers: tickerViews,
    denominators: den
  };
}

function mwsRollupL2ToL1_(l2WeightMap, level2Map) {
  // deterministic rollup: sum weights by parent_l1
  var out = {};
  Object.keys(l2WeightMap).forEach(function(l2){
    var w = Number(l2WeightMap[l2]);
    var parent = (level2Map[l2] || {}).parent_l1;
    if (!parent) return;
    out[parent] = (out[parent] || 0) + (isFinite(w) ? w : 0);
  });

  // normalize tiny float artifacts
  Object.keys(out).forEach(function(k){
    out[k] = Number(out[k].toFixed(6));
  });
  return out;
}

function mwsBuildL2ToL1Map_(level2Map) {
  var out = {};
  Object.keys(level2Map || {}).forEach(function(l2){
    out[l2] = level2Map[l2].parent_l1 || null;
  });
  return out;
}

/** ---------- Drive JSON Loader ---------- **/

function mwsLoadJsonFromDrive_(fileId, label) {
  try {
    var blob = DriveApp.getFileById(fileId).getBlob().getDataAsString();
    return JSON.parse(blob);
  } catch (e) {
    throw new Error("Failed to load/parse " + label + " JSON from Drive fileId=" + fileId + " :: " + e.message);
  }
}

/** ---------- Tracker parser (universe gatekeeper) ---------- **/

function mwsExtractTrackerTickers_(tracker) {
  var set = {};
  function add(t) {
    if (!t) return;
    var T = mwsNormTicker_(t);
    if (T) set[T] = true;
  }

  // common shapes: { tickers:[ "VTI", {ticker:"QQQ"} ] } or inventory/positions
  if (tracker && typeof tracker === "object") {
    if (Array.isArray(tracker.tickers)) {
      tracker.tickers.forEach(function(x){
        if (typeof x === "string") add(x);
        else if (x && typeof x === "object" && x.ticker) add(x.ticker);
      });
    }
    ["inventory", "positions"].forEach(function(k){
      if (Array.isArray(tracker[k])) {
        tracker[k].forEach(function(x){
          if (typeof x === "string") add(x);
          else if (x && typeof x === "object" && x.ticker) add(x.ticker);
        });
      }
    });
  }

  return Object.keys(set).sort();
}

/** ---------- Small helpers ---------- **/

function mwsNormTicker_(t) {
  return String(t || "").trim().toUpperCase();
}

function mwsIsYMD_(s) {
  return /^\d{4}-\d{2}-\d{2}$/.test(String(s || "").trim());
}