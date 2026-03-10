/**
 * ============================================================
 * TITANIUM MWS (with CLASP): UNIFIED RUNNER (v13.7) + 2-PANEL DASHBOARD
 * ============================================================
 * Design rule:
 *  - CONFIG is DEFAULTS ONLY.
 *  - All runtime values MUST come from C = getC_() (CONFIG merged with CONFIG_PVT.json).
 *
 * Your private config file is looked up via Script Properties key:
 *   MWS_PRIVATE_CONFIG_FILE_ID
 *
 * Run initPrivateConfigFileId() once in the Apps Script editor to register
 * your CONFIG_PVT.json Drive file ID. Never commit the real ID to source control.
 * ============================================================
 */

const CONFIG = {
  // Safe fallback (will be overridden by CONFIG_PVT.json)
  EMAIL_RECIPIENT: "bhatnagar.vivek@gmail.com",

  // IDs are expected from CONFIG_PVT.json; keep null defaults here.
  POLICY_FILE_ID: null,
  STATE_FILE_ID: null,
  HIST_FILE_ID: null,
  LOG_FILE_ID: null,
  HOLDINGS_FILE_ID: null,
  SCRATCH_SHEET_ID: null,
  SCRATCH_SHEET_NAME: "TMP",

  // Runtime
  RUNTIME_BUDGET_MS: 5 * 60 * 1000,
  GOOGLEFINANCE_SLEEP_MS: 2500,

  MIN_BACKFILL_DAYS: 366,
  CHUNK_DAYS: 180,

  REVIEW_DAYS: 30,

  // Defaults (CONFIG_PVT.json does NOT need to define these)
  BASELINES: ["SPY", "QQQ"],
  MIN_CORR_RETURNS: 6,
  CORR_ALERT_THRESHOLD: 0.85
};

/**
 * One-time initializer (optional): sets your private config file ID into Script Properties.
 * Run this once manually in Apps Script to store the file ID you provided.
 */
function initPrivateConfigFileId() {
  // SECURITY: never commit your real Drive file ID to version control.
  // Paste your CONFIG_PVT.json file ID from Google Drive here, run this
  // function once in the Apps Script editor, then delete the value again.
  const PRIVATE_ID = "YOUR_DRIVE_FILE_ID_HERE";
  if (PRIVATE_ID === "YOUR_DRIVE_FILE_ID_HERE") {
    throw new Error("initPrivateConfigFileId: replace PRIVATE_ID with your real Drive file ID before running.");
  }
  PropertiesService.getScriptProperties().setProperty("MWS_PRIVATE_CONFIG_FILE_ID", PRIVATE_ID);
  console.log(`[INIT] Set MWS_PRIVATE_CONFIG_FILE_ID (length=${PRIVATE_ID.length})`);
}

function loadConfig_() {
  const fileId = PropertiesService.getScriptProperties().getProperty("MWS_PRIVATE_CONFIG_FILE_ID");
  if (!fileId) throw new Error("Missing Script Property: MWS_PRIVATE_CONFIG_FILE_ID");
  const file = DriveApp.getFileById(fileId);
  const raw = file.getBlob().getDataAsString();
  try {
    return JSON.parse(raw);
  } catch (e) {
    throw new Error(`CONFIG_PVT.json is not valid JSON: ${e.message}`);
  }
}

// NOTE: GAS creates a fresh V8 isolate for every execution, so a module-level
// variable is reset on each run. Do not add a module-level cache here; just
// call loadConfig_() directly and merge with CONFIG on every invocation.
function getC_() {
  const cfg = loadConfig_();
  return Object.assign({}, CONFIG, cfg);
}

/**
 * ============================================================
 * ENTRYPOINT
 * ============================================================
 */
function runDailyRoutine() {
  const START = Date.now();
  const TZ = Session.getScriptTimeZone();
  const TODAY = Utilities.formatDate(new Date(), TZ, "yyyy-MM-dd");
  const alerts = [];

  let C = null;

  try {
    C = getC_();
    console.log(`[START] Titanium Unified v13.7 | Date=${TODAY}`);

    // Basic config sanity (fail fast with clear errors)
    assertConfig_(C);

    const policy = JSON.parse(DriveApp.getFileById(C.POLICY_FILE_ID).getBlob().getDataAsString());
    const state = JSON.parse(DriveApp.getFileById(C.STATE_FILE_ID).getBlob().getDataAsString());

    console.log(`[POLICY] name=${policy?.meta?.policy_name || "N/A"} version=${policy?.meta?.policy_version || "N/A"}`);

    // Universe
    const trackerTickers = normalizeInventory_(state);
    const policyRequired = getPolicyRequiredTickers_(policy);

    (C.BASELINES || []).forEach(b => policyRequired.add(String(b).trim().toUpperCase()));

    const requiredSet = new Set([...trackerTickers, ...policyRequired]);
    const requiredTickers = [...requiredSet].sort();

    const missingFromTracker = [...policyRequired].filter(t => !trackerTickers.includes(t));
    if (missingFromTracker.length) {
      alerts.push(`AUDIT: Policy-required tickers not in tracker (kept anyway): ${missingFromTracker.join(", ")}`);
    }
    console.log(`[UNIVERSE] tracker=${trackerTickers.length}, policy_required=${policyRequired.size}, total_required=${requiredTickers.length}`);

    // HIST maintenance (purge + backfill + today)
    const sync = updateHistDatabase_(TODAY, requiredTickers, policyRequired, TZ, START);
    if (sync.purgedTickers.length) alerts.push(`PURGE: Removed rows for: ${sync.purgedTickers.join(", ")}`);
    if (sync.unfinishedTickers.length) alerts.push(`SYNC: Unfinished backfill for ${sync.unfinishedTickers.length} tickers (will resume next run).`);

    // Snapshot + perf log update
    const snapshot = processSnapshot_(policy, TZ, alerts);

    // Charts from perf log (2 panels)
    let charts = null;
    try {
      charts = generateDashboardCharts2Panel_(policy);
    } catch (e) {
      console.error(`[CHART] Skipped: ${e.message}`);
      alerts.push(`CHART: ${e.message}`);
    }

    // Email
    sendDailyEmail_(snapshot, charts, TODAY, alerts);

    console.log(`[FINISH] OK. Elapsed=${((Date.now() - START) / 1000).toFixed(1)}s`);
  } catch (e) {
    const stack = e && e.stack ? e.stack : String(e);
    console.error(`[FATAL] ${stack}`);

    // Robust fatal email: use merged config if available, else CONFIG fallback
    const to = (C && C.EMAIL_RECIPIENT) ? C.EMAIL_RECIPIENT : CONFIG.EMAIL_RECIPIENT;
    MailApp.sendEmail(to, "⚠️ MWS FATAL ERROR", stack);
  }
}

function assertConfig_(C) {
  const req = ["POLICY_FILE_ID", "STATE_FILE_ID", "HIST_FILE_ID", "LOG_FILE_ID", "HOLDINGS_FILE_ID", "SCRATCH_SHEET_ID", "SCRATCH_SHEET_NAME", "EMAIL_RECIPIENT"];
  const missing = req.filter(k => !C[k] || String(C[k]).includes("PUT_YOUR"));
  if (missing.length) throw new Error(`Missing required config keys in CONFIG_PVT.json: ${missing.join(", ")}`);
  if (!Array.isArray(C.BASELINES) || C.BASELINES.length < 1) throw new Error("C.BASELINES must be a non-empty array (e.g., [\"VTI\",\"QQQ\"]).");
}

