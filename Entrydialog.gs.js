/**
 * Entry Dialogue: driven by a cadence-based "what's next" queue rather
 * than a single route+day picked from wherever your cursor was on canBuy.
 *
 * Opening the dialogue shows nothing until you click "Get Next Flights".
 * That (and the moment right after every successful Log) calls
 * getNextBatch(), which:
 *   - evaluates every route's flights for TODAY against the cadence
 *     settings (see NEXT_UP_SETTINGS_KEY_ below) to find the single
 *     most-due one
 *   - returns THAT route's whole day (every departure for that exact
 *     org/dest pair, past the 45-min hard cutoff) - same as the dialogue
 *     always showed for one route; opportunistic grabbing happens within
 *     that route's other departures, not across every route on one page
 *   - if nothing anywhere is currently due, rows is empty and waitMinutes
 *     says how long until something will be
 *
 * Cadence settings are user-editable from the dialogue's own settings
 * panel and persist in PropertiesService (see loadNextUpSettings_ /
 * saveNextUpSettings below), not hardcoded and not in a visible sheet.
 * A flight is "due" once it sits inside a tier's window AND either it's
 * never been logged today, or enough real time (recheckGapHours) has
 * passed since its most recent reading today. This is evaluated fresh
 * every single call - no phase/state to track across calls, so a flight
 * can't get stuck in a stale tier.
 *
 * A small pencil per row still unlocks that row's departure time, aircraft
 * config, and flight number for editing, writing straight back to the
 * matching FlightSchedule row.
 */

var FLIGHT_SCHEDULE_SHEET_NAME = 'FlightSchedule';
var AIRCRAFT_CONFIGS_SHEET_NAME = 'AircraftConfigs';

// FlightSchedule column order, as built: carrier, flightNumber, org, dest,
// dayOfWeek, depTime, aircraftConfig - single header row.
var FS_COL_CARRIER = 1;
var FS_COL_FLIGHT_NUMBER = 2;
var FS_COL_ORG = 3;
var FS_COL_DEST = 4;
var FS_COL_DAY_OF_WEEK = 5;
var FS_COL_DEP_TIME = 6;
var FS_COL_AIRCRAFT_CONFIG = 7;

// Delta stops showing/offering a flight this close to departure - same
// hard stop walkUpcomingFlights already respects.
var DEP_CUTOFF_MINUTES_ = 45;

// Cadence settings (tier windows, recheck gaps, "log everything" override)
// live in PropertiesService, not hardcoded and not in Legend - editable
// live from the dialogue itself, sticks across opens/closes since it's a
// real persistent store, and doesn't clutter any visible sheet. Defaults
// below are gut-feel starting numbers, not derived - tune from the
// dialogue as needed.
//
// Near tier is fundamentally different from far tiers and stays without a
// targetHours/doneToleranceHours: it's the actual go/no-go window right up
// to the 45-min cutoff, where a close reading does NOT mean "done for the
// day" - it just keeps recheck-gap cycling like before.
//
// A far tier (targetHours + doneToleranceHours present) can become
// PERMANENTLY done for a flight today: once any reading logged today
// lands within doneToleranceHours of targetHours, that tier closes for
// that flight for the rest of the day, regardless of whether the normal
// recheckGapHours cooldown would otherwise have expired.
var NEXT_UP_SETTINGS_KEY_ = 'nextUpSettings';
var DEFAULT_NEXT_UP_SETTINGS_ = {
  tiers: [
    { minHours: 0, maxHours: 2, recheckGapHours: 0.5 },
    { minHours: 3.5, maxHours: 4.5, targetHours: 4, doneToleranceHours: 0.3, recheckGapHours: 1 }
  ],
  logEverything: false
};

function loadNextUpSettings_() {
  var stored = PropertiesService.getDocumentProperties().getProperty(NEXT_UP_SETTINGS_KEY_);
  if (!stored) return DEFAULT_NEXT_UP_SETTINGS_;
  try {
    return JSON.parse(stored);
  } catch (e) {
    return DEFAULT_NEXT_UP_SETTINGS_;
  }
}

