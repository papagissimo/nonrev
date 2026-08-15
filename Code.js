/**
 * Nonrev Tools
 *
 * Setup (one time):
 * 1. In this Google Sheet: Extensions > Apps Script
 * 2. Delete the placeholder "Code.gs" content, paste in this file's contents
 * 3. Add EntryDialog.gs, ScheduleEditDialog.gs, EntryDialog.html, and
 *    ScheduleEditDialog.html as their own files in the same project (HTML
 *    files are named exactly that, no ".html" typed into the file-name
 *    field - Apps Script adds the extension itself).
 * 4. Save (disk icon). Close the Apps Script tab, reload the spreadsheet.
 * 5. A "Nonrev Tools" menu will appear. Run any menu item once to trigger
 *    Google's one-time authorization prompt.
 * 6. Optional hotkeys: Extensions > Macros > Manage macros, find
 *    "filterToRoute", "clearRouteFilter", assign each a number 1-9
 *    (Ctrl+Alt+Shift+N).
 *
 * COLUMN MAPPING: canBuy has two header rows. Rather than hardcoding column
 * letters, this script reads both header rows once per run and matches on
 * that text, so reordering columns in canBuy does NOT require editing this
 * script - only the header text itself needs to stay recognizable (see
 * CANBUY_FIELDS below).
 *
 * ROW LAYOUT: canBuy reserves CANBUY_PANEL_ROWS rows at the top (currently
 * 0 - see the comment on that constant below), with the two header rows and
 * all data rows below that. Every formula generator and row-position check
 * in this script reads CANBUY_HEADER_ROW1/2 and CANBUY_DATA_START_ROW/
 * END_ROW below rather than hardcoding row numbers.
 */

// Always 0 now - the selection-panel feature (filter inputs, readout,
// history chart) that used to occupy the top rows was tried, had bugs, and
// was removed for good along with all its code (setupPanel,
// onSelectionChange, applyPanelFilters_, etc.) during a code cleanup pass -
// nothing left in this file references the panel anymore. Headers sit at
// row 1/2, data starts at row 3, matching the original layout.
var CANBUY_PANEL_ROWS = 0;
var CANBUY_HEADER_ROW1 = CANBUY_PANEL_ROWS + 1;
var CANBUY_HEADER_ROW2 = CANBUY_PANEL_ROWS + 2;
var CANBUY_DATA_START_ROW = CANBUY_PANEL_ROWS + 3;
// Formula-range ceiling used everywhere a "whole column" range gets built -
// raised from the old hardcoded 1000, which was already too low given
// 1,200+ existing rows of history even before the panel's 12-row shift.
var CANBUY_DATA_END_ROW = 5000;

// Sheet that archived (finalized) days get moved to - see
// archiveDateAndEarlier() below. Columns are matched to canBuy by header
// TEXT (not position), so the two sheets can have their columns in
// different orders and canBuy can grow new columns later without breaking
// the match - see buildGenericHeaderMap_/getOrCreateArchiveColumn_.
var ARCHIVE_SHEET_NAME = 'Archive';

// Each entry: logical field name -> [row1 text, row2 text-or-null].
// row2 is null for fields where row1 alone is unique (car/org/dest).
var CANBUY_FIELDS = {
  car:          ['car', null],
  org:          ['org', null],
  dest:         ['dest', null],
  checkYr:      ['check', 'yyyy'],
  checkMMDD:    ['check', 'mmdd'],
  checkHHMM:    ['check', 'hhmm'],
  flightYr:     ['flt', 'yyyy'],
  flightMMDD:   ['flt', 'mmdd'],
  dow:          ['flt', 'DoW'],
  dep:          ['dep', '24mm'],
  aircraft:     ['flt', 'airc'],
  hoursBeforeDep: ['T-', 'hrs'],
  comm:         ['comm', null],
  tag:          ['tag', null],
  next:         ['next', null],
  totalSeats:   ['ttl', 'avail'],
  t12Est:       ['t12', null],
  t4Est:        ['t4', null],
  t1Est:        ['t1', null],
  // Delta's own buckets (replacing the old Google Flights D1/1st/bus/prEco/b eco scheme):
  availD1:      ['d1', 'avail'],
  availMain:    ['y', 'avail'],
  availCPlus:   ['c+', 'avail'],
  availOnePS:   ['1/ps', 'avail']  // header row 2 is typo'd "aval" - normalized below
};

// Reads canBuy's row 1 + row 2 headers and returns { fieldName: colIndex (1-based) }.
// Throws a descriptive error if any expected header can't be found, rather
// than silently writing to the wrong column.
function getCanBuyColumnMap_(sheet) {
  var lastCol = sheet.getLastColumn();
  var row1 = sheet.getRange(CANBUY_HEADER_ROW1, 1, 1, lastCol).getValues()[0];
  var row2 = sheet.getRange(CANBUY_HEADER_ROW2, 1, 1, lastCol).getValues()[0];

  var map = {};
  var missing = [];

  Object.keys(CANBUY_FIELDS).forEach(function (field) {
    var wantRow1 = String(CANBUY_FIELDS[field][0]).toLowerCase();
    var wantRow2 = CANBUY_FIELDS[field][1];
    var found = -1;
    for (var c = 0; c < lastCol; c++) {
      var r1 = String(row1[c] || '').toLowerCase();
      if (r1 !== wantRow1) continue;
      if (wantRow2 === null) {
        found = c + 1;
        break;
      }
      var r2 = String(row2[c] || '').toLowerCase();
      if (r2 === 'aval') r2 = 'avail'; // tolerate the "aval" typo seen in the 1/PS column
      if (r2 === String(wantRow2).toLowerCase()) {
        found = c + 1;
        break;
      }
    }
    if (found === -1) {
      missing.push(field + ' (row1="' + CANBUY_FIELDS[field][0] + '"' +
          (wantRow2 !== null ? ', row2="' + wantRow2 + '"' : '') + ')');
    } else {
      map[field] = found;
    }
  });

  if (missing.length > 0) {
    throw new Error('Could not find these canBuy columns by header text: ' + missing.join('; ') +
        '. Check that row 1/row 2 header labels still match what the script expects.');
  }
  return map;
}

function onOpen() {
  SpreadsheetApp.getUi()
      .createMenu('Nonrev Tools')
      .addItem("What's Next (New Schema)", 'walkUpcomingFlightsNewSchema')
      .addItem('Filter to This Flight (Org/Dest)', 'filterToRoute')
      .addItem('Log CanBuy Seats', 'openEntryDialog')
      .addItem('Edit Flight Schedule', 'openScheduleEditDialog')
      .addItem('Clear Route Filter', 'clearRouteFilter')
      .addItem('Validate fltSched', 'validateFltSched')
      .addItem('Recompute All T-12/T-4/T-1', 'recomputeAllBucketEstimates')
      .addItem('Archive Date and Earlier', 'archiveDateAndEarlier')
      .addToUi();
}