/**
 * ============================================================
 * POLICY REQUIRED TICKERS
 * ============================================================
 */
function getPolicyRequiredTickers_(policy) {
  const set = new Set();

  const baselines = policy?.governance?.reporting_baselines || {};
  (baselines.active_benchmarks || []).forEach(t => set.add(String(t).trim().toUpperCase()));
  if (baselines.corr_anchor_ticker) set.add(String(baselines.corr_anchor_ticker).trim().toUpperCase());

  // Add every ticker that appears in ticker_constraints regardless of lifecycle stage
  // so that inducted, watchlist, and phasing-out tickers are all tracked in HIST.
  // Mirrors Python's get_policy_required_tickers() in mws_titanium_runner.py.
  // Fixed-price synthetic assets (e.g. CASH, TREASURY_NOTE) — never real market tickers
  const fixedPrices = policy?.governance?.fixed_asset_prices || {};
  const syntheticTickers = new Set(Object.keys(fixedPrices).map(t => String(t).trim().toUpperCase()));

  const tc = policy?.ticker_constraints || {};
  Object.keys(tc).forEach(t => {
    const T = String(t).trim().toUpperCase();
    // Skip fixed-price synthetic assets and non-GOOGLEFINANCE symbols
    if (syntheticTickers.has(T)) { return; }
    try { sanitizeTicker_(T); } catch (_) { return; }
    set.add(T);
    const lc = tc[t]?.lifecycle;
    if (lc?.benchmark_proxy) set.add(String(lc.benchmark_proxy).trim().toUpperCase());
  });

  [...set].forEach(x => { if (!x) set.delete(x); });
  return set;
}

/**
 * ============================================================
 * HIST DATABASE ENGINE (PURGE + BACKFILL + TODAY)
 * ============================================================
 * No SpreadsheetApp.create(). Reuses scratch sheet.
 */
function updateHistDatabase_(today, requiredTickers, policyRequiredSet, tz, startTime) {
  const props = PropertiesService.getScriptProperties();
  const C = getC_();

  const csvFile = DriveApp.getFileById(C.HIST_FILE_ID);
  const raw = csvFile.getBlob().getDataAsString().replace(/^\ufeff/g, "").trim();
  const parsed = Utilities.parseCsv(raw);
  if (!parsed || parsed.length < 2) throw new Error("HIST CSV empty or malformed.");

  const header = parsed[0];
  const col = buildColumnMap_(header);
  col.width = header.length;

  let rows = parsed.slice(1).map(r => normalizeRowWidth_(r, col.width));

  // Existing tickers in HIST
  const existingTickers = new Set(
    rows.map(r => String(r[col.ticker] || "").trim().toUpperCase()).filter(Boolean)
  );

  const requiredSet = new Set(requiredTickers);
  const purgedTickers = [...existingTickers].filter(t => !requiredSet.has(t));

  // Purge tickers not required
  if (purgedTickers.length) {
    console.log(`[PURGE] Removing tickers from HIST: ${purgedTickers.join(", ")}`);
    rows = rows.filter(r => requiredSet.has(String(r[col.ticker]).trim().toUpperCase()));
    purgedTickers.forEach(t => props.deleteProperty(`CURSOR_${t}`));
  }

  // Dedupe map
  const map = {};
  rows.forEach(r => {
    const d = String(r[col.date] || "").trim();
    const t = String(r[col.ticker] || "").trim().toUpperCase();
    if (!d || !t) return;
    map[`${d}|${t}`] = r;
  });

  // Bounds per ticker
  const bounds = {};
  requiredTickers.forEach(t => (bounds[t] = { min: null, max: null }));
  Object.keys(map).forEach(k => {
    const [d, t] = k.split("|");
    if (!bounds[t]) return;
    if (!bounds[t].min || d < bounds[t].min) bounds[t].min = d;
    if (!bounds[t].max || d > bounds[t].max) bounds[t].max = d;
  });

  const requiredStart = addDaysYMD_(today, -Number(C.MIN_BACKFILL_DAYS), tz);
  const unfinishedTickers = new Set();

  const scratch = openScratch_();
  const sh = ensureScratchSheet_(scratch);

  // Backfill: one chunk per ticker per run
  for (const t of requiredTickers) {
    if (Date.now() - startTime > Number(C.RUNTIME_BUDGET_MS)) { unfinishedTickers.add(t); continue; }

    const minHave = bounds[t]?.min;
    const needsYear = (!minHave || minHave > requiredStart);
    if (!needsYear) continue;

    const cursorKey = `CURSOR_${t}`;
    const cursor = props.getProperty(cursorKey);

    let chunkEnd;
    if (cursor && /^\d{4}-\d{2}-\d{2}$/.test(cursor)) chunkEnd = cursor;
    else if (minHave && /^\d{4}-\d{2}-\d{2}$/.test(minHave)) chunkEnd = addDaysYMD_(minHave, -1, tz);
    else chunkEnd = addDaysYMD_(today, -1, tz);

    if (chunkEnd < requiredStart) { props.deleteProperty(cursorKey); continue; }

    const tentativeStart = addDaysYMD_(chunkEnd, -(Number(C.CHUNK_DAYS) - 1), tz);
    const chunkStart = (tentativeStart < requiredStart) ? requiredStart : tentativeStart;

    // Skip synthetic/fixed-price tickers that aren't valid GOOGLEFINANCE symbols (e.g. TREASURY_NOTE, CASH)
    try { sanitizeTicker_(t); } catch (e) {
      console.log(`[BACKFILL] Skipping synthetic ticker ${t} (not a GOOGLEFINANCE symbol)`);
      continue;
    }

    console.log(`[BACKFILL] ${t} request ${chunkStart}..${chunkEnd} (minHave=${minHave || "NONE"} requiredStart=${requiredStart})`);

    const ingested = ingestBackfillChunk_(sh, t, chunkStart, chunkEnd, requiredStart, today, tz, map, col);
    console.log(`[BACKFILL] ${t} ingested=${ingested} rows`);

    const nextEnd = addDaysYMD_(chunkStart, -1, tz);
    if (nextEnd >= requiredStart) props.setProperty(cursorKey, nextEnd);
    else props.deleteProperty(cursorKey);
  }

  // Live prices for all required tickers (single-sheet batch)
  const livePrices = getBatchPricesScratch_(requiredTickers);

  requiredTickers.forEach(t => {
    const p = livePrices[t] || 0;
    if (p > 0) {
      const row = new Array(col.width).fill("");
      row[col.date] = today;
      row[col.ticker] = t;
      row[col.price] = p.toFixed(2);
      map[`${today}|${t}`] = row;
    } else if (policyRequiredSet.has(t)) {
      console.warn(`[LIVE] Missing price for policy-required ticker ${t} on ${today}`);
    }
  });

  const finalRows = Object.values(map).sort((a, b) => {
    const da = String(a[col.date]);
    const db = String(b[col.date]);
    const ta = String(a[col.ticker]).toUpperCase();
    const tb = String(b[col.ticker]).toUpperCase();
    return da.localeCompare(db) || ta.localeCompare(tb);
  });

  csvFile.setContent([header].concat(finalRows).map(r => normalizeRowWidth_(r, col.width).map(csvQuote_).join(",")).join("\n"));

  return { purgedTickers, unfinishedTickers: Array.from(unfinishedTickers) };
}