/**
 * Called from the dialogue's settings panel to persist changes. No
 * trailing underscore - this one has to be callable from google.script.run.
 */
function saveNextUpSettings(settings) {
  PropertiesService.getDocumentProperties().setProperty(NEXT_UP_SETTINGS_KEY_, JSON.stringify(settings));
  return { saved: true };
}

/**
 * Reads AircraftConfigs into { configKey: hasD1 (boolean) }, so the dialog
 * knows whether to show a D1 input box for a given row's aircraft - per
 * Delta's own convention (show the column only when the plane has D1 seats)
 * rather than a manual show/hide toggle.
 */
function loadAircraftD1Map_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(AIRCRAFT_CONFIGS_SHEET_NAME);
  var map = {};
  if (!sheet) return map;
  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    var key = data[i][0];
    var d1 = data[i][2];
    if (!key) continue;
    map[String(key).toLowerCase()] = (typeof d1 === 'number' && d1 > 0);
  }
  return map;
}

// Every distinct aircraft config key in AircraftConfigs, in sheet order -
// fed into the dialog as a <datalist> so typing into the aircraft field
// offers real suggestions instead of requiring the exact string from memory.
function loadAircraftConfigOptions_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(AIRCRAFT_CONFIGS_SHEET_NAME);
  var options = [];
  if (!sheet) return options;
  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    var key = data[i][0];
    if (key) options.push(String(key));
  }
  return options;
}

// Converts a 24h hhmm integer to a "7:48 am"-style string for display,
// matching Delta's own site instead of canBuy's internal 24h storage.
function hhmmTo12Hour_(hhmm) {
  if (hhmm === '' || hhmm === null || hhmm === undefined) return '';
  var h = Math.floor(hhmm / 100), m = hhmm % 100;
  var period = h >= 12 ? 'pm' : 'am';
  var h12 = h % 12; if (h12 === 0) h12 = 12;
  return h12 + ':' + ('0' + m).slice(-2) + ' ' + period;
}

/**
 * Menu entry point ("Log CanBuy Seats"). No longer reads anything from the
 * active canBuy row - the dialog opens empty; "Get Next Flights"
 * (client-side) is what actually populates it via getNextBatch().
 */
function openEntryDialog() {
  var ui = SpreadsheetApp.getUi();
  var initData = { aircraftOptions: loadAircraftConfigOptions_() };
  var raw = HtmlService.createHtmlOutputFromFile('EntryDialog').getContent();
  var withData = raw.replace('__INIT_DATA__', JSON.stringify(initData));
  var html = HtmlService.createHtmlOutput(withData).setWidth(760).setHeight(600);
  ui.showModalDialog(html, 'CanBuy');
}

// Scans today's canBuy rows (matching flightYr/flightMMDD exactly) into a
// flat list of {org, dest, depEtMinutes, hrs, y, cplus, onePS, d1}, so
// getFlightReadingsToday_/getPreviousReadingsForRow_ can pull out every
// reading logged so far today for a given flight - same fuzzy org/dest/dep
// match (converted to ET-equivalent minutes, +/-30min tolerance) used
// everywhere else in this project. Raw seat values are carried alongside
// hrs so the dialogue can show what was actually seen on each prior
// reading, not just how long ago it was.
function getTodaysReadings_(canBuySheet, cols, flightYr, flightMMDD, offsets) {
  // See findLastDataRow_ in Code.gs - same getLastRow() vs. getMaxRows() fix.
  var lastRow = canBuySheet.getLastRow();
  var fields = ['org', 'dest', 'dep', 'flightYr', 'flightMMDD', 'hoursBeforeDep',
      'availMain', 'availCPlus', 'availOnePS', 'availD1'];
  var colData = {};
  fields.forEach(function (f) {
    colData[f] = canBuySheet.getRange(1, cols[f], lastRow, 1).getValues();
  });
  var readings = [];
  for (var r = 0; r < lastRow; r++) {
    var org = colData.org[r][0], dest = colData.dest[r][0], dep = colData.dep[r][0];
    if (org === '' || org === null || dest === '' || dest === null || dep === '' || dep === null) continue;
    if (colData.flightYr[r][0] !== flightYr || colData.flightMMDD[r][0] !== flightMMDD) continue;
    var hrs = colData.hoursBeforeDep[r][0];
    if (typeof hrs !== 'number') continue;
    readings.push({
      org: String(org).toLowerCase(), dest: String(dest).toLowerCase(),
      depEtMinutes: toEtMinutes_(dep, org, offsets), hrs: hrs,
      y: colData.availMain[r][0], cplus: colData.availCPlus[r][0],
      onePS: colData.availOnePS[r][0], d1: colData.availD1[r][0]
    });
  }
  return readings;
}