// Filters canBuy down to just the origin/destination of whatever row the
// active cell is on - lets you land on the first not-yet-filled-in row for
// a route/day, run this, and see every flight for that route (any date
// filter you already have set stays in effect, since this only ever touches
// the org/dest column criteria - see clearRouteFilter below for the
// corresponding undo). Reuses an existing basic filter if one is already
// active on the sheet (so a pre-existing date filter/sort is left alone);
// creates one covering the whole sheet if none exists yet.
function filterToRoute() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName('canBuy');
  var ui = SpreadsheetApp.getUi();
  if (!sheet) { ui.alert('No sheet named "canBuy" found.'); return; }

  var cols;
  try {
    cols = getCanBuyColumnMap_(sheet);
  } catch (e) {
    ui.alert(e.message);
    return;
  }

  var activeCell = sheet.getActiveCell();
  var row = activeCell.getRow();
  if (row < CANBUY_DATA_START_ROW) {
    ui.alert('Click a data row (row ' + CANBUY_DATA_START_ROW + ' or below) first, then run this again.');
    return;
  }

  var orgVal = sheet.getRange(row, cols.org).getValue();
  var destVal = sheet.getRange(row, cols.dest).getValue();
  if (orgVal === '' || orgVal === null || destVal === '' || destVal === null) {
    ui.alert('The selected row has no org/dest filled in - click a row with a flight on it first.');
    return;
  }

  var filter = sheet.getFilter();
  if (!filter) {
    filter = sheet.getDataRange().createFilter();
  }
  filter.setColumnFilterCriteria(cols.org,
      SpreadsheetApp.newFilterCriteria().whenTextEqualTo(String(orgVal)).build());
  filter.setColumnFilterCriteria(cols.dest,
      SpreadsheetApp.newFilterCriteria().whenTextEqualTo(String(destVal)).build());
}

// Undoes exactly what filterToRoute did: clears filter criteria on the
// org/dest columns only, so canBuy goes back to showing every route. Any
// other filter criteria you had set on other columns (e.g. a date filter)
// before or after running filterToRoute is left completely alone.
function clearRouteFilter() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName('canBuy');
  var ui = SpreadsheetApp.getUi();
  if (!sheet) { ui.alert('No sheet named "canBuy" found.'); return; }

  var cols;
  try {
    cols = getCanBuyColumnMap_(sheet);
  } catch (e) {
    ui.alert(e.message);
    return;
  }

  var filter = sheet.getFilter();
  if (!filter) {
    ui.alert('No filter is currently active on canBuy.');
    return;
  }
  filter.removeColumnFilterCriteria(cols.org);
  filter.removeColumnFilterCriteria(cols.dest);
}

// Rounds an hhmm integer (e.g. 1447) up to the next multiple of 10 minutes,
// carrying into the hour (and wrapping past 2359 -> 0000) if needed.
function roundUpToNext10_(hhmm) {
  var h = Math.floor(hhmm / 100);
  var m = hhmm % 100;
  var totalMin = h * 60 + m;
  var rounded = Math.ceil(totalMin / 10) * 10;
  if (rounded >= 24 * 60) rounded -= 24 * 60;
  var rh = Math.floor(rounded / 60);
  var rm = rounded % 60;
  return rh * 100 + rm;
}

// Reads the Legend "Hrs Behind ET" table (dynamically, wherever it starts/ends)
// into a { AIRPORTCODE: offsetHours } map, so filtering/sorting logic can put
// every airport's local departure time on the same absolute timeline before
// comparing - the same correction the Hours-Before-Dep formula already does,
// just applied here in script logic too instead of only in that one formula.
function getAirportOffsets_() {
  var legend = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Legend');
  var offsets = {};
  if (!legend) return offsets;
  var data = legend.getRange('D1:E' + legend.getLastRow()).getValues();
  for (var r = 0; r < data.length; r++) {
    var code = data[r][0];
    var hrs = data[r][1];
    if (typeof code === 'string' && code.trim() !== '' && typeof hrs === 'number') {
      offsets[code.trim().toUpperCase()] = hrs;
    }
  }
  return offsets;
}

// Converts a local hhmm departure time at a given airport into "ET-equivalent
// minutes since midnight" - may exceed 1440 (meaning the next ET calendar
// day), which is fine since this value is only ever used for relative
// comparison/sorting, never displayed directly.
function toEtMinutes_(hhmm, airportCode, offsets) {
  var h = Math.floor(hhmm / 100);
  var m = hhmm % 100;
  var offsetHrs = offsets[String(airportCode).toUpperCase()] || 0;
  return h * 60 + m + offsetHrs * 60;
}

// Gets "now" explicitly in America/New_York, regardless of where this script
// physically runs from, and regardless of what timezone the spreadsheet
// itself happens to be set to. This is what makes the Legend "Hrs Behind ET"
// math correct whether you're on your couch in Ohio, visiting SLC, or
// checking this from Edinburgh, Honolulu, or Auckland - "now" is a universal
// fact, this just asks Apps Script to express it in Eastern terms explicitly
// instead of trusting whatever timezone happens to be ambient.
function getEasternNow_() {
  var d = new Date();
  var yyyy = parseInt(Utilities.formatDate(d, 'America/New_York', 'yyyy'), 10);
  var mmdd = parseInt(Utilities.formatDate(d, 'America/New_York', 'Mdd'), 10);
  var hhmm = parseInt(Utilities.formatDate(d, 'America/New_York', 'HHmm'), 10);
  return { checkYYYY: yyyy, checkMMDD: mmdd, rawHHMM: hhmm };
}

// Fuzzy departure-time matching tolerance (+/- minutes), used across this
// project wherever two logged rows need to be treated as "the same flight
// instance" despite small clock differences - real-world schedule wobble and
// the occasional fat-fingered digit are common and mostly harmless here,
// worth tolerating rather than requiring an exact match. Confirmed safe
// given the project's routes never run flights under ~90 minutes apart.
var FULL_MATCH_WINDOW_MINUTES_ = 30;

// Minutes since a fixed reference point (Unix epoch, UTC midnight of the
// given calendar date) - not meant to be a real-world-accurate instant, just
// a stable, monotonically increasing day marker so "today" and "some future
// date" can be compared on one continuous timeline instead of as two cases.
function dayEpochMinutes_(yyyy, month1based, day) {
  return Math.floor(Date.UTC(yyyy, month1based - 1, day) / 60000);
}

var DOW_ABBREV_ = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];

function dowAbbrev_(dateObj) {
  return DOW_ABBREV_[dateObj.getDay()];
}

// Reads every existing canBuy row's org/dest/flightYr/flightMMDD/dep/tag
// once, so a freshly-logged "What's Next" row can inherit whatever tag was
// on the most recent PRIOR instance of the same route+timeslot+day-of-week.
// Keyed the same way fuzzy flight-instance matching is done everywhere else
// in this project (org+dest+dep within +/-30 min) - deliberately NOT flight
// number, which this project doesn't track (see HANDOFF discussion on why
// that'd cost more daily friction than it's worth) - but this ALSO requires
// the SAME day-of-week, since a Tuesday-only tag ("watch this on Tuesday")
// should never bleed onto a Wednesday or Saturday reading of the same route
// in between.
function getTagHistory_(sheet, cols, offsets) {
  var maxRows = sheet.getMaxRows();
  var fields = ['org', 'dest', 'flightYr', 'flightMMDD', 'dep', 'tag'];
  var colData = {};
  fields.forEach(function (f) {
    colData[f] = sheet.getRange(1, cols[f], maxRows, 1).getValues();
  });

  var history = [];
  for (var r = 0; r < maxRows; r++) {
    var org = colData.org[r][0], dest = colData.dest[r][0], dep = colData.dep[r][0];
    var flightYr = colData.flightYr[r][0], flightMMDD = colData.flightMMDD[r][0];
    if (org === '' || org === null || dest === '' || dest === null || dep === '' || dep === null) continue;
    if (typeof flightYr !== 'number' || typeof flightMMDD !== 'number') continue;

    var month = Math.floor(flightMMDD / 100), day = flightMMDD % 100;
    var dateObj = new Date(flightYr, month - 1, day);

    history.push({
      org: String(org).toLowerCase(),
      dest: String(dest).toLowerCase(),
      dayEpoch: dayEpochMinutes_(flightYr, month, day),
      dow: dowAbbrev_(dateObj),
      depEtMinutes: toEtMinutes_(dep, org, offsets),
      tag: colData.tag[r][0]
    });
  }
  return history;
}