function ingestBackfillChunk_(sheet, ticker, chunkStart, chunkEnd, requiredStart, today, tz, map, col) {
  const C = getC_();
  sheet.clearContents();
  sheet.getRange("A1").setFormula(
    `=GOOGLEFINANCE("${sanitizeTicker_(ticker)}","price",DATEVALUE("${chunkStart}"),DATEVALUE("${chunkEnd}"))`
  );

  SpreadsheetApp.flush();
  Utilities.sleep(Number(C.GOOGLEFINANCE_SLEEP_MS));

  const values = sheet.getDataRange().getValues();
  if (!values || values.length < 2) return 0;

  let ingested = 0;
  values.slice(1).forEach(v => {
    if (!(v[0] instanceof Date)) return;
    const dStr = Utilities.formatDate(v[0], tz, "yyyy-MM-dd");
    if (dStr < requiredStart || dStr >= today) return;

    const px = parseFloat(v[1]);
    if (!isFinite(px)) return;

    const row = new Array(col.width).fill("");
    row[col.date] = dStr;
    row[col.ticker] = ticker;
    row[col.price] = px.toFixed(2);

    map[`${dStr}|${ticker}`] = row;
    ingested++;
  });

  sheet.clearContents();
  return ingested;
}

/**
 * ============================================================
 * SNAPSHOT + ANALYTICS
 * ============================================================
 */
function processSnapshot_(policy, tz, alerts) {
  const C = getC_();

  const histRaw = DriveApp.getFileById(C.HIST_FILE_ID).getBlob().getDataAsString().replace(/^\ufeff/g, "").trim();
  const hist = Utilities.parseCsv(histRaw);
  const col = buildColumnMap_(hist[0]);
  const rows = hist.slice(1);

  const allDates = [...new Set(rows.map(r => String(r[col.date])))]
    .filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .sort();

  if (!allDates.length) throw new Error("HIST has no valid date rows.");
  const asOfDate = allDates[allDates.length - 1];

  // Price map
  const priceByKey = {};
  rows.forEach(r => {
    const d = String(r[col.date]);
    const t = String(r[col.ticker]).trim().toUpperCase();
    const px = parseFloat(r[col.price]);
    if (!d || !t || !isFinite(px)) return;
    priceByKey[`${d}|${t}`] = px;
  });

  // Portfolio total from holdings — look up columns by header name, not positional index
  const holdingsRaw = DriveApp.getFileById(C.HOLDINGS_FILE_ID).getBlob().getDataAsString().replace(/^\ufeff/g, "").trim();
  const holdingsParsed = holdingsRaw ? Utilities.parseCsv(holdingsRaw) : [];
  if (holdingsParsed.length < 2) throw new Error("Holdings CSV empty or malformed.");
  const hdrH = holdingsParsed[0].map(v => String(v || "").trim().toLowerCase());
  const hTickerIdx = hdrH.indexOf("ticker");
  const hSharesIdx = hdrH.indexOf("shares");
  if (hTickerIdx === -1 || hSharesIdx === -1)
    throw new Error(`Holdings CSV missing required columns. Got: ${holdingsParsed[0].join(", ")}`);
  const holdings = holdingsParsed.slice(1);
  let totalVal = 0;

  holdings.forEach(r => {
    const t = String(r[hTickerIdx] || "").trim().toUpperCase();
    const qty = parseFloat(r[hSharesIdx]) || 0;
    const fixedEntry = policy?.governance?.fixed_asset_prices?.[t];
    let px;
    if (fixedEntry === undefined) {
      // No fixed price — use live price from HIST
      px = priceByKey[`${asOfDate}|${t}`] || 0;
    } else if (typeof fixedEntry === 'object') {
      // v2.7.1 structured format
      if (fixedEntry.price_type === 'market') {
        // Prefer live price; fall back to fallback_price if not in HIST
        px = priceByKey[`${asOfDate}|${t}`] || fixedEntry.fallback_price || 0;
      } else {
        px = fixedEntry.fallback_price || 0;
      }
    } else {
      // Plain scalar (CASH = 1.0, any legacy entries)
      px = fixedEntry;
    }
    totalVal += qty * px;
  });

  // Ghost analytics for activated tickers vs baselines
  const tc = policy?.ticker_constraints || {};
  const ghosts = [];

  Object.keys(tc).forEach(t => {
    const lc = tc[t]?.lifecycle;
    if (lc?.stage !== "activated") return;

    const T = String(t).trim().toUpperCase();
    const startStr = String(lc.entered_stage_date || "").trim();

    const baselineResults = {};
    (C.BASELINES || []).forEach(b => {
      baselineResults[b] = calculateAlphaAndCorrVsBenchmark_(rows, col, T, b, startStr);
    });

    let status = "ACTIVE";
    const daysActive = diffDaysYMD_(asOfDate, startStr);
    if (daysActive === null) status = "⚠️ BAD POLICY DATE";
    else if (daysActive < 14) status = "🆕 ONBOARDING";
    else {
      const daysLeft = Number(C.REVIEW_DAYS) - daysActive;
      status = (daysLeft < 0) ? "⚠️ REVIEW DUE" : `⏳ REVIEW IN ${daysLeft}d`;
    }

    ghosts.push({
      ticker: T,
      policyStart: startStr || "N/A",
      asOfDate: asOfDate,
      status: status,
      baselines: baselineResults
    });
  });

  // Performance log update
  try {
    const bl = policy?.governance?.reporting_baselines || {};
    const benches = (bl.active_benchmarks || []).map(x => String(x).trim().toUpperCase());

    if (!benches.length) {
      alerts.push("LOG: No active_benchmarks in policy; skipped perf-log update.");
    } else {
      const benchPrices = benches.map(b => priceByKey[`${asOfDate}|${b}`] || 0);
      if (benchPrices.every(p => p > 0)) {
        upsertAndRecomputePerformanceLog_(asOfDate, totalVal, benches, benchPrices, policy);
      } else {
        alerts.push(`LOG: Skipped perf-log update (missing benchmark price(s) on ${asOfDate}).`);
      }
    }
  } catch (e) {
    alerts.push(`LOG: ${e.message}`);
  }

  return { asOfDate, totalVal, ghosts };
}