// Every hoursBeforeDep value already logged today for this exact flight
// (fuzzy org/dest/dep match), in no particular order. Needed as a full
// list (not just the closest one) so a far tier's done-tolerance check can
// scan every reading, since the close-enough one might not be the most
// recent.
function getFlightReadingsToday_(readings, org, dest, depEtMinutes) {
  var hrsList = [];
  readings.forEach(function (rd) {
    if (rd.org !== org || rd.dest !== dest) return;
    if (Math.abs(rd.depEtMinutes - depEtMinutes) > FULL_MATCH_WINDOW_MINUTES_) return;
    hrsList.push(rd.hrs);
  });
  return hrsList;
}

// Every reading logged today for this exact flight, WITH raw seat values,
// sorted most-recent-check first (ascending hrs, since hrs counts down as
// departure approaches) - what the dialogue displays per row so he can see
// what's already been seen today before logging another check.
function getPreviousReadingsForRow_(readings, org, dest, depEtMinutes) {
  var matches = readings.filter(function (rd) {
    if (rd.org !== org || rd.dest !== dest) return false;
    return Math.abs(rd.depEtMinutes - depEtMinutes) <= FULL_MATCH_WINDOW_MINUTES_;
  });
  matches.sort(function (a, b) { return a.hrs - b.hrs; });
  return matches.map(function (rd) {
    return { hrs: rd.hrs, y: rd.y, cplus: rd.cplus, onePS: rd.onePS, d1: rd.d1 };
  });
}

// Evaluates a tier list for one flight against its current hoursUntilDep
// and every reading logged for it today. Stateless: a flight is eligible
// the moment it sits inside ANY tier's window, and which tier that ends up
// being falls straight out of this calculation - no phase to track across
// calls.
//
// Each tier's window can be missed permanently for a given flight today:
// since hoursUntilDep only ever decreases, once it drops below a tier's
// minHours without having been caught inside that window, that tier can
// never open for this flight again today (a real gap, not "eventually").
//
// A tier with targetHours + doneToleranceHours set can ALSO close
// permanently the other way: if any reading today already landed within
// doneToleranceHours of targetHours, that tier contributes nothing further
// for this flight today, regardless of the normal recheckGapHours cooldown.
//
// minutesUntilEligible is the soonest a still-reachable window opens, or
// null if every window is already closed for this flight (either passed
// or done) - it'll only ever be picked up again if a still-open window
// exists on a different tier.
function evaluateNextUpEligibility_(hoursUntilDep, todaysHrsList, tiers) {
  var lastLoggedHours = todaysHrsList.length > 0 ? Math.min.apply(null, todaysHrsList) : null;
  var eligibleNow = false;
  var minutesUntilEligible = null;

  tiers.forEach(function (tier) {
    if (tier.targetHours !== undefined && tier.doneToleranceHours !== undefined) {
      var alreadyDone = todaysHrsList.some(function (hrs) {
        return Math.abs(hrs - tier.targetHours) <= tier.doneToleranceHours;
      });
      if (alreadyDone) return; // permanently closed for this flight today
    }

    var ceiling = tier.maxHours;
    if (lastLoggedHours !== null) {
      ceiling = Math.min(ceiling, lastLoggedHours - tier.recheckGapHours);
    }
    if (hoursUntilDep >= tier.minHours && hoursUntilDep <= ceiling) {
      eligibleNow = true;
    } else if (hoursUntilDep > ceiling) {
      // Hasn't reached this window's ceiling yet - still reachable later today.
      var mins = Math.round((hoursUntilDep - ceiling) * 60);
      if (minutesUntilEligible === null || mins < minutesUntilEligible) minutesUntilEligible = mins;
    }
    // else hoursUntilDep < tier.minHours: already past this window for today - no contribution.
  });

  if (eligibleNow) minutesUntilEligible = 0;
  return { eligibleNow: eligibleNow, minutesUntilEligible: minutesUntilEligible };
}