// Finds the most recent (by flight date) prior entry matching org/dest,
// departure within +/-30 min, and the SAME day-of-week as the new row being
// created - then returns whatever tag that entry had, including blank if
// you'd already cleared it (that's the intended "off switch": clear the tag
// once, and it stops propagating from then on). Returns '' if nothing
// qualifies yet, so a genuinely new route/timeslot just starts blank rather
// than erroring.
function findMostRecentTag_(history, org, dest, depEtMinutes, dow, beforeDayEpoch) {
  var best = null;
  for (var i = 0; i < history.length; i++) {
    var h = history[i];
    if (h.org !== org || h.dest !== dest || h.dow !== dow) continue;
    if (Math.abs(h.depEtMinutes - depEtMinutes) > FULL_MATCH_WINDOW_MINUTES_) continue;
    if (h.dayEpoch >= beforeDayEpoch) continue;
    if (best === null || h.dayEpoch > best.dayEpoch) best = h;
  }
  return best ? (best.tag || '') : '';
}

// A row counts as "real" if it has Origin, Destination, and Departure time
// all filled in - that's the minimum that only exists once a specific flight
// has actually been committed to. Check Yr/Flight Yr/etc. are deliberately
// NOT part of this test: those get bulk-filled far in advance across many
// rows (a year rarely changes), so they'd make terrible sentinels - a
// half-filled row is the user's business to finish or discard, not this
// script's to flag.
function findLastDataRow_(sheet, cols) {
  // getLastRow() (last row with ANY content) instead of getMaxRows() (the
  // sheet's full provisioned grid, which can run far past your actual data
  // and made every one of these scans slower than it needed to be as
  // canBuy grew).
  var lastRow = sheet.getLastRow();
  var orgCol = sheet.getRange(1, cols.org, lastRow, 1).getValues();
  var destCol = sheet.getRange(1, cols.dest, lastRow, 1).getValues();
  var depCol = sheet.getRange(1, cols.dep, lastRow, 1).getValues();
  for (var r = lastRow - 1; r >= 0; r--) {
    var hasOrg = orgCol[r][0] !== '' && orgCol[r][0] !== null;
    var hasDest = destCol[r][0] !== '' && destCol[r][0] !== null;
    var hasDep = depCol[r][0] !== '' && depCol[r][0] !== null;
    if (hasOrg && hasDest && hasDep) {
      return r + 1;
    }
  }
  return CANBUY_DATA_START_ROW - 1; // only header rows exist - caller adds 1
}

// Builds the DoW and Hours-Before-Dep formula strings using whatever columns
// the header map says to use, so they stay correct even after a reorder.
function dowFormula_(r, cols) {
  var g = columnLetter_(cols.flightYr), h = columnLetter_(cols.flightMMDD);
  return '=IF(OR(' + g + r + '="",' + h + r + '=""),"",' +
      'TEXT(DATE(' + g + r + ',INT(' + h + r + '/100),MOD(' + h + r + ',100)),"ddd"))';
}

// Total-seats formula: plain sum of the four granular availability columns
// (Main/C+/1st-PS/D1). Used to be a longer IF/ISNUMBER chain that (a) fell
// back to a "number of nines" shorthand column, and (b) deliberately blanked
// the whole total if a cell held stray non-numeric text like an "nfs?" note
// - both retired now that the numNines column is gone and NFS-style notes
// no longer live in these cells (they either go in comm, or - now that the
// hard T-45min cutoff makes that ambiguity moot - just aren't written at
// all). SUM() treats a blank cell as 0 and silently skips non-numeric text
// rather than erroring on it, same as the old formula's blank-cell handling
// but without the old error-flagging behavior for stray text - fine now
// that stray text shouldn't occur in these columns in the first place.
function totalSeatsFormula_(r, cols) {
  var o = columnLetter_(cols.availMain), p = columnLetter_(cols.availCPlus),
      q = columnLetter_(cols.availOnePS), rr = columnLetter_(cols.availD1);
  return '=SUM(' + o + r + ',' + p + r + ',' + q + r + ',' + rr + r + ')';
}

// Shared engine behind the t12/t4/t1 columns - same underlying algorithm for
// all three, just a different target hour. Computed here in plain JS (once,
// in script) rather than as a live spreadsheet ARRAYFORMULA - the formula
// version worked but was ruinously slow: with 1,200+ rows, a live per-row
// array formula re-scanning thousands of rows recalculates on EVERY edit
// anywhere in the sheet, which compounds badly at this row count. This
// version computes a plain number once, written via setValue - see
// writeBucketEstimatesForRow_ (single row, used when a row is first logged)
// and recomputeAllBucketEstimates (whole-sheet batch refresh, menu item).
//
// readings: array of {hrs, ttl, ceiling} for ONE flight instance.
//   1. A reading counts as "ceiling" (not precise) if any of y/c+/1-PS shows
//      exactly 9 - e.g. 9,0,2 counts as ceiling, same as a bare 9, since the
//      true total could be understating an unknown amount either way.
//   2. If 2+ PRECISE readings exist, ceiling readings are discarded entirely
//      for this estimate. Otherwise every reading (ceiling included) stays
//      eligible, per the standing principle that this sheet should never
//      refuse to produce an estimate.
//   3. If at least one eligible reading sits on each side of the target
//      hour, interpolate between the nearest on each side - a straddle
//      counts as "done" regardless of gap size, no minimum required.
//   4. If every eligible reading is on the SAME side, extrapolate using the
//      nearest-to-target and farthest-from-target readings on that side, for
//      the widest (most stable) slope. Judgment call, not something
//      explicitly settled on - worth revisiting once there's a few weeks of
//      real data to eyeball.
//   5. Only one eligible reading at all -> returned flat, a low-confidence
//      bound rather than a real interpolation/extrapolation.
//   6. No eligible reading at all -> '' (blank) - the one legitimate
//      "no estimate" case, since there's genuinely zero data.
//
// NOT yet implemented: the T-1-specific nuance about preferring to skip a
// T-45-60min reading unless it upgrades an extrapolation into an
// interpolation or is the only post-T-4 data available - every eligible
// reading is treated the same regardless of proximity to departure, which
// leans the same direction as the "be fairly liberal about using them"
// observation already on record.
function computeBucketEstimate_(readings, targetHours) {
  var precise = readings.filter(function (rd) { return !rd.ceiling; });
  var eligible = precise.length >= 2 ? precise : readings;

  var before = eligible.filter(function (rd) { return rd.hrs >= targetHours; });
  var after = eligible.filter(function (rd) { return rd.hrs <= targetHours; });

  function nearestOf(list, wantMin) {
    return list.reduce(function (best, cur) {
      if (!best) return cur;
      if (wantMin) return cur.hrs < best.hrs ? cur : best;
      return cur.hrs > best.hrs ? cur : best;
    }, null);
  }

  if (before.length >= 1 && after.length >= 1) {
    var b = nearestOf(before, true);   // smallest hrs among before-side (nearest to target from above)
    var a = nearestOf(after, false);   // largest hrs among after-side (nearest to target from below)
    if (b.hrs === a.hrs) return b.ttl;
    return b.ttl + (a.ttl - b.ttl) * (b.hrs - targetHours) / (b.hrs - a.hrs);
  }
  if (before.length >= 1) {
    var b2 = nearestOf(before, true);
    if (before.length >= 2) {
      var far = nearestOf(before, false); // largest hrs among before-side (farthest from target)
      if (far.hrs === b2.hrs) return b2.ttl;
      var slope = (far.ttl - b2.ttl) / (far.hrs - b2.hrs);
      return b2.ttl + slope * (targetHours - b2.hrs);
    }
    return b2.ttl;
  }
  if (after.length >= 1) {
    var a2 = nearestOf(after, false);
    if (after.length >= 2) {
      var far2 = nearestOf(after, true); // smallest hrs among after-side (farthest from target)
      if (far2.hrs === a2.hrs) return a2.ttl;
      var slope2 = (far2.ttl - a2.ttl) / (far2.hrs - a2.hrs);
      return a2.ttl + slope2 * (targetHours - a2.hrs);
    }
    return a2.ttl;
  }
  return '';
}