function calculateAlphaAndCorrVsBenchmark_(rows, col, ticker, bench, startStr) {
  const C = getC_();
  const T = String(ticker).trim().toUpperCase();
  const B = String(bench).trim().toUpperCase();

  if (!startStr || !/^\d{4}-\d{2}-\d{2}$/.test(String(startStr))) {
    return { ok: false, err: "BAD_DATE", policyStart: startStr || "N/A", bench: B };
  }

  const tMap = new Map();
  const bMap = new Map();

  rows.forEach(r => {
    const d = String(r[col.date]);
    if (d < startStr) return;

    const sym = String(r[col.ticker]).trim().toUpperCase();
    const px = parseFloat(r[col.price]);
    if (!isFinite(px)) return;

    if (sym === T) tMap.set(d, px);
    if (sym === B) bMap.set(d, px);
  });

  if (tMap.size < 2 || bMap.size < 2) return { ok: false, err: "NO_DATA", policyStart: startStr, bench: B };

  const dates = intersectSortedKeys_(tMap, bMap);
  if (dates.length < 2) return { ok: false, err: "GAP", policyStart: startStr, bench: B };

  const d0 = dates[0];
  const dN = dates[dates.length - 1];

  const t0 = tMap.get(d0), tN = tMap.get(dN);
  const b0 = bMap.get(d0), bN = bMap.get(dN);

  const trT = (tN / t0) - 1;
  const trB = (bN / b0) - 1;
  const alpha = trT - trB;

  let corrNum = NaN;
  const x = [], y = [];
  for (let i = 1; i < dates.length; i++) {
    const prev = dates[i - 1];
    const cur = dates[i];

    const tp = tMap.get(prev), tc = tMap.get(cur);
    const bp = bMap.get(prev), bc = bMap.get(cur);
    if (!isFinite(tp) || !isFinite(tc) || !isFinite(bp) || !isFinite(bc)) continue;

    x.push((tc / tp) - 1);
    y.push((bc / bp) - 1);
  }

  if (x.length >= Number(C.MIN_CORR_RETURNS || 6)) {
    corrNum = pearson_(x, y);
  }

  const alphaStr = `${alpha >= 0 ? "🟢" : "🔴"} ${(alpha * 100).toFixed(1)}%`;
  const corrStr = (isFinite(corrNum))
    ? `${corrNum <= Number(C.CORR_ALERT_THRESHOLD) ? "🟢" : "🔴"} ${corrNum.toFixed(2)}`
    : `N/A`;

  return {
    ok: true,
    bench: B,
    policyStart: startStr,
    windowStart: d0,
    windowEnd: dN,
    alpha: alphaStr,
    corr: corrStr
  };
}

/**
 * ============================================================
 * EMAIL + 2-PANEL DASHBOARD
 * ============================================================
 */
function sendDailyEmail_(snapshot, charts, runDateStr, alerts) {
  const C = getC_();
  const subject = (alerts.length ? "⚠️ " : "✅ ") + `MWS Report: ${runDateStr}`;
  const headColor = alerts.length ? "#d32f2f" : "#2e7d32";

  // Use actual configured baselines — never hardcode ticker symbols here
  const b0 = (C.BASELINES && C.BASELINES[0]) ? String(C.BASELINES[0]).trim().toUpperCase() : null;
  const b1 = (C.BASELINES && C.BASELINES[1]) ? String(C.BASELINES[1]).trim().toUpperCase() : null;
  const b0Label = b0 ? `${benchDisplay_(b0)} (${b0})` : "Benchmark 1";
  const b1Label = b1 ? `${benchDisplay_(b1)} (${b1})` : "Benchmark 2";

  const minPolicyStart = getMinPolicyStart_(snapshot.ghosts) || snapshot.asOfDate;
  const rangeLabel = `(${fmtMDY_(minPolicyStart)}→${fmtMDY_(snapshot.asOfDate)})`;

  let html = `<div style="font-family: Arial, sans-serif; color:#333; max-width: 900px; margin:auto; border: 1px solid #eee; padding: 14px;">`;
  html += `<div style="border-bottom:2px solid #f0f0f0; padding-bottom:8px; margin-bottom:10px;">
      <div style="font-size:22px; font-weight:700; color:${headColor};">Titanium MWS Unified (v13.7)</div>
    </div>`;

  html += `<div style="font-size:16px; font-weight:700; color:#222; margin: 0 0 10px 0;">
      Portfolio: $${Number(snapshot.totalVal).toLocaleString(undefined, { minimumFractionDigits: 2 })}
    </div>`;

  if (alerts.length) {
    html += `<div style="background:#fff4f4; border-left:5px solid #d32f2f; padding:10px 12px; margin:10px 0 14px 0;">
      <b style="color:#d32f2f;">System alerts</b>
      <ul style="margin:6px 0 0 18px; padding:0; font-size:12px; color:#444;">${alerts.map(a => `<li>${escapeHtml_(a)}</li>`).join("")}</ul>
    </div>`;
  }

  if (snapshot.ghosts && snapshot.ghosts.length) {
    html += `<table style="width:100%; border-collapse:collapse; table-layout:fixed; font-size:13px; margin-bottom:12px;">
      <thead>
        <tr style="background:#f4f4f4; color:#666; text-align:left;">
          <th style="padding:10px; border:1px solid #eee; width:26%;">Ticker / Status</th>
          <th style="padding:10px; border:1px solid #eee; width:37%; text-align:center;" colspan="2">vs ${escapeHtml_(b0Label)}</th>
          <th style="padding:10px; border:1px solid #eee; width:37%; text-align:center;" colspan="2">vs ${escapeHtml_(b1Label)}</th>
        </tr>
        <tr style="background:#f8f8f8; color:#666; text-align:left;">
          <th style="padding:8px 10px; border:1px solid #eee; font-weight:600;">${escapeHtml_(rangeLabel)}</th>
          <th style="padding:8px 10px; border:1px solid #eee; font-weight:600;">Alpha</th>
          <th style="padding:8px 10px; border:1px solid #eee; font-weight:600;">Correlation</th>
          <th style="padding:8px 10px; border:1px solid #eee; font-weight:600;">Alpha</th>
          <th style="padding:8px 10px; border:1px solid #eee; font-weight:600;">Correlation</th>
        </tr>
      </thead>
      <tbody>`;

    snapshot.ghosts.forEach(g => {
      const r0 = b0 ? g.baselines[b0] : null;
      const r1 = b1 ? g.baselines[b1] : null;

      html += `<tr>
        <td style="padding:10px; border:1px solid #eee; vertical-align:top;">
          <b>${escapeHtml_(g.ticker)}</b><br>
          <span style="font-size:11px; color:#666;">${escapeHtml_(g.status)}</span>
        </td>

        <td style="padding:10px; border:1px solid #eee; vertical-align:top; white-space:nowrap;">${formatCellValue_(r0, "alpha")}</td>
        <td style="padding:10px; border:1px solid #eee; vertical-align:top; white-space:nowrap;">${formatCellValue_(r0, "corr")}</td>

        <td style="padding:10px; border:1px solid #eee; vertical-align:top; white-space:nowrap;">${formatCellValue_(r1, "alpha")}</td>
        <td style="padding:10px; border:1px solid #eee; vertical-align:top; white-space:nowrap;">${formatCellValue_(r1, "corr")}</td>
      </tr>`;
    });

    html += `</tbody></table>`;
  } else {
    html += `<div style="font-size:12px; color:#777;">No activated lifecycle tickers found in policy.</div>`;
  }

  if (charts && charts.perf) {
    html += `<div style="margin-top:10px; border-top:2px solid #f0f0f0; padding-top:12px; text-align:center;">
      <img src="cid:chart_perf" style="width:100%; height:auto; display:block; margin:auto;">
    </div>`;
  }
  if (charts && charts.alpha) {
    html += `<div style="margin-top:10px; text-align:center;">
      <img src="cid:chart_alpha" style="width:100%; height:auto; display:block; margin:auto;">
    </div>`;
  }

  html += `</div>`;

  const inlineImages = {};
  if (charts && charts.perf) inlineImages.chart_perf = charts.perf;
  if (charts && charts.alpha) inlineImages.chart_alpha = charts.alpha;

  MailApp.sendEmail({
    to: C.EMAIL_RECIPIENT,
    subject: subject,
    htmlBody: html,
    inlineImages: inlineImages
  });
}