/**
 * Called by the client on "Get Next Flights" and again automatically right
 * after every successful Log. The cadence rules evaluate every route
 * today to find the single most-due flight, then the response scopes down
 * to THAT flight's whole route+day (every departure for that exact
 * org/dest pair today, same as the dialogue always showed for one route -
 * opportunistic grabbing happens within a route's other departures, not
 * across every route at once). If nothing is due anywhere, rows is empty
 * and waitMinutes says how long until something will be.
 *
 * skipRouteKeys: optional array of "org|dest" strings the client wants
 * passed over for now (see "Skip this route" client-side - it's a
 * session-only list, never written anywhere, so it resets the moment the
 * dialogue is closed and reopened). Only affects WHICH route gets picked
 * next - a skipped route still counts normally toward the wait-time
 * fallback if nothing eligible is left.
 */
function getNextBatch(skipRouteKeys) {
  var skipSet = {};
  (skipRouteKeys || []).forEach(function (k) { skipSet[k] = true; });

  var settings = loadNextUpSettings_();

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var canBuySheet = ss.getSheetByName('canBuy');
  var schedSheet = ss.getSheetByName(FLIGHT_SCHEDULE_SHEET_NAME);
  var cols = getCanBuyColumnMap_(canBuySheet);

  var offsets = getAirportOffsets_();
  var d1Map = loadAircraftD1Map_();

  var nowInfo = getEasternNow_();
  var nowMonth = Math.floor(nowInfo.checkMMDD / 100), nowDay = nowInfo.checkMMDD % 100;
  var nowAbsMinutes = dayEpochMinutes_(nowInfo.checkYYYY, nowMonth, nowDay) +
      Math.floor(nowInfo.rawHHMM / 100) * 60 + (nowInfo.rawHHMM % 100);
  var todayEpochMinutes = dayEpochMinutes_(nowInfo.checkYYYY, nowMonth, nowDay);
  var dow = dowAbbrev_(new Date(nowInfo.checkYYYY, nowMonth - 1, nowDay));

  var readings = getTodaysReadings_(canBuySheet, cols, nowInfo.checkYYYY, nowInfo.checkMMDD, offsets);

  var dowDisplay = dow.charAt(0).toUpperCase() + dow.slice(1);
  var dateDisplay = ('0000' + nowInfo.checkMMDD).slice(-4);

  // Pass 1: every route today, past the 45-min hard cutoff (Delta stops
  // showing these - same as walkUpcomingFlights, and this ALWAYS applies
  // even in logEverything mode, since it's a real platform limit, not a
  // cadence choice), used only to find which ONE route is most due right
  // now.
  var schedData = schedSheet.getDataRange().getValues();
  var candidates = [];
  var pastCutoffCount = 0;

  for (var i = 1; i < schedData.length; i++) {
    var r = schedData[i];
    var rOrg = r[FS_COL_ORG - 1], rDest = r[FS_COL_DEST - 1], rDow = r[FS_COL_DAY_OF_WEEK - 1], rDep = r[FS_COL_DEP_TIME - 1];
    if (!rOrg || !rDest || !rDow || rDep === '' || rDep === null || rDep === undefined) continue;
    if (String(rDow).toLowerCase().substring(0, 3) !== dow) continue;

    var org = String(rOrg).toLowerCase(), dest = String(rDest).toLowerCase();
    var depEtMinutes = toEtMinutes_(rDep, rOrg, offsets);
    var flightAbsMinutes = todayEpochMinutes + depEtMinutes;
    var hoursUntilDep = (flightAbsMinutes - nowAbsMinutes) / 60;
    if (hoursUntilDep * 60 <= DEP_CUTOFF_MINUTES_) { pastCutoffCount++; continue; }

    var elig;
    if (settings.logEverything) {
      elig = { eligibleNow: true, minutesUntilEligible: 0 };
    } else {
      var todaysHrsList = getFlightReadingsToday_(readings, org, dest, depEtMinutes);
      elig = evaluateNextUpEligibility_(hoursUntilDep, todaysHrsList, settings.tiers);
    }

    candidates.push({
      scheduleRow: i + 1, org: org, dest: dest,
      car: String(r[FS_COL_CARRIER - 1] || 'dl').toLowerCase(),
      dep: rDep, depEtMinutes: depEtMinutes,
      aircraftConfig: r[FS_COL_AIRCRAFT_CONFIG - 1] || 'TBD',
      flightNumber: r[FS_COL_FLIGHT_NUMBER - 1] || '',
      hoursUntilDep: hoursUntilDep,
      eligibleNow: elig.eligibleNow, minutesUntilEligible: elig.minutesUntilEligible
    });
  }

  candidates.sort(function (a, b) { return a.depEtMinutes - b.depEtMinutes; });

  // Top pick across every route: the soonest-departing currently-eligible
  // flight NOT on the skip list. Tier priority falls out of the sort for
  // free - a flight only becomes eligible under the tighter T-2 tier once
  // it's genuinely within 2h, which is already earlier in this sort than
  // anything only eligible under the farther tier.
  var nextCandidate = candidates.filter(function (c) {
    return c.eligibleNow && !skipSet[c.org + '|' + c.dest];
  })[0];

  if (!nextCandidate) {
    var futureWaits = candidates
        .map(function (c) { return c.minutesUntilEligible; })
        .filter(function (m) { return m !== null; });
    var waitMinutes = futureWaits.length > 0 ? Math.min.apply(null, futureWaits) : null;
    var waitMessage = waitMinutes !== null
        ? 'Next flight due for a check in ' + waitMinutes + ' min.'
        : 'Nothing left to check today.';
    return {
      dowDisplay: dowDisplay, dateDisplay: dateDisplay,
      flightYr: nowInfo.checkYYYY, flightMMDD: nowInfo.checkMMDD,
      routeOrg: null, routeDest: null, summaryText: '',
      waitMinutes: waitMinutes, waitMessage: waitMessage, rows: [],
      aircraftOptions: loadAircraftConfigOptions_(), settings: settings
    };
  }

  // Pass 2: every departure for THAT ONE route today (past the same
  // cutoff), building the actual rows to display - the dialogue is still
  // one-route-at-a-time, cadence just picks which route automatically now.
  var routeRows = candidates
      .filter(function (c) { return c.org === nextCandidate.org && c.dest === nextCandidate.dest; })
      .map(function (c) {
        return {
          scheduleRow: c.scheduleRow, org: c.org, dest: c.dest, car: c.car,
          dep: c.dep, depDisplay: hhmmTo12Hour_(c.dep),
          flightNumber: c.flightNumber, aircraftConfig: c.aircraftConfig,
          hasD1: !!d1Map[String(c.aircraftConfig).toLowerCase()],
          hoursUntilDep: Math.round(c.hoursUntilDep * 10) / 10,
          isNext: c.scheduleRow === nextCandidate.scheduleRow,
          previousReadings: getPreviousReadingsForRow_(readings, c.org, c.dest, c.depEtMinutes)
        };
      });

  // Route-specific "already left" count (matching the route this batch is
  // actually showing, not a global count across every other route).
  var routeDepartedCount = 0;
  for (var j = 1; j < schedData.length; j++) {
    var rr = schedData[j];
    var rrOrg = rr[FS_COL_ORG - 1], rrDest = rr[FS_COL_DEST - 1], rrDow = rr[FS_COL_DAY_OF_WEEK - 1], rrDep = rr[FS_COL_DEP_TIME - 1];
    if (!rrOrg || !rrDest || !rrDow || rrDep === '' || rrDep === null || rrDep === undefined) continue;
    if (String(rrDow).toLowerCase().substring(0, 3) !== dow) continue;
    if (String(rrOrg).toLowerCase() !== nextCandidate.org || String(rrDest).toLowerCase() !== nextCandidate.dest) continue;
    var rrDepEt = toEtMinutes_(rrDep, rrOrg, offsets);
    var rrHoursUntil = (todayEpochMinutes + rrDepEt - nowAbsMinutes) / 60;
    if (rrHoursUntil * 60 <= DEP_CUTOFF_MINUTES_) routeDepartedCount++;
  }

  var summaryText = routeDepartedCount > 0
      ? routeDepartedCount + ' flight' + (routeDepartedCount === 1 ? '' : 's') + ' already left, not shown'
      : '';

  return {
    dowDisplay: dowDisplay, dateDisplay: dateDisplay,
    flightYr: nowInfo.checkYYYY, flightMMDD: nowInfo.checkMMDD,
    routeOrg: nextCandidate.org, routeDest: nextCandidate.dest,
    summaryText: summaryText, waitMinutes: null, rows: routeRows,
    aircraftOptions: loadAircraftConfigOptions_(), settings: settings
  };
}