// Target is T-61min, expressed as an exact fraction of an hour (61/60) - per
// the "golden ticket sits at ~T-61min, just outside the last unmodeled
// ~15-minute scramble" discussion, rather than the rounder T-60min/T-1hr.
var T1_TARGET_HOURS_ = 61 / 60;

function hoursBeforeDepFormula_(r, cols) {
  var g = columnLetter_(cols.flightYr), h = columnLetter_(cols.flightMMDD);
  var j = columnLetter_(cols.dep), b = columnLetter_(cols.org);
  var d = columnLetter_(cols.checkYr), e = columnLetter_(cols.checkMMDD), f = columnLetter_(cols.checkHHMM);
  return '=IF(OR(' + g + r + '="",' + h + r + '="",' + j + r + '="",' +
      d + r + '="",' + e + r + '="",' + f + r + '=""),"",' +
      'ROUND(((DATE(' + g + r + ',INT(' + h + r + '/100),MOD(' + h + r + ',100))+' +
      '(INT(' + j + r + '/100)+MOD(' + j + r + ',100)/60)/24+' +
      'IFERROR(VLOOKUP(' + b + r + ',Legend!$D$41:$E$55,2,FALSE()),0)/24)-' +
      '(DATE(' + d + r + ',INT(' + e + r + '/100),MOD(' + e + r + ',100))+' +
      '(INT(' + f + r + '/100)+MOD(' + f + r + ',100)/60)/24))*24,1))';
}

// Forces a cell to display as a 4-digit leading-zero value (e.g. mmdd or
// hhmm fields showing "0802" instead of "802"), regardless of whatever
// format the destination cell already happened to have. setValue() never
// touches a cell's number format on its own - relying on a pre-formatted
// template row being there underneath is fragile, so every place this
// script writes an mmdd/hhmm-style field now sets this explicitly instead.
function setLeadingZeroFormat_(sheet, row, col) {
  sheet.getRange(row, col).setNumberFormat('0000');
}

function columnLetter_(colIndex) {
  var letter = '';
  while (colIndex > 0) {
    var rem = (colIndex - 1) % 26;
    letter = String.fromCharCode(65 + rem) + letter;
    colIndex = Math.floor((colIndex - 1) / 26);
  }
  return letter;
}

// Parses the flight-date text from a date prompt. Accepts:
//   ''         -> today (Eastern "now")
//   'mmdd'     (4 digits, leading zero required, e.g. '0802') -> current year
//   'yyyymmdd' (8 digits, e.g. '20270105') -> explicit year, for year-boundary
//              trips where "current year" would be ambiguous
// Returns {flightYYYY, flightMMDD} or throws an Error with a user-facing message.
function parseFlightDateText_(dateText, checkYYYY, checkMMDD) {
  dateText = String(dateText).trim();
  if (dateText === '') {
    return { flightYYYY: checkYYYY, flightMMDD: checkMMDD };
  }
  if (/^\d{4}$/.test(dateText)) {
    return { flightYYYY: checkYYYY, flightMMDD: parseInt(dateText, 10) };
  }
  if (/^\d{8}$/.test(dateText)) {
    return {
      flightYYYY: parseInt(dateText.substring(0, 4), 10),
      flightMMDD: parseInt(dateText.substring(4), 10)
    };
  }
  throw new Error('Could not parse "' + dateText + '" - expected mmdd (e.g. 0802) ' +
      'or yyyymmdd (e.g. 20270105), or leave blank for today.');
}

// Scans the entire fltSched tab for two kinds of ambiguity that "What's
// Next" can't safely resolve on its own: (1) more than one default
// (blank-DoW) row for the same carrier/org/dest, and (2) two or more
// day-specific rows for the same carrier/org/dest whose day-lists overlap
// on at least one weekday. Reports every problem found in one pass rather
// than stopping at the first.
function validateFltSched() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var schedSheet = ss.getSheetByName('fltSched');
  if (!schedSheet) {
    SpreadsheetApp.getUi().alert('No sheet named "fltSched" found.');
    return;
  }

  var data = schedSheet.getDataRange().getValues();
  var groups = {};

  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    if (!row[0]) continue;
    var car = String(row[0]).toLowerCase();
    var org = String(row[1]).toLowerCase();
    var dest = String(row[2]).toLowerCase();
    var key = car + '|' + org + '|' + dest;
    if (!groups[key]) groups[key] = { blankRows: [], specificRows: [] };

    var dowField = row[3];
    var sheetRow = i + 1;
    if (!dowField || String(dowField).trim() === '') {
      groups[key].blankRows.push(sheetRow);
    } else {
      var days = String(dowField).toLowerCase().split(',').map(function (s) {
        return s.trim().substring(0, 3);
      });
      groups[key].specificRows.push({ sheetRow: sheetRow, days: days });
    }
  }

  var problems = [];
  Object.keys(groups).forEach(function (key) {
    var g = groups[key];
    var label = key.split('|').join(' ').toUpperCase();

    if (g.blankRows.length > 1) {
      problems.push(label + ': ' + g.blankRows.length + ' default (blank-DoW) rows (sheet rows ' +
          g.blankRows.join(', ') + ') - should be exactly one.');
    }

    for (var a = 0; a < g.specificRows.length; a++) {
      for (var b = a + 1; b < g.specificRows.length; b++) {
        var overlap = g.specificRows[a].days.filter(function (d) {
          return g.specificRows[b].days.indexOf(d) !== -1;
        });
        if (overlap.length > 0) {
          problems.push(label + ': sheet rows ' + g.specificRows[a].sheetRow + ' and ' +
              g.specificRows[b].sheetRow + ' both claim ' + overlap.join(', ').toUpperCase() + '.');
        }
      }
    }
  });

  var ui = SpreadsheetApp.getUi();
  if (problems.length === 0) {
    ui.alert('fltSched looks clean - no duplicate defaults or overlapping day-specific rows found.');
  } else {
    ui.alert('fltSched has ' + problems.length + ' problem(s):\n\n' + problems.join('\n'));
  }
}