function formatCellValue_(res, kind) {
  if (!res || !res.ok) return `<span style="color:#999;">${escapeHtml_(res?.err || "ERR")}</span>`;
  if (kind === "alpha") return escapeHtml_(res.alpha);
  if (kind === "corr") return escapeHtml_(res.corr);
  return `<span style="color:#999;">ERR</span>`;
}

/**
 * ============================================================
 * PERFORMANCE LOG (Drive CSV) - schema-stable, non-truncating
 * ============================================================
 */
/**
 * Returns the total scheduled cash flow for a given date from policy.scheduled_cash_flows.
 * Negative = withdrawal (e.g. SEPP), positive = contribution.
 * Supports recurrence: "annual" (same month/day every year) or "once" (exact date match).
 */
function getScheduledCashFlow_(dateStr, policy) {
  const flows = policy?.scheduled_cash_flows;
  if (!Array.isArray(flows) || !flows.length) return 0;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return 0;

  const year  = parseInt(dateStr.substring(0, 4), 10);
  const month = parseInt(dateStr.substring(5, 7), 10);
  const day   = parseInt(dateStr.substring(8, 10), 10);

  let total = 0;
  for (const flow of flows) {
    const amount = parseFloat(flow.amount);
    if (!isFinite(amount) || amount === 0) continue;

    const rec = String(flow.recurrence || "once").toLowerCase();
    if (rec === "annual") {
      if (parseInt(flow.month, 10) === month && parseInt(flow.day, 10) === day) {
        total += amount;
        console.log(`[CASHFLOW] Scheduled flow on ${dateStr}: ${flow.label || "unnamed"} ${amount}`);
      }
    } else {
      // "once" — exact date match
      const exactDate = String(flow.date || "").trim();
      if (exactDate === dateStr) {
        total += amount;
        console.log(`[CASHFLOW] Scheduled flow on ${dateStr}: ${flow.label || "unnamed"} ${amount}`);
      }
    }
  }
  return total;
}

function upsertAndRecomputePerformanceLog_(dateStr, portfolioVal, benches, benchPrices, policy) {
  const C = getC_();
  const file = DriveApp.getFileById(C.LOG_FILE_ID);
  const raw = file.getBlob().getDataAsString().replace(/^\ufeff/g, "").trim();
  const rows = raw ? Utilities.parseCsv(raw) : [];

  const b = policy?.governance?.reporting_baselines || {};
  const chartStart = String(b.chart_start_date || "").trim();

  const header = buildPerfLogHeader_(benches);

  let data = [];
  if (rows.length >= 2) {
    data = rows.slice(1)
      .map(r => normalizeRowWidth_(r, header.length))
      .filter(r => /^\d{4}-\d{2}-\d{2}$/.test(String(r[0] || "")));
  }

  // Preserve EventLabel values written by Python before deduping/sorting
  const elCol = header.indexOf("EventLabel");
  const savedLabels = {};
  if (elCol >= 0) {
    data.forEach(r => {
      const lbl = String(r[elCol] || "").trim();
      if (lbl) savedLabels[String(r[0])] = lbl;
    });
  }

  // Preserve CashFlow values entered manually (negative = withdrawal, positive = contribution)
  // GAS never auto-populates CashFlow; only preserves values already in the CSV.
  const cfCol = header.indexOf("CashFlow");
  const savedCFs = {};
  if (cfCol >= 0) {
    data.forEach(r => {
      const cf = parseFloat(r[cfCol]);
      if (isFinite(cf) && cf !== 0) savedCFs[String(r[0])] = cf;
    });
  }

  // Derive all column offsets from the actual header — never hardcode.
  // Must be done before the upsert block which writes bench prices by offset.
  const priceStartCol = header.indexOf(`Price_${benches[0]}`);
  if (priceStartCol === -1) throw new Error(`Perf log header missing Price_${benches[0]} column.`);
  const portPctCol   = header.indexOf("PortfolioPct");
  if (portPctCol === -1) throw new Error("Perf log header missing PortfolioPct column.");
  const pctStartCol  = header.indexOf(`Pct_${benches[0]}`);
  if (pctStartCol === -1) throw new Error(`Perf log header missing Pct_${benches[0]} column.`);
  const diffStartCol = header.indexOf(`Diff_${benches[0]}`);
  if (diffStartCol === -1) throw new Error(`Perf log header missing Diff_${benches[0]} column.`);

  // Upsert today's row
  data = data.filter(r => String(r[0]) !== dateStr);

  const newRow = new Array(header.length).fill("");
  newRow[0] = dateStr;
  newRow[1] = Number(portfolioVal).toFixed(2);
  // Auto-populate CashFlow from policy.scheduled_cash_flows (e.g. annual SEPP withdrawal).
  // Any manually-entered value already in the CSV is preserved via savedCFs above.
  const scheduledCF = savedCFs[dateStr] !== undefined
    ? savedCFs[dateStr]                          // manual override takes precedence
    : getScheduledCashFlow_(dateStr, policy);    // otherwise use policy schedule
  if (cfCol >= 0) newRow[cfCol] = String(scheduledCF || "0");
  for (let i = 0; i < benches.length; i++) {
    newRow[priceStartCol + i] = Number(benchPrices[i]).toFixed(2);
  }

  data.push(newRow);
  data.sort((a, b) => String(a[0]).localeCompare(String(b[0])));

  // Restore EventLabels after sort — GAS never writes these
  if (elCol >= 0) {
    data.forEach(r => {
      const lbl = savedLabels[String(r[0])];
      if (lbl) r[elCol] = lbl;
    });
  }

  // Restore CashFlow values after sort — GAS never auto-writes these
  if (cfCol >= 0) {
    data.forEach(r => {
      const cf = savedCFs[String(r[0])];
      if (cf !== undefined) r[cfCol] = String(cf);
    });
  }

  if (!data.length) throw new Error("Perf log has no rows after upsert.");

  // Benchmark baseline still comes from chart_start_date
  let baseIndex = -1;
  if (/^\d{4}-\d{2}-\d{2}$/.test(chartStart)) {
    baseIndex = data.findIndex(r => String(r[0]) === chartStart);
  }
  if (baseIndex === -1) baseIndex = 0;

  const baseB = benches.map((_, i) => parseFloat(data[baseIndex][priceStartCol + i]));
  if (baseB.some(x => !isFinite(x) || x <= 0)) {
    throw new Error("Perf log base benchmark price invalid.");
  }

  // Rolling recompute window: today + prior 5 calendar days
  // Use addDaysYMD_ (which respects script timezone) rather than raw Date arithmetic + UTC format,
  // which could shift by one day around midnight in non-UTC timezones.
  const winStartStr = addDaysYMD_(dateStr, -5, Session.getScriptTimeZone());

  let recalcStart = data.findIndex(r => String(r[0]) >= winStartStr);
  if (recalcStart === -1) recalcStart = data.length - 1;
  if (recalcStart < baseIndex) recalcStart = baseIndex;

  // Anchor from the row immediately before recalcStart
  let prevPV = NaN;
  let prevCumPort = NaN;

  if (recalcStart === baseIndex) {
    prevCumPort = 0.0; // first row in audited series will be reset below
  } else if (recalcStart > 0) {
    prevPV = parseFloat(data[recalcStart - 1][1]);
    prevCumPort = parseFloat(data[recalcStart - 1][portPctCol]);
  }

  for (let ri = 0; ri < data.length; ri++) {
    const row = normalizeRowWidth_(data[ri], header.length);

    // Rows before baseIndex are outside the audited chart window
    if (ri < baseIndex) {
      row[portPctCol] = "N/A";
      for (let i = 0; i < benches.length; i++) row[pctStartCol + i] = "N/A";
      for (let i = 0; i < benches.length; i++) row[diffStartCol + i] = "N/A";
      data[ri] = row;
      continue;
    }

    // Rows inside audited window but before rolling recompute window:
    // preserve PortfolioPct/Diff as stored, but refresh benchmark columns if blank
    if (ri < recalcStart) {
      for (let i = 0; i < benches.length; i++) {
        const px = parseFloat(row[priceStartCol + i]);
        const pB = (isFinite(px) && px > 0) ? ((px / baseB[i]) - 1) : NaN;
        row[pctStartCol + i] = isFinite(pB) ? pB.toFixed(4) : "N/A";
      }
      const pPort = parseFloat(row[portPctCol]);
      for (let i = 0; i < benches.length; i++) {
        const pB = parseFloat(row[pctStartCol + i]);
        row[diffStartCol + i] = (isFinite(pPort) && isFinite(pB)) ? (pPort - pB).toFixed(4) : "N/A";
      }
      data[ri] = row;
      continue;
    }

    const pv = parseFloat(row[1]);
    let pPort = NaN;

    if (ri === baseIndex) {
      pPort = 0.0;
    } else if (isFinite(pv) && isFinite(prevPV) && prevPV > 0 && isFinite(prevCumPort)) {
      // TWR: adjust denominator for any cash flow on this day.
      // CashFlow convention: negative = withdrawal (reduces denominator → higher return),
      //                      positive = contribution (increases denominator → lower return).
      // This correctly strips the effect of cash flows from the return calculation,
      // matching Chase's time-weighted return methodology.
      const cf = (cfCol >= 0) ? (parseFloat(row[cfCol]) || 0) : 0;
      const adjustedPrevPV = prevPV + cf;   // e.g. prevPV + (-45000) for a $45k withdrawal
      if (adjustedPrevPV > 0) {
        const dailyPortRet = (pv / adjustedPrevPV) - 1;
        pPort = (1 + prevCumPort) * (1 + dailyPortRet) - 1;
      } else {
        // Safety: if adjustment would make denominator ≤ 0, skip the daily return
        pPort = prevCumPort;
      }
    }

    row[portPctCol] = isFinite(pPort) ? pPort.toFixed(4) : "N/A";

    for (let i = 0; i < benches.length; i++) {
      const px = parseFloat(row[priceStartCol + i]);
      const pB = (isFinite(px) && px > 0) ? ((px / baseB[i]) - 1) : NaN;
      row[pctStartCol + i] = isFinite(pB) ? pB.toFixed(4) : "N/A";
    }

    for (let i = 0; i < benches.length; i++) {
      const pB = parseFloat(row[pctStartCol + i]);
      row[diffStartCol + i] = (isFinite(pPort) && isFinite(pB)) ? (pPort - pB).toFixed(4) : "N/A";
    }

    prevPV = pv;
    prevCumPort = pPort;
    data[ri] = row;
  }

  file.setContent(
    [header].concat(data)
      .map(r => normalizeRowWidth_(r, header.length).map(csvQuote_).join(","))
      .join("\n")
  );
}

