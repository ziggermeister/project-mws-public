function runVNextContractCheck() {
  // Your provided Drive file IDs
  var POLICY_FILE_ID  = "1KRv0QSAT6EYsYuV06F23SuJ2Q7tNv1Xr"; // mws_policy_vnext.json
  var TRACKER_FILE_ID = "1Fuc-nAxuhopwvhDQYO7qamcK4ra5Tu0l"; // mws_tracker.json

  // Hard-fail if anything violates the contract
  var bundle = mwsLoadValidateAndProject(POLICY_FILE_ID, TRACKER_FILE_ID, { hardFail: true });

  // Print a human-readable report
  mwsPrintContractReport(bundle);

  // Optional: log a couple high-signal derived views
  console.log("Momentum eligible tickers: " + bundle.derived.eligibility.momentumEligible.join(", "));
  console.log("Overlay tickers: " + bundle.derived.eligibility.overlays.join(", "));
  console.log("Reference tickers: " + bundle.derived.eligibility.references.join(", "));

  // Example: inspect one ticker’s mapping rollups
  // console.log(JSON.stringify(bundle.derived.tickers["IBIT"], null, 2));
}