// ============================================================================
// NEW-SCHEMA VERSION of What's Next - reads the normalized FlightSchedule
// sheet (flightNumber | org | dest | dayOfWeek | depTime | aircraftConfig,
// one row per real flight instance, single header row) instead of the old
// wide fltSched.
//
// Because the new schema has exactly one row per (org, dest, dayOfWeek,
// depTime) by construction, there's no blank-default-vs-exception grouping
// to do here at all - that whole class of ambiguity (and the bug it caused)
// structurally can't occur in this shape. The one thing still worth
// guarding against is a genuine data-entry accident - two rows that
// shouldn't both exist for the same (org,dest,dayOfWeek,depTime). That's
// checked once per run, below, and any such duplicates are reported and
// excluded rather than silently double-logged.
//
// flightNumber is read but not written anywhere - canBuy has no flight
// number column (a deliberate standing decision, see HANDOFF), and
// AircraftConfigs isn't read here either since canBuy's aircraft column
// just stores the same free-text aircraftConfig key FlightSchedule already
// has (matching what fltSched's aircraft column stored before) - no seat
// capacity math is wired up yet to need the lookup itself.
function walkUpcomingFlightsNewSchema() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var schedSheet = ss.getSheetByName('FlightSchedule');
  var canBuySheet = ss.getSheetByName('canBuy');
  var ui = SpreadsheetApp.getUi();
  if (!schedSheet) { ui.alert('No sheet named "FlightSchedule" found.'); return; }
  if (!canBuySheet) { ui.alert('No sheet named "canBuy" found.'); return; }

  var cols;
  try {
    cols = getCanBuyColumnMap_(canBuySheet);
  } catch (e) {
    ui.alert(e.message);
    return;
  }

  var easternNow = getEasternNow_();
  var checkYYYY = easternNow.checkYYYY;
  var checkMMDD = easternNow.checkMMDD;
  var rawHHMM = easternNow.rawHHMM;
  var checkHHMM = roundUpToNext10_(rawHHMM);

  var dateResp = ui.prompt('Which flight date? (new schema)',
      'Enter mmdd (e.g. 0802) for this year, yyyymmdd (e.g. 20270105) for a ' +
      'different year, or leave blank for today.',
      ui.ButtonSet.OK_CANCEL);
  if (dateResp.getSelectedButton() === ui.Button.CANCEL) return;

  var flightYYYY, flightMMDD;
  try {
    var parsed = parseFlightDateText_(dateResp.getResponseText(), checkYYYY, checkMMDD);
    flightYYYY = parsed.flightYYYY;
    flightMMDD = parsed.flightMMDD;
  } catch (e) {
    ui.alert(e.message);
    return;
  }
  var flightMonth = Math.floor(flightMMDD / 100);
  var flightDay = flightMMDD % 100;
  var dow = dowAbbrev_(new Date(flightYYYY, flightMonth - 1, flightDay));

  var schedData = schedSheet.getDataRange().getValues();
  // Column order matches the FlightSchedule tab as built: carrier,
  // flightNumber, org, dest, dayOfWeek, depTime, aircraftConfig - single
  // header row (row 1).
  var seen = {}; // (org|dest|dow|dep) -> sheet row of first occurrence, for duplicate detection
  var duplicates = [];
  var rows = [];
  for (var i = 1; i < schedData.length; i++) {
    var row = schedData[i];
    var car = row[0], org = row[2], dest = row[3], rowDow = row[4], dep = row[5], airc = row[6];
    if (!org || !dest || !rowDow || dep === '' || dep === null || dep === undefined) continue;
    car = String(car || 'dl').toLowerCase();
    org = String(org).toLowerCase();
    dest = String(dest).toLowerCase();
    var rowDowAbbrev = String(rowDow).toLowerCase().substring(0, 3);
    var sheetRow = i + 1;
    var key = org + '|' + dest + '|' + rowDowAbbrev + '|' + dep;
    if (seen[key] !== undefined) {
      duplicates.push('Rows ' + seen[key] + ' and ' + sheetRow + ' both have ' +
          org.toUpperCase() + '-' + dest.toUpperCase() + ' ' + rowDowAbbrev.toUpperCase() + ' dep ' + dep);
      continue; // exclude the duplicate from candidates - don't guess which is right
    }
    seen[key] = sheetRow;
    if (rowDowAbbrev !== dow) continue;
    rows.push({ car: car, org: org, dest: dest, dep: dep, airc: (airc || 'TBD') });
  }

  if (duplicates.length > 0) {
    ui.alert('FlightSchedule has ' + duplicates.length + ' duplicate row(s) - excluded from this run:\n\n' +
        duplicates.join('\n') + '\n\nFix these in FlightSchedule before relying on this list being complete.');
  }

  var offsets = getAirportOffsets_();
  var readingsIndex = buildReadingsIndex_(canBuySheet, cols);
  var checkMonth = Math.floor(checkMMDD / 100);
  var checkDay = checkMMDD % 100;
  var checkAbsMinutes = dayEpochMinutes_(checkYYYY, checkMonth, checkDay) +
      Math.floor(checkHHMM / 100) * 60 + (checkHHMM % 100);
  var flightDayEpoch = dayEpochMinutes_(flightYYYY, flightMonth, flightDay);

  var candidates = [];
  rows.forEach(function (row) {
    var depEtMinutes = toEtMinutes_(row.dep, row.org, offsets);
    var depAbsMinutes = flightDayEpoch + depEtMinutes;
    // 45-minute hard cutoff - Delta stops showing/offering a flight this
    // close to departure. Naturally excludes nothing extra for a future
    // date, since a future day's absolute minutes are already thousands
    // ahead of "now" - no today-vs-future branch needed.
    if (depAbsMinutes < checkAbsMinutes + 45) return;
    candidates.push({ car: row.car, org: row.org, dest: row.dest, dep: row.dep, airc: row.airc, depEtMinutes: depEtMinutes });
  });

  if (candidates.length === 0) {
    ui.alert('No scheduled flights found for ' + flightYYYY + '-' + flightMMDD + ' in FlightSchedule.');
    return;
  }

  // Sort by true chronological (ET-equivalent) order, not raw local clock digits -
  // otherwise a SLC (Mountain) flight and a LAX (Pacific) flight showing the same
  // clock-looking dep time would sort as simultaneous when they're really an hour apart.
  candidates.sort(function (a, b) { return a.depEtMinutes - b.depEtMinutes; });

  var autoLogResp = ui.alert('Auto-log? (new schema)',
      'Log every remaining matching flight automatically, without asking one at a time?',
      ui.ButtonSet.YES_NO);
  var autoLogAll = (autoLogResp === ui.Button.YES);

  var tagHistory = getTagHistory_(canBuySheet, cols, offsets);
  var nextWriteRow = findLastDataRow_(canBuySheet, cols) + 1;

  var logged = 0, skipped = 0;
  for (var c = 0; c < candidates.length; c++) {
    var f = candidates[c];

    if (!autoLogAll) {
      var depStr = ('0000' + f.dep).slice(-4);
      depStr = depStr.substring(0, 2) + ':' + depStr.substring(2);
      var msg = f.car.toUpperCase() + '  ' + f.org.toUpperCase() + '-' + f.dest.toUpperCase() +
          '  dep ' + depStr + '  (' + f.airc + ')\n\nLog this flight?';
      var resp = ui.alert('Next flight [new schema] (' + (c + 1) + ' of ' + candidates.length + ')', msg, ui.ButtonSet.YES_NO_CANCEL);

      if (resp === ui.Button.CANCEL) break;
      if (resp === ui.Button.NO) { skipped++; continue; }
    }

    var r = nextWriteRow;
    nextWriteRow++;
    canBuySheet.getRange(r, cols.car).setValue(f.car);
    canBuySheet.getRange(r, cols.org).setValue(f.org);
    canBuySheet.getRange(r, cols.dest).setValue(f.dest);
    canBuySheet.getRange(r, cols.checkYr).setValue(checkYYYY);
    canBuySheet.getRange(r, cols.checkMMDD).setValue(checkMMDD);
    setLeadingZeroFormat_(canBuySheet, r, cols.checkMMDD);
    // checkHHMM deliberately left blank - a batch logged at once would
    // otherwise all get stamped with whatever time the macro happened to
    // run, even for rows checked later or in a different session. Format
    // is still set now so whatever's typed in later displays correctly.
    setLeadingZeroFormat_(canBuySheet, r, cols.checkHHMM);
    canBuySheet.getRange(r, cols.flightYr).setValue(flightYYYY);
    canBuySheet.getRange(r, cols.flightMMDD).setValue(flightMMDD);
    setLeadingZeroFormat_(canBuySheet, r, cols.flightMMDD);
    canBuySheet.getRange(r, cols.dow).setFormula(dowFormula_(r, cols));
    canBuySheet.getRange(r, cols.dep).setValue(f.dep);
    setLeadingZeroFormat_(canBuySheet, r, cols.dep);
    canBuySheet.getRange(r, cols.aircraft).setValue(f.airc);
    canBuySheet.getRange(r, cols.hoursBeforeDep).setFormula(hoursBeforeDepFormula_(r, cols));
    canBuySheet.getRange(r, cols.totalSeats).setFormula(totalSeatsFormula_(r, cols));
    writeBucketEstimatesForRow_(canBuySheet, cols, r, offsets, readingsIndex);

    var priorTag = findMostRecentTag_(tagHistory, f.org, f.dest, f.depEtMinutes, dow, flightDayEpoch);
    if (priorTag) canBuySheet.getRange(r, cols.tag).setValue(priorTag);

    logged++;
  }

  ui.alert('Done [new schema]: logged ' + logged + ', skipped ' + skipped + '.');
}