function buildPerfLogHeader_(benches) {
  const h = ["Date", "PortfolioValue", "CashFlow"];  // CashFlow: negative = withdrawal, positive = contribution
  benches.forEach(b => h.push(`Price_${b}`));
  h.push("PortfolioPct");
  benches.forEach(b => h.push(`Pct_${b}`));
  benches.forEach(b => h.push(`Diff_${b}`));
  h.push("EventLabel");  // written by Python runner; never modified by GAS
  return h;
}

/**
 * ============================================================
 * CHARTS (2-panel dashboard from perf log CSV)
 * ============================================================
 */
function generateDashboardCharts2Panel_(policy) {
  const C = getC_();
  const b = policy?.governance?.reporting_baselines;
  if (!b) throw new Error("Policy missing governance.reporting_baselines");

  const raw = DriveApp.getFileById(C.LOG_FILE_ID).getBlob().getDataAsString().replace(/^\ufeff/g, "").trim();
  const parsed = raw ? Utilities.parseCsv(raw) : [];
  if (parsed.length < 3) throw new Error("Performance log too short (need >=2 rows).");

  const header = parsed[0].map(x => String(x || "").trim());
  const rows = parsed.slice(1);

  const benches = (b.active_benchmarks || []).map(x => String(x).trim().toUpperCase());
  if (!benches.length) throw new Error("No active_benchmarks in policy.");

  const portPctIdx = header.indexOf("PortfolioPct");
  if (portPctIdx === -1) throw new Error("Perf log missing PortfolioPct column.");

  const pctIdx = benches.map(sym => header.indexOf(`Pct_${sym}`));
  if (pctIdx.some(i => i === -1)) throw new Error("Perf log missing one or more Pct_<bench> columns.");

  const diffIdx = benches.map(sym => header.indexOf(`Diff_${sym}`));
  if (diffIdx.some(i => i === -1)) throw new Error("Perf log missing one or more Diff_<bench> columns.");

  const dr = rows.filter(r => String((r[portPctIdx] ?? "")).trim() !== "N/A");
  if (dr.length < 2) throw new Error("Performance log has <2 audited rows.");

  const perf = buildPerfPanelChart_(dr, benches, portPctIdx, pctIdx, b.chart_start_date);
  const alpha = buildAlphaPanelChart_(dr, benches, diffIdx, b.chart_start_date);

  return { perf, alpha };
}

// ── Date formatting helpers for chart x-axis ─────────────────────────────────
const MONTH_ABBR_ = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

/**
 * Formats a YYYY-MM-DD string as "Jan 05", "Jan 12", etc.
 */
function fmtChartDate_(ymd) {
  const m = parseInt(ymd.substring(5, 7), 10) - 1;
  const d = parseInt(ymd.substring(8, 10), 10);
  return `${MONTH_ABBR_[m]} ${String(d).padStart(2, "0")}`;
}