/**
 * Called from the dialog's "Log these readings" button.
 *
 * payload: {
 *   flightYr, flightMMDD,
 *   entries: [{ scheduleRow, org, dest, car, dep (24h int), aircraftConfig,
 *               flightNumber, scheduleEdited (bool),
 *               y, cplus, onePS, d1 (each '' or a number as typed) }]
 * }
 *
 * Rows now span multiple routes (and, now that FlightSchedule has a real
 * carrier column, potentially multiple carriers too) - no single org/dest/
 * car for the whole batch anymore, so all three travel per-entry instead
 * of once at the payload level. Everything else - append-only canBuy,
 * batched setValues()/setFormulas() per column, dow/hoursBeforeDep/
 * totalSeats as live formulas - unchanged from before.
 */
function saveEntryDialog(payload) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var canBuySheet = ss.getSheetByName('canBuy');
  var schedSheet = ss.getSheetByName(FLIGHT_SCHEDULE_SHEET_NAME);
  var cols = getCanBuyColumnMap_(canBuySheet);

  var offsets = getAirportOffsets_();
  var readingsIndex = buildReadingsIndex_(canBuySheet, cols);

  // Schedule fixes (pencil edits) are rare and touch FlightSchedule, not
  // the large canBuy sheet, so they stay simple, immediate, per-row writes.
  payload.entries.forEach(function (entry) {
    if (entry.scheduleEdited) {
      schedSheet.getRange(entry.scheduleRow, FS_COL_DEP_TIME).setValue(entry.dep);
      schedSheet.getRange(entry.scheduleRow, FS_COL_AIRCRAFT_CONFIG).setValue(entry.aircraftConfig);
      schedSheet.getRange(entry.scheduleRow, FS_COL_FLIGHT_NUMBER).setValue(entry.flightNumber);
    }
  });

  var toWrite = payload.entries.filter(function (entry) {
    return ['y', 'cplus', 'onePS', 'd1'].some(function (f) {
      return entry[f] !== '' && entry[f] !== null && entry[f] !== undefined;
    });
  });
  if (toWrite.length === 0) {
    return { logged: 0 };
  }

  var easternNow = getEasternNow_();
  var checkYYYY = easternNow.checkYYYY;
  var checkMMDD = easternNow.checkMMDD;
  var checkHHMM = easternNow.rawHHMM;

  var startRow = findLastDataRow_(canBuySheet, cols) + 1;
  var n = toWrite.length;

  var col = {
    car: [], org: [], dest: [], checkYr: [], checkMMDD: [], checkHHMM: [],
    flightYr: [], flightMMDD: [], dep: [], aircraft: [],
    availMain: [], availCPlus: [], availOnePS: [], availD1: [],
    t12Est: [], t4Est: [], t1Est: []
  };

  toWrite.forEach(function (entry) {
    var y = (entry.y !== '' && entry.y !== null && entry.y !== undefined) ? Number(entry.y) : '';
    var cplus = (entry.cplus !== '' && entry.cplus !== null && entry.cplus !== undefined) ? Number(entry.cplus) : '';
    var onePS = (entry.onePS !== '' && entry.onePS !== null && entry.onePS !== undefined) ? Number(entry.onePS) : '';
    var d1 = (entry.d1 !== '' && entry.d1 !== null && entry.d1 !== undefined) ? Number(entry.d1) : '';

    var est = computeBucketEstimatesFromIndex_(readingsIndex, entry.car, entry.org, entry.dest,
        payload.flightYr, payload.flightMMDD, entry.dep, offsets);

    col.car.push([entry.car]);
    col.org.push([entry.org]);
    col.dest.push([entry.dest]);
    col.checkYr.push([checkYYYY]);
    col.checkMMDD.push([checkMMDD]);
    col.checkHHMM.push([checkHHMM]);
    col.flightYr.push([payload.flightYr]);
    col.flightMMDD.push([payload.flightMMDD]);
    col.dep.push([entry.dep]);
    col.aircraft.push([entry.aircraftConfig]);
    col.availMain.push([y]);
    col.availCPlus.push([cplus]);
    col.availOnePS.push([onePS]);
    col.availD1.push([d1]);
    col.t12Est.push([est.t12]);
    col.t4Est.push([est.t4]);
    col.t1Est.push([est.t1]);
  });

  Object.keys(col).forEach(function (field) {
    canBuySheet.getRange(startRow, cols[field], n, 1).setValues(col[field]);
  });

  // dow/hoursBeforeDep/totalSeats: live formulas, one row-formula per new
  // row, still batched into one setFormulas() call per column.
  var dowFormulas = [], hoursFormulas = [], totalFormulas = [];
  for (var i = 0; i < n; i++) {
    var r = startRow + i;
    dowFormulas.push([dowFormula_(r, cols)]);
    hoursFormulas.push([hoursBeforeDepFormula_(r, cols)]);
    totalFormulas.push([totalSeatsFormula_(r, cols)]);
  }
  canBuySheet.getRange(startRow, cols.dow, n, 1).setFormulas(dowFormulas);
  canBuySheet.getRange(startRow, cols.hoursBeforeDep, n, 1).setFormulas(hoursFormulas);
  canBuySheet.getRange(startRow, cols.totalSeats, n, 1).setFormulas(totalFormulas);

  // Leading-zero display, applied once across the whole new block rather
  // than per row.
  canBuySheet.getRange(startRow, cols.checkMMDD, n, 1).setNumberFormat('0000');
  canBuySheet.getRange(startRow, cols.checkHHMM, n, 1).setNumberFormat('0000');
  canBuySheet.getRange(startRow, cols.flightMMDD, n, 1).setNumberFormat('0000');
  canBuySheet.getRange(startRow, cols.dep, n, 1).setNumberFormat('0000');

  // Forces the dow/hoursBeforeDep/totalSeats formulas above to fully commit
  // and recalculate before this function returns. Without this, the
  // dialogue's immediate follow-up call to getNextBatch() (see
  // saveAndGetNextBatch below) can race ahead of Sheets finishing that
  // recalculation - reading hoursBeforeDep for the row you just logged as
  // still blank, which makes that flight look never-checked-today and get
  // offered right back to you.
  SpreadsheetApp.flush();

  return { logged: n };
}

/**
 * Combines a save with the immediate "what's next" refresh into a single
 * server round trip, instead of the dialogue firing saveEntryDialog() and
 * then getNextBatch() as two separate google.script.run calls back to
 * back. Each round trip carries its own network/HtmlService overhead, so
 * this halves that overhead on every Log click. Behavior is identical to
 * calling them one after another - this just does it server-side in one go.
 */
function saveAndGetNextBatch(payload, skipRouteKeys) {
  var saveResult = saveEntryDialog(payload);
  var nextResult = getNextBatch(skipRouteKeys);
  return { logged: saveResult.logged, next: nextResult };
}