// Groups canBuy readings into flight-instances (same fuzzy-match tolerance
// as the rest of this project: same car/org/dest/flightYr/flightMMDD, dep
// times clustered within 30 min of each other), then for each instance
// with real readings on both sides, finds the first transition from
// "not full" (ttl > 2) to "full" (ttl <= 2) as the day's checks progress
// toward departure, and reports the interpolated midpoint (in hours before
// departure) as a single point estimate - no uncertainty band, deliberately
// left unresolved for now rather than fabricating one. Flights already full
// on their very first reading have no bracket to interpolate and are
// reported separately instead of guessing.
function buildGenericHeaderMap_(sheet) {
  var lastCol = sheet.getLastColumn();
  var map = {};
  if (lastCol < 1) return map;
  var row1 = sheet.getRange(CANBUY_HEADER_ROW1, 1, 1, lastCol).getValues()[0];
  var row2 = sheet.getRange(CANBUY_HEADER_ROW2, 1, 1, lastCol).getValues()[0];
  for (var c = 0; c < lastCol; c++) {
    var r1raw = row1[c], r2raw = row2[c];
    var keyPart1 = String(r1raw || '').trim().toLowerCase();
    var keyPart2 = String(r2raw || '').trim().toLowerCase();
    var key = keyPart1 + '||' + keyPart2;
    if (key === '||') key = '__col_' + (c + 1) + '__';
    map[key] = { col: c + 1, row1: r1raw, row2: r2raw };
  }
  return map;
}

// Looks up (or creates) the Archive column matching a given header key.
// If Archive doesn't have this header yet - either it's a brand new sheet,
// or canBuy grew a column since Archive was last touched - a new column is
// appended to Archive's right edge with the same header text, widening the
// sheet first if needed. archiveMap is updated in place so repeated calls
// within the same archive run see columns already created this run.
function getOrCreateArchiveColumn_(archiveSheet, archiveMap, key, row1Raw, row2Raw) {
  if (archiveMap[key]) return archiveMap[key].col;
  var newCol = archiveSheet.getLastColumn() + 1;
  if (archiveSheet.getMaxColumns() < newCol) {
    archiveSheet.insertColumnsAfter(Math.max(archiveSheet.getMaxColumns(), 1), 1);
  }
  archiveSheet.getRange(CANBUY_HEADER_ROW1, newCol).setValue(row1Raw);
  archiveSheet.getRange(CANBUY_HEADER_ROW2, newCol).setValue(row2Raw);
  archiveMap[key] = { col: newCol, row1: row1Raw, row2: row2Raw };
  return newCol;
}

// Moves every canBuy row whose FLIGHT date is on or before a date you type,
// out to the Archive sheet - a deliberate, manual "I'm done reviewing this
// day" action, never automatic and never triggered by a schedule or an
// onEdit. Keeps canBuy small (fast filtering, fast Recompute) while keeping
// every logged reading permanently - nothing is deleted, only relocated.
//
// The Archive sheet is created on first use. Columns are matched by HEADER
// TEXT, not position (see buildGenericHeaderMap_/getOrCreateArchiveColumn_
// above) - so canBuy and Archive can have their columns in different
// orders, and if canBuy grows a new column later, the next archive run just
// adds a matching column to Archive automatically rather than misaligning
// or erroring. (Renaming a header, rather than reordering, is NOT the same
// thing to this matching - a renamed column reads as a brand new one, so
// its older archived history stays under the old header name alongside a
// fresh column under the new name, rather than being merged.)
//
// Values are copied, not live formulas - once a day is archived it's meant
// to be "in the books" (per our discussion: no more recomputing, whatever
// was captured is captured), so there's no ongoing dependency on formula
// references that would otherwise need adjusting after the move. Each
// destination cell's number format is copied from its source cell
// individually, so the leading-zero mmdd/hhmm display carries over without
// this script needing to know which fields specifically require it.
// Originals are deleted from canBuy in contiguous chunks afterward (see the
// run-grouping inside the function) rather than one row at a time - row-by-
// row deletion forces a full sheet reflow per call and gets brutally slow
// once more than a few dozen rows are involved.
function archiveDateAndEarlier() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var canBuySheet = ss.getSheetByName('canBuy');
  var ui = SpreadsheetApp.getUi();
  if (!canBuySheet) { ui.alert('No sheet named "canBuy" found.'); return; }

  var cols;
  try {
    cols = getCanBuyColumnMap_(canBuySheet);
  } catch (e) {
    ui.alert(e.message);
    return;
  }

  var dateResp = ui.prompt('Archive which flight date (and everything earlier)?',
      'Enter mmdd (e.g. 0805) for this year, or yyyymmdd (e.g. 20270105) for a different ' +
      'year. Every canBuy row whose flight date is on or before this date moves to the ' +
      'Archive sheet. A date is required - there\'s no "today" default here.',
      ui.ButtonSet.OK_CANCEL);
  if (dateResp.getSelectedButton() === ui.Button.CANCEL) return;

  var currentYear = getEasternNow_().checkYYYY;
  var flightYYYY, flightMMDD;
  try {
    // Passing null for checkMMDD makes parseFlightDateText_ reject a blank
    // entry instead of silently defaulting to "today" - see its comment.
    var parsed = parseFlightDateText_(dateResp.getResponseText(), currentYear, null);
    flightYYYY = parsed.flightYYYY;
    flightMMDD = parsed.flightMMDD;
  } catch (e) {
    ui.alert(e.message);
    return;
  }
  var targetDayEpoch = dayEpochMinutes_(flightYYYY, Math.floor(flightMMDD / 100), flightMMDD % 100);
  var targetLabel = flightYYYY + '-' + ('0000' + flightMMDD).slice(-4);

  var maxRows = canBuySheet.getMaxRows();
  var orgCol = canBuySheet.getRange(1, cols.org, maxRows, 1).getValues();
  var flightYrCol = canBuySheet.getRange(1, cols.flightYr, maxRows, 1).getValues();
  var flightMMDDCol = canBuySheet.getRange(1, cols.flightMMDD, maxRows, 1).getValues();

  var matchedRows = [];
  for (var r = CANBUY_DATA_START_ROW; r <= maxRows; r++) {
    var i = r - 1;
    var org = orgCol[i][0];
    var fYr = flightYrCol[i][0], fMD = flightMMDDCol[i][0];
    if (org === '' || org === null) continue; // blank/template row
    if (typeof fYr !== 'number' || typeof fMD !== 'number') continue;
    var rowEpoch = dayEpochMinutes_(fYr, Math.floor(fMD / 100), fMD % 100);
    if (rowEpoch <= targetDayEpoch) matchedRows.push(r);
  }

  if (matchedRows.length === 0) {
    ui.alert('No canBuy rows found with a flight date on or before ' + targetLabel + '.');
    return;
  }

  var archiveSheet = ss.getSheetByName(ARCHIVE_SHEET_NAME);
  if (!archiveSheet) archiveSheet = ss.insertSheet(ARCHIVE_SHEET_NAME);
  archiveSheet.setFrozenRows(2); // header rows sit at CANBUY_HEADER_ROW1/2 = 1/2, same as canBuy

  var canBuyHeaderMap = buildGenericHeaderMap_(canBuySheet);
  var archiveHeaderMap = buildGenericHeaderMap_(archiveSheet);

  var archiveWriteRow = Math.max(archiveSheet.getLastRow() + 1, 3); // never write into/over the 2 header rows
  var neededRows = archiveWriteRow + matchedRows.length - 1;
  if (archiveSheet.getMaxRows() < neededRows) {
    archiveSheet.insertRowsAfter(archiveSheet.getMaxRows(), neededRows - archiveSheet.getMaxRows());
  }

  // One pass per canBuy column (not per row) - reads that whole column once,
  // finds/creates its matching Archive column by header text, then batch-
  // writes just the matched rows' values into a single contiguous block
  // starting at archiveWriteRow. Number format is copied from the first
  // matched row's source cell and applied across the whole written block -
  // fine here since a column's format is normally uniform down its length.
  Object.keys(canBuyHeaderMap).forEach(function (key) {
    var entry = canBuyHeaderMap[key];
    var destCol = getOrCreateArchiveColumn_(archiveSheet, archiveHeaderMap, key, entry.row1, entry.row2);

    var srcColValues = canBuySheet.getRange(1, entry.col, maxRows, 1).getValues();
    var outValues = matchedRows.map(function (r) { return [srcColValues[r - 1][0]]; });

    var destRange = archiveSheet.getRange(archiveWriteRow, destCol, outValues.length, 1);
    destRange.setValues(outValues);
    destRange.setNumberFormat(canBuySheet.getRange(matchedRows[0], entry.col).getNumberFormat());
  });

  // Delete in contiguous chunks, not one row at a time - deleteRow() forces
  // a full-sheet reflow on EVERY call, so removing a large batch row-by-row
  // is effectively O(n^2) and can blow well past Apps Script's execution
  // time limit. matchedRows is already ascending, so adjacent runs (e.g.
  // rows 40-85 all archived together) collapse into a single deleteRows()
  // call instead of 46 separate ones. Runs are deleted in descending order
  // so row numbers in not-yet-deleted runs stay valid as we go.
  var runs = [];
  var runStart = matchedRows[0], runLen = 1;
  for (var idx = 1; idx < matchedRows.length; idx++) {
    if (matchedRows[idx] === matchedRows[idx - 1] + 1) {
      runLen++;
    } else {
      runs.push([runStart, runLen]);
      runStart = matchedRows[idx];
      runLen = 1;
    }
  }
  runs.push([runStart, runLen]);

  for (var ri = runs.length - 1; ri >= 0; ri--) {
    canBuySheet.deleteRows(runs[ri][0], runs[ri][1]);
  }

  var remaining = findLastDataRow_(canBuySheet, cols) - CANBUY_DATA_START_ROW + 1;
  ui.alert('Archived ' + matchedRows.length + ' row(s) with flight date on or before ' + targetLabel +
      ' to the "' + ARCHIVE_SHEET_NAME + '" sheet. canBuy now has ' + Math.max(remaining, 0) + ' data row(s) left.');
}