/**
 * Returns true if a YYYY-MM-DD date is a Monday.
 * Used to space x-axis labels one week apart.
 */
function isMonday_(ymd) {
  const [y, m, d] = ymd.split("-").map(Number);
  return new Date(y, m - 1, d).getDay() === 1;
}

// Display names for benchmark tickers — mirrors BENCH_DISPLAY_NAMES in mws_titanium_runner.py
const BENCH_DISPLAY_NAMES_ = {
  "SPY":  "S&P 500",
  "VTI":  "Total Market",
  "QQQ":  "Nasdaq-100",
  "ONEQ": "Nasdaq Composite",
  "IWM":  "Russell 2000",
  "DIA":  "Dow Jones",
  "EFA":  "Intl Developed",
  "EEM":  "Emerging Markets",
};
function benchDisplay_(ticker) {
  return BENCH_DISPLAY_NAMES_[String(ticker).trim().toUpperCase()] || String(ticker).trim().toUpperCase();
}

// Color palette — matches mws_titanium_runner.py exactly
const CHART_COLORS_ = {
  titanium: "#1f77b4",   // blue
  b0:       "#e69500",   // orange
  b1:       "#2ca02c",   // green
  bg:       "#f8f9fa",   // panel background
};

function buildPerfPanelChart_(dr, benches, portPctIdx, pctIdx, chartStartDate) {
  // Column order: benchmarks first (bottom fills), Titanium last (top fill).
  // Matches Python's layering: b1 fill → b0 fill → Titanium fill → lines on top.
  const dt = Charts.newDataTable().addColumn(Charts.ColumnType.STRING, "Date");

  // Benchmark series (drawn underneath Titanium)
  benches.forEach(t => {
    dt.addColumn(Charts.ColumnType.NUMBER, `${t} (${benchDisplay_(t)})`);
    dt.addColumn(Charts.ColumnType.STRING,  `${t}_ann`);
  });

  // Titanium series on top
  dt.addColumn(Charts.ColumnType.NUMBER, "Titanium (MWS)");
  dt.addColumn(Charts.ColumnType.STRING,  "T_ann");

  dr.forEach((r, i) => {
    const isLast = (i === dr.length - 1);
    const rawDate = String(r[0]).trim();  // YYYY-MM-DD
    // Show label only on Mondays (weekly cadence) or the last point; blank otherwise
    const dateLabel = (isMonday_(rawDate) || isLast) ? fmtChartDate_(rawDate) : "";

    const row = [dateLabel];

    // Benchmark columns first
    benches.forEach((_, j) => {
      const val = parseFloat(String(r[pctIdx[j]]).replace("%", ""));
      row.push(
        isFinite(val) ? val : null,
        (isLast && isFinite(val)) ? (val * 100).toFixed(2) + "%" : null
      );
    });

    // Titanium last
    const pPort = parseFloat(String(r[portPctIdx]).replace("%", ""));
    row.push(
      isFinite(pPort) ? pPort : null,
      (isLast && isFinite(pPort)) ? (pPort * 100).toFixed(2) + "%" : null
    );

    dt.addRow(row);
  });

  // Build column index list: Date + (value, annotation) pairs for each series
  const totalSeries = benches.length + 1;
  const cols = [0];
  for (let k = 0; k < totalSeries; k++) {
    cols.push(k * 2 + 1, { sourceColumn: k * 2 + 2, role: "annotation" });
  }

  // Colors: b0 (orange), b1 (green if present), Titanium (blue) — matches column order
  const seriesColors = [];
  if (benches.length > 0) seriesColors.push(CHART_COLORS_.b0);
  if (benches.length > 1) seriesColors.push(CHART_COLORS_.b1);
  seriesColors.push(CHART_COLORS_.titanium);

  return Charts.newAreaChart()
    .setDataTable(dt)
    .setDataViewDefinition(Charts.newDataViewDefinition().setColumns(cols))
    .setTitle(`Titanium Performance (Since ${chartStartDate || "Start"})  ·  TWR net of cash flows`)
    .setDimensions(1000, 420)
    .setOption("backgroundColor",  { fill: CHART_COLORS_.bg })
    .setOption("chartArea",         { backgroundColor: CHART_COLORS_.bg, left: 64, right: 130, top: 44, bottom: 36 })
    .setOption("colors",            seriesColors)
    .setOption("areaOpacity",       0.15)
    .setOption("lineWidth",         2)
    .setOption("isStacked",         false)
    .setOption("legend",            { position: "top", textStyle: { fontSize: 11, color: "#222222" } })
    .setOption("vAxis",             {
      format: "percent",
      gridlines: { color: "#cccccc", count: 6 },
      textStyle: { fontSize: 9, color: "#444" }
    })
    .setOption("hAxis",             {
      showTextEvery: 1,   // force every tick to render; blanks stay blank, only Mondays show
      slantedText: true,
      slantedTextAngle: 45,
      textStyle: { fontSize: 9, color: "#333" },
      gridlines: { color: "#dddddd" }
    })
    .setOption("annotations",       { textStyle: { fontSize: 10, bold: true, color: "#222" } })
    .build()
    .getAs("image/png");
}

function buildAlphaPanelChart_(dr, benches, diffIdx, chartStartDate) {
  const dt = Charts.newDataTable().addColumn(Charts.ColumnType.STRING, "Date");

  benches.forEach(t => {
    dt.addColumn(Charts.ColumnType.NUMBER, `vs ${benchDisplay_(t)}`);
    dt.addColumn(Charts.ColumnType.STRING,  `vs ${t}_ann`);
  });

  dr.forEach((r, i) => {
    const isLast = (i === dr.length - 1);
    const rawDate = String(r[0]).trim();  // YYYY-MM-DD
    const dateLabel = (isMonday_(rawDate) || isLast) ? fmtChartDate_(rawDate) : "";

    const row = [dateLabel];
    benches.forEach((t, j) => {
      const v = parseFloat(String(r[diffIdx[j]]).replace("%", ""));
      row.push(
        isFinite(v) ? v : null,
        (isLast && isFinite(v)) ? (v * 100).toFixed(2) + "%" : null
      );
    });

    dt.addRow(row);
  });

  const cols = [0];
  for (let k = 0; k < benches.length; k++) {
    cols.push(k * 2 + 1, { sourceColumn: k * 2 + 2, role: "annotation" });
  }

  const seriesColors = [];
  if (benches.length > 0) seriesColors.push(CHART_COLORS_.b0);
  if (benches.length > 1) seriesColors.push(CHART_COLORS_.b1);

  return Charts.newAreaChart()
    .setDataTable(dt)
    .setDataViewDefinition(Charts.newDataViewDefinition().setColumns(cols))
    .setTitle(`Cumulative Alpha vs. Benchmarks (Since ${chartStartDate || "Start"})`)
    .setDimensions(1000, 320)
    .setOption("backgroundColor",  { fill: CHART_COLORS_.bg })
    .setOption("chartArea",         { backgroundColor: CHART_COLORS_.bg, left: 64, right: 130, top: 44, bottom: 36 })
    .setOption("colors",            seriesColors)
    .setOption("areaOpacity",       0.15)
    .setOption("lineWidth",         2)
    .setOption("isStacked",         false)
    .setOption("legend",            { position: "top", textStyle: { fontSize: 11, color: "#222222" } })
    .setOption("vAxis",             {
      format: "percent",
      gridlines: { color: "#cccccc", count: 5 },
      baseline: 0,
      baselineColor: "#888888",
      textStyle: { fontSize: 9, color: "#444" }
    })
    .setOption("hAxis",             {
      showTextEvery: 1,   // force every tick to render; blanks stay blank, only Mondays show
      slantedText: true,
      slantedTextAngle: 45,
      textStyle: { fontSize: 9, color: "#333" },
      gridlines: { color: "#dddddd" }
    })
    .setOption("annotations",       { textStyle: { fontSize: 10, bold: true, color: "#222" } })
    .build()
    .getAs("image/png");
}