// Reads canBuy ONCE into an in-memory index keyed by exact
// car|org|dest|flightYr|flightMMDD, so a batch of new rows (e.g. 30-40
// flights logged in one "What's Next" run) can each look up their sibling
// readings in memory instead of triggering a fresh full-column sheet scan
// per row - that repeated re-scanning was the remaining slowness after
// switching t12/t4/t1 off live formulas. Dep-time fuzzy-matching (+/-30min,
// same tolerance used everywhere else) happens per-lookup against the small
// per-key list, not against the whole sheet, so it stays cheap.
function buildReadingsIndex_(sheet, cols) {
  // See findLastDataRow_ - same getLastRow() vs. getMaxRows() fix, same reason.
  var lastRow = sheet.getLastRow();
  var fields = ['car', 'org', 'dest', 'flightYr', 'flightMMDD', 'dep', 'hoursBeforeDep', 'totalSeats',
      'availMain', 'availCPlus', 'availOnePS'];
  var colData = {};
  fields.forEach(function (f) {
    colData[f] = sheet.getRange(1, cols[f], lastRow, 1).getValues();
  });

  var index = {};
  for (var i = 0; i < lastRow; i++) {
    var org = colData.org[i][0], dest = colData.dest[i][0], dep = colData.dep[i][0];
    if (org === '' || org === null || dest === '' || dest === null || dep === '' || dep === null) continue;
    var hrs = colData.hoursBeforeDep[i][0], ttl = colData.totalSeats[i][0];
    if (typeof hrs !== 'number' || typeof ttl !== 'number') continue;
    var key = String(colData.car[i][0]).toLowerCase() + '|' + String(org).toLowerCase() + '|' +
        String(dest).toLowerCase() + '|' + colData.flightYr[i][0] + '|' + colData.flightMMDD[i][0];
    if (!index[key]) index[key] = [];
    var ceiling = (colData.availMain[i][0] === 9 || colData.availCPlus[i][0] === 9 || colData.availOnePS[i][0] === 9);
    index[key].push({ org: org, dep: dep, hrs: hrs, ttl: ttl, ceiling: ceiling });
  }
  return index;
}

// Computes {t12, t4, t1} for one flight instance by looking it up in a
// pre-built index (see buildReadingsIndex_) instead of scanning the sheet.
function computeBucketEstimatesFromIndex_(index, car, org, dest, flightYr, flightMMDD, dep, offsets) {
  var key = car + '|' + org + '|' + dest + '|' + flightYr + '|' + flightMMDD;
  var candidates = index[key] || [];
  var depEtMinutes = toEtMinutes_(dep, org, offsets);
  var readings = candidates.filter(function (c) {
    return Math.abs(toEtMinutes_(c.dep, c.org, offsets) - depEtMinutes) <= FULL_MATCH_WINDOW_MINUTES_;
  }).map(function (c) { return { hrs: c.hrs, ttl: c.ttl, ceiling: c.ceiling }; });

  return {
    t12: computeBucketEstimate_(readings, 12),
    t4: computeBucketEstimate_(readings, 4),
    t1: computeBucketEstimate_(readings, T1_TARGET_HOURS_)
  };
}

// Computes and writes t12/t4/t1 for ONE just-created row, based on whatever
// sibling readings already exist for that same flight instance right now.
// Best-effort at creation time - a brand new flight with no other readings
// yet will just get '' (blank) or a flat single-point value. Does NOT
// automatically update later if a sibling row for the same flight gets new
// data - run "Recompute All T-12/T-4/T-1" from the menu periodically (e.g.
// after a check session) to refresh everything.
//
// index is optional: pass a pre-built one (buildReadingsIndex_) when
// writing many rows in a loop, so the sheet only gets scanned once for the
// whole batch rather than once per row. Omit it for a genuine one-off call
// and this builds (and discards) its own index internally.
function writeBucketEstimatesForRow_(sheet, cols, r, offsets, index) {
  if (!index) index = buildReadingsIndex_(sheet, cols);

  var car = String(sheet.getRange(r, cols.car).getValue()).toLowerCase();
  var org = String(sheet.getRange(r, cols.org).getValue()).toLowerCase();
  var dest = String(sheet.getRange(r, cols.dest).getValue()).toLowerCase();
  var dep = sheet.getRange(r, cols.dep).getValue();
  var flightYr = sheet.getRange(r, cols.flightYr).getValue();
  var flightMMDD = sheet.getRange(r, cols.flightMMDD).getValue();

  var est = computeBucketEstimatesFromIndex_(index, car, org, dest, flightYr, flightMMDD, dep, offsets);

  sheet.getRange(r, cols.t12Est).setValue(est.t12);
  sheet.getRange(r, cols.t4Est).setValue(est.t4);
  sheet.getRange(r, cols.t1Est).setValue(est.t1);
}