/**
 * ============================================================
 * LIVE PRICES (batch) - scratch sheet reuse
 * ============================================================
 */
function getBatchPricesScratch_(tickers) {
  const scratch = openScratch_();
  const sh = ensureScratchSheet_(scratch);

  sh.clearContents();

  tickers.forEach((t, i) => {
    sh.getRange(i + 1, 1).setValue(t);
    let formula;
    try { formula = `=IFERROR(GOOGLEFINANCE("${sanitizeTicker_(t)}"), 0)`; }
    catch (_) { formula = `=0`; }  // synthetic ticker (e.g. TREASURY_NOTE) — no live price
    sh.getRange(i + 1, 2).setFormula(formula);
  });

  SpreadsheetApp.flush();
  Utilities.sleep(1200);

  const data = sh.getRange(1, 1, tickers.length, 2).getValues();
  sh.clearContents();

  const out = {};
  data.forEach(r => {
    const k = String(r[0] || "").trim().toUpperCase();
    const v = parseFloat(r[1]);
    if (k) out[k] = isFinite(v) ? v : 0;
  });

  return out;
}

function openScratch_() {
  const C = getC_();
  if (!C.SCRATCH_SHEET_ID || String(C.SCRATCH_SHEET_ID).includes("PUT_YOUR")) {
    throw new Error("C.SCRATCH_SHEET_ID is not set. Provide your existing scratch spreadsheet ID.");
  }
  return SpreadsheetApp.openById(C.SCRATCH_SHEET_ID);
}

function ensureScratchSheet_(ss) {
  const C = getC_();
  let sh = ss.getSheetByName(C.SCRATCH_SHEET_NAME);
  if (!sh) sh = ss.insertSheet(C.SCRATCH_SHEET_NAME);
  sh.clearContents();
  return sh;
}

/**
 * ============================================================
 * HELPERS
 * ============================================================
 */
function buildColumnMap_(header) {
  const h = header.map(v => String(v).trim().toLowerCase());
  const dateIdx = h.indexOf("date");
  const tickerIdx = h.indexOf("ticker");

  let priceIdx = h.indexOf("adjclose");
  if (priceIdx === -1) priceIdx = h.indexOf("close");
  if (priceIdx === -1) priceIdx = h.findIndex(x => x.includes("adjclose") || x.includes("close") || x.includes("price"));

  if (dateIdx === -1 || tickerIdx === -1 || priceIdx === -1) {
    throw new Error(`HIST header missing required columns. date=${dateIdx}, ticker=${tickerIdx}, price=${priceIdx}`);
  }
  return { date: dateIdx, ticker: tickerIdx, price: priceIdx };
}

function normalizeRowWidth_(row, width) {
  const r = Array.isArray(row) ? row.slice() : [];
  while (r.length < width) r.push("");
  if (r.length > width) r.length = width;
  return r;
}

function normalizeInventory_(state) {
  const raw = state.inventory || state.tickers || [];
  return [...new Set(
    raw.map(item => {
      const t = (typeof item === "string") ? item : item?.ticker;
      return t ? String(t).trim().toUpperCase() : null;
    }).filter(Boolean)
  )].sort();
}

function addDaysYMD_(ymd, deltaDays, tz) {
  const d = ymdToLocalDate_(ymd);
  d.setDate(d.getDate() + deltaDays);
  return Utilities.formatDate(d, tz, "yyyy-MM-dd");
}

function ymdToLocalDate_(ymd) {
  const parts = String(ymd).split("-").map(Number);
  return new Date(parts[0], parts[1] - 1, parts[2]);
}

function diffDaysYMD_(laterYMD, earlierYMD) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(laterYMD))) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(earlierYMD))) return null;
  const a = ymdToLocalDate_(earlierYMD);
  const b = ymdToLocalDate_(laterYMD);
  return Math.floor((b.getTime() - a.getTime()) / 86400000);
}

function intersectSortedKeys_(m1, m2) {
  const out = [];
  m1.forEach((_, k) => { if (m2.has(k)) out.push(k); });
  out.sort();
  return out;
}

function pearson_(x, y) {
  const n = Math.min(x.length, y.length);
  if (n < 2) return NaN;

  let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0, sumY2 = 0;
  for (let i = 0; i < n; i++) {
    const a = x[i], b = y[i];
    sumX += a; sumY += b;
    sumXY += a * b;
    sumX2 += a * a;
    sumY2 += b * b;
  }
  const num = (n * sumXY) - (sumX * sumY);
  const den = Math.sqrt(((n * sumX2) - (sumX * sumX)) * ((n * sumY2) - (sumY * sumY)));
  return den === 0 ? NaN : (num / den);
}

/**
 * Quotes a CSV field value if it contains a comma, double-quote, or newline.
 * Prevents EventLabel values like "FOMC, Rate Hike" from corrupting CSV structure on re-read.
 */
/**
 * Validates a ticker string before embedding it in a GOOGLEFINANCE formula.
 * Rejects anything that isn't alphanumeric + hyphens/periods (standard ticker chars).
 * Prevents formula injection if a malformed ticker somehow enters the system.
 */
function sanitizeTicker_(t) {
  const s = String(t || "").trim().toUpperCase();
  // Allow: letters, digits, dot, hyphen, colon (for exchange-prefixed tickers like INDEXCBOE:VIX, CURRENCY:USDEUR)
  // Reject: spaces, quotes, backticks, $, =, +, parens, and anything else that could be formula injection
  if (!/^[A-Z0-9.\-:]{1,30}$/.test(s)) {
    throw new Error(`Ticker "${s}" failed safety validation for GOOGLEFINANCE formula injection.`);
  }
  return s;
}

function csvQuote_(val) {
  const s = String(val === null || val === undefined ? "" : val);
  if (s.includes(",") || s.includes('"') || s.includes("\n") || s.includes("\r")) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function escapeHtml_(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function getMinPolicyStart_(ghosts) {
  let min = null;
  (ghosts || []).forEach(g => {
    const s = String(g.policyStart || "").trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return;
    if (!min || s < min) min = s;
  });
  return min;
}

function fmtMDY_(ymd) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(ymd))) return String(ymd || "");
  const Y = ymd.substring(0, 4), M = ymd.substring(5, 7), D = ymd.substring(8, 10);
  return `${M}/${D}/${Y}`;
}