// Recomputes t12/t4/t1 for EVERY row in canBuy in one efficient pass -
// replaces the old "copy the live formula down 1,200 rows" backfill plan,
// and doubles as the periodic refresh you'd run after a check session
// (since these values no longer auto-update on their own).
//
// Each row gets its OWN estimate, based only on readings whose check
// timestamp is at or before that row's own check timestamp - NOT one
// shared value broadcast across every row of the flight. That's deliberate:
// sorted by check time, the early checks of the day should show rough
// (or blank) estimates, and later checks should show the estimate
// sharpening as more of that day's readings accumulate - watching that
// improve row by row is the whole point of this recompute.
//
// Groups rows into flight instances ONCE (same fuzzy car/org/dest/
// flightYr/flightMMDD/dep clustering used elsewhere), then within each
// small cluster, computes every member's cutoff-filtered estimate against
// just that cluster (cheap - clusters are a handful of rows, not the whole
// sheet), then writes all results back with three batch setValues calls.
function recomputeAllBucketEstimates() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName('canBuy');
  var ui = SpreadsheetApp.getUi();
  if (!sheet) { ui.alert('No sheet named "canBuy" found.'); return; }

  var cols;
  try {
    cols = getCanBuyColumnMap_(sheet);
  } catch (e) {
    ui.alert(e.message);
    return;
  }

  var offsets = getAirportOffsets_();
  var maxRows = sheet.getMaxRows();
  var fields = ['car', 'org', 'dest', 'flightYr', 'flightMMDD', 'dep', 'hoursBeforeDep', 'totalSeats',
      'availMain', 'availCPlus', 'availOnePS', 'checkYr', 'checkMMDD', 'checkHHMM'];
  var colData = {};
  fields.forEach(function (f) {
    colData[f] = sheet.getRange(1, cols[f], maxRows, 1).getValues();
  });

  // Group by exact car|org|dest|flightYr|flightMMDD key first (fast, O(N)),
  // then cluster each group by dep-time proximity (same technique used in
  // analyzeFullTransitions, back when that existed) rather than an O(N^2)
  // pairwise scan.
  var groups = {};
  for (var i = 0; i < maxRows; i++) {
    var org = colData.org[i][0], dest = colData.dest[i][0], dep = colData.dep[i][0];
    if (org === '' || org === null || dest === '' || dest === null || dep === '' || dep === null) continue;
    var key = String(colData.car[i][0]).toLowerCase() + '|' + String(org).toLowerCase() + '|' +
        String(dest).toLowerCase() + '|' + colData.flightYr[i][0] + '|' + colData.flightMMDD[i][0];
    if (!groups[key]) groups[key] = [];
    var depEtMinutes = toEtMinutes_(dep, org, offsets);
    var hrs = colData.hoursBeforeDep[i][0], ttl = colData.totalSeats[i][0];
    var ceiling = (colData.availMain[i][0] === 9 || colData.availCPlus[i][0] === 9 || colData.availOnePS[i][0] === 9);

    // checkEpoch: a true monotonic minute-level timestamp (survives month/
    // year boundaries, unlike comparing mmdd/hhmm digits directly) - only
    // computed when checkYr/checkMMDD/checkHHMM are all real numbers, which
    // in practice is guaranteed whenever hrs/ttl are real too, since
    // hoursBeforeDepFormula_ itself requires checkHHMM to produce a number.
    var checkEpoch = null;
    var cYr = colData.checkYr[i][0], cMD = colData.checkMMDD[i][0], cHM = colData.checkHHMM[i][0];
    if (typeof cYr === 'number' && typeof cMD === 'number' && typeof cHM === 'number') {
      checkEpoch = dayEpochMinutes_(cYr, Math.floor(cMD / 100), cMD % 100) +
          Math.floor(cHM / 100) * 60 + (cHM % 100);
    }

    groups[key].push({
      rowIndex: i, depEtMinutes: depEtMinutes, hrs: hrs, ttl: ttl, ceiling: ceiling, checkEpoch: checkEpoch
    });
  }

  var t12Out = new Array(maxRows), t4Out = new Array(maxRows), t1Out = new Array(maxRows);

  Object.keys(groups).forEach(function (key) {
    var members = groups[key];
    members.sort(function (a, b) { return a.depEtMinutes - b.depEtMinutes; });
    var clusters = [];
    members.forEach(function (m) {
      var last = clusters.length > 0 ? clusters[clusters.length - 1] : null;
      if (last && Math.abs(m.depEtMinutes - last[last.length - 1].depEtMinutes) <= FULL_MATCH_WINDOW_MINUTES_) {
        last.push(m);
      } else {
        clusters.push([m]);
      }
    });

    clusters.forEach(function (cluster) {
      var valid = cluster.filter(function (m) {
        return typeof m.hrs === 'number' && typeof m.ttl === 'number' && m.checkEpoch !== null;
      });

      valid.forEach(function (m) {
        // Cumulative-as-of-this-check: only readings checked at or before
        // THIS row's own check timestamp count, including this row itself.
        var readingsSoFar = valid
            .filter(function (m2) { return m2.checkEpoch <= m.checkEpoch; })
            .map(function (m2) { return { hrs: m2.hrs, ttl: m2.ttl, ceiling: m2.ceiling }; });

        t12Out[m.rowIndex] = computeBucketEstimate_(readingsSoFar, 12);
        t4Out[m.rowIndex] = computeBucketEstimate_(readingsSoFar, 4);
        t1Out[m.rowIndex] = computeBucketEstimate_(readingsSoFar, T1_TARGET_HOURS_);
      });

      // Rows that had hrs/ttl but no usable checkEpoch (shouldn't normally
      // happen - see the guarantee noted above) get left blank rather than
      // guessed at.
      cluster.filter(function (m) {
        return !(typeof m.hrs === 'number' && typeof m.ttl === 'number' && m.checkEpoch !== null);
      }).forEach(function (m) {
        t12Out[m.rowIndex] = '';
        t4Out[m.rowIndex] = '';
        t1Out[m.rowIndex] = '';
      });
    });
  });

  // Rows with no org/dest/dep (blank/template rows) never entered a group,
  // so they were never assigned - leave those cells untouched rather than
  // blanking them, in case they hold something unrelated.
  var writeCount = 0;
  for (var r = 0; r < maxRows; r++) {
    if (t12Out[r] === undefined) continue;
    writeCount++;
  }
  // Batch-write only the contiguous data range for simplicity - since
  // canBuy's data is one contiguous block starting at CANBUY_DATA_START_ROW,
  // build arrays covering exactly that block.
  var lastRow = maxRows; // 1-based sheet row = array index + 1
  var blockStart = CANBUY_DATA_START_ROW;
  var blockLen = lastRow - blockStart + 1;
  var t12Block = [], t4Block = [], t1Block = [];
  for (var rr = blockStart; rr <= lastRow; rr++) {
    var idx = rr - 1; // 0-based into colData/t12Out arrays
    t12Block.push([t12Out[idx] !== undefined ? t12Out[idx] : '']);
    t4Block.push([t4Out[idx] !== undefined ? t4Out[idx] : '']);
    t1Block.push([t1Out[idx] !== undefined ? t1Out[idx] : '']);
  }
  sheet.getRange(blockStart, cols.t12Est, blockLen, 1).setValues(t12Block);
  sheet.getRange(blockStart, cols.t4Est, blockLen, 1).setValues(t4Block);
  sheet.getRange(blockStart, cols.t1Est, blockLen, 1).setValues(t1Block);

  // No completion ui.alert() here on purpose - Google Sheets already shows
  // its own "running script" / "finished" indicator in the corner, and a
  // second modal on top of that was just noise he had to click through.
  // writeCount is still computed above in case it's ever useful again
  // (e.g. logged to a cell, or reintroduced for a specific error case).
}
