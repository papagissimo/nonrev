/**
 * Schedule Edit Dialogue: add/edit/delete FlightSchedule rows for one
 * route + day-of-week at a time, modeled on the same interaction pattern
 * as EntryDialog.gs (a popup you type into, not a spreadsheet view).
 *
 * Unlike the daily entry dialogue (which cadence picks for you), this one
 * always starts from an explicit route + day you type in yourself - there's
 * no "next due" concept here, since schedule edits aren't a daily cadence
 * task. It covers three cases that all turn out to be the same UI:
 *   - fixing the day's wobble (a time nudged, an aircraft corrected)
 *   - a flight Delta added or dropped (add/delete a row)
 *   - building a brand-new route from scratch for a new study
 *
 * FS_COL_CONFIRMED (column H) is new - see setupFlightScheduleConfirmedColumn()
 * below for the one-time setup that adds the header and backfills existing
 * rows. Whole-row, not per-field: a row is "confirmed" once you've opened
 * IT specifically in this dialogue and saved, regardless of whether you
 * actually changed anything - opening and saving IS the confirmation.
 */

var FS_COL_CONFIRMED = 8;

// One row per route (org+dest), not per departure - duration doesn't vary
// by day-of-week or which of a route's several daily flights you're on, so
// this deliberately does NOT live as a duplicated column on FlightSchedule.
// Its own confirmed flag since it's a different grain than FlightSchedule's
// row-level one - unset routes start FALSE, flips TRUE once a real number
// is saved through this dialogue.
var ROUTE_DURATIONS_SHEET_NAME = 'RouteDurations';
var RD_COL_ORG = 1;
var RD_COL_DEST = 2;
var RD_COL_DURATION = 3;
var RD_COL_CONFIRMED = 4;

function normalizeDow_(dow) {
  return String(dow || '').trim().toLowerCase().substring(0, 3);
}

// Looks up the RouteDurations row for a route, or null if the sheet
// doesn't exist yet or has no row for this route.
function getRouteDuration_(org, dest) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(ROUTE_DURATIONS_SHEET_NAME);
  if (!sheet) return null;
  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    var r = data[i];
    if (String(r[RD_COL_ORG - 1] || '').toLowerCase() === org && String(r[RD_COL_DEST - 1] || '').toLowerCase() === dest) {
      return { rowIndex: i + 1, durationMinutes: r[RD_COL_DURATION - 1], confirmed: r[RD_COL_CONFIRMED - 1] === true };
    }
  }
  return null;
}

// Writes (or creates) the RouteDurations row for a route, always setting
// confirmed = TRUE - this is only ever called when he's actually typed a
// real number into the dialogue and saved. Silently does nothing if the
// RouteDurations sheet hasn't been set up yet, rather than blocking the
// rest of the schedule save over a sheet that's optional until he opts in.
function saveRouteDuration_(org, dest, durationMinutes) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(ROUTE_DURATIONS_SHEET_NAME);
  if (!sheet) return;
  var existing = getRouteDuration_(org, dest);
  if (existing) {
    sheet.getRange(existing.rowIndex, RD_COL_DURATION).setValue(durationMinutes);
    sheet.getRange(existing.rowIndex, RD_COL_CONFIRMED).setValue(true);
  } else {
    var r = sheet.getLastRow() + 1;
    sheet.getRange(r, RD_COL_ORG).setValue(org);
    sheet.getRange(r, RD_COL_DEST).setValue(dest);
    sheet.getRange(r, RD_COL_DURATION).setValue(durationMinutes);
    sheet.getRange(r, RD_COL_CONFIRMED).setValue(true);
  }
}

// One-time menu action - creates the RouteDurations sheet with headers if
// it doesn't already exist. Safe to re-run.
function setupRouteDurationsSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var ui = SpreadsheetApp.getUi();
  var sheet = ss.getSheetByName(ROUTE_DURATIONS_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(ROUTE_DURATIONS_SHEET_NAME);
  }
  var header = sheet.getRange(1, 1, 1, 4).getValues()[0];
  if (String(header[0]).trim().toLowerCase() !== 'org') {
    sheet.getRange(1, 1, 1, 4).setValues([['org', 'dest', 'durationMinutes', 'confirmed']]);
  }
  ui.alert('RouteDurations sheet is set up.');
}

/**
 * Menu entry point ("Edit Flight Schedule"). Opens empty except for
 * today's day-of-week, which pre-fills the day picker - route is always
 * typed in, since a brand-new study's route won't exist anywhere to read
 * from a cursor position the way the canBuy pencil-edit does.
 */
function openScheduleEditDialog() {
  var nowInfo = getEasternNow_();
  var todayDow = dowAbbrev_(new Date(nowInfo.checkYYYY, Math.floor(nowInfo.checkMMDD / 100) - 1, nowInfo.checkMMDD % 100));
  var initData = { todayDow: todayDow, aircraftOptions: loadAircraftConfigOptions_() };
  var raw = HtmlService.createHtmlOutputFromFile('ScheduleEditDialog').getContent();
  if (raw.indexOf('__INIT_DATA__') === -1) {
    throw new Error('ScheduleEditDialog.html does not contain the __INIT_DATA__ placeholder - ' +
        'the HTML file contents likely got pasted incompletely. Re-paste the whole file and try again.');
  }
  var withData = raw.replace('__INIT_DATA__', JSON.stringify(initData));
  var html = HtmlService.createHtmlOutput(withData).setWidth(700).setHeight(560);
  SpreadsheetApp.getUi().showModalDialog(html, 'Edit Flight Schedule');
}

// Highest existing "bogusN" flight number across the WHOLE sheet (not just
// this route), so a freshly-added blank row always gets a placeholder that
// can't collide with one already in use elsewhere.
function getNextBogusNumber_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(FLIGHT_SCHEDULE_SHEET_NAME);
  var data = sheet.getDataRange().getValues();
  var max = 0;
  for (var i = 1; i < data.length; i++) {
    var v = String(data[i][FS_COL_FLIGHT_NUMBER - 1] || '');
    var m = v.match(/^bogus(\d+)$/i);
    if (m) {
      var n = parseInt(m[1], 10);
      if (n > max) max = n;
    }
  }
  return max + 1;
}

/**
 * Called when the dialog's Load button fires. Returns every FlightSchedule
 * row for this exact org/dest/dow (empty array if none), plus isNewRoute -
 * true only when this route has ZERO rows on ANY day of the week, which is
 * the one condition under which saveScheduleForRouteDay should later offer
 * to copy the day being built out to the other six days.
 */
function getScheduleForRouteDay(org, dest, dow) {
  org = String(org || '').trim().toLowerCase();
  dest = String(dest || '').trim().toLowerCase();
  dow = normalizeDow_(dow);

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(FLIGHT_SCHEDULE_SHEET_NAME);
  var data = sheet.getDataRange().getValues();

  var rows = [];
  var anyOtherDayRowsForRoute = false;

  for (var i = 1; i < data.length; i++) {
    var r = data[i];
    var rOrg = String(r[FS_COL_ORG - 1] || '').toLowerCase();
    var rDest = String(r[FS_COL_DEST - 1] || '').toLowerCase();
    if (rOrg !== org || rDest !== dest) continue;

    var rDow = normalizeDow_(r[FS_COL_DAY_OF_WEEK - 1]);
    if (rDow !== dow) {
      anyOtherDayRowsForRoute = true;
      continue;
    }

    rows.push({
      scheduleRow: i + 1,
      carrier: r[FS_COL_CARRIER - 1] || 'dl',
      flightNumber: r[FS_COL_FLIGHT_NUMBER - 1] || '',
      dep: r[FS_COL_DEP_TIME - 1],
      depDisplay: hhmmTo12Hour_(r[FS_COL_DEP_TIME - 1]),
      aircraftConfig: r[FS_COL_AIRCRAFT_CONFIG - 1] || '',
      confirmed: r[FS_COL_CONFIRMED - 1] === true
    });
  }

  rows.sort(function (a, b) { return a.dep - b.dep; });

  var duration = getRouteDuration_(org, dest);

  return {
    org: org, dest: dest, dow: dow,
    rows: rows,
    isNewRoute: rows.length === 0 && !anyOtherDayRowsForRoute,
    nextBogusNumber: getNextBogusNumber_(),
    aircraftOptions: loadAircraftConfigOptions_(),
    durationMinutes: duration ? duration.durationMinutes : '',
    durationConfirmed: duration ? duration.confirmed : false
  };
}

/**
 * Called from the dialog's Save button.
 *
 * payload: {
 *   org, dest, dow,
 *   rows: [{ scheduleRow (existing row number, or null for a new row),
 *            carrier, flightNumber, dep (24h int), aircraftConfig,
 *            deleted (bool) }],
 *   durationMinutes: '' or a number-as-text
 * }
 *
 * Order matters here: existing rows are updated FIRST (using their
 * original scheduleRow numbers, before anything shifts), THEN deletions
 * run in descending row order (so deleting a higher row never invalidates
 * a lower row number still waiting to be deleted), THEN new rows are
 * appended. Doing it any other order risks a delete silently shifting a
 * not-yet-processed scheduleRow onto the wrong data.
 */
function saveScheduleForRouteDay(payload) {
  var org = String(payload.org || '').trim().toLowerCase();
  var dest = String(payload.dest || '').trim().toLowerCase();
  var dow = normalizeDow_(payload.dow);

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(FLIGHT_SCHEDULE_SHEET_NAME);

  // Snapshot BEFORE any writes - the copy-to-other-days offer must only
  // ever fire for a route that had genuinely zero rows anywhere, never on
  // top of a route that already has real day-specific data elsewhere.
  var isNewRoute = getScheduleForRouteDay(org, dest, dow).isNewRoute;

  // Pass 1: update existing, non-deleted rows in place.
  payload.rows.forEach(function (entry) {
    if (!entry.scheduleRow || entry.deleted) return;
    sheet.getRange(entry.scheduleRow, FS_COL_CARRIER).setValue(entry.carrier || 'dl');
    sheet.getRange(entry.scheduleRow, FS_COL_FLIGHT_NUMBER).setValue(entry.flightNumber);
    sheet.getRange(entry.scheduleRow, FS_COL_DEP_TIME).setValue(entry.dep);
    sheet.getRange(entry.scheduleRow, FS_COL_AIRCRAFT_CONFIG).setValue(entry.aircraftConfig);
    sheet.getRange(entry.scheduleRow, FS_COL_CONFIRMED).setValue(true);
  });

  // Pass 2: delete removed rows, highest sheet row first.
  var toDelete = payload.rows
      .filter(function (entry) { return entry.scheduleRow && entry.deleted; })
      .map(function (entry) { return entry.scheduleRow; })
      .sort(function (a, b) { return b - a; });
  toDelete.forEach(function (r) { sheet.deleteRow(r); });

  // Pass 3: append brand-new rows.
  var newRows = payload.rows.filter(function (entry) { return !entry.scheduleRow && !entry.deleted; });
  newRows.forEach(function (entry) {
    var r = sheet.getLastRow() + 1;
    sheet.getRange(r, FS_COL_CARRIER).setValue(entry.carrier || 'dl');
    sheet.getRange(r, FS_COL_FLIGHT_NUMBER).setValue(entry.flightNumber);
    sheet.getRange(r, FS_COL_ORG).setValue(org);
    sheet.getRange(r, FS_COL_DEST).setValue(dest);
    sheet.getRange(r, FS_COL_DAY_OF_WEEK).setValue(dow);
    sheet.getRange(r, FS_COL_DEP_TIME).setValue(entry.dep);
    sheet.getRange(r, FS_COL_AIRCRAFT_CONFIG).setValue(entry.aircraftConfig);
    sheet.getRange(r, FS_COL_CONFIRMED).setValue(true);
  });

  // Route duration lives in its own sheet/grain (see getRouteDuration_ /
  // saveRouteDuration_ above) - only touched when a real value was typed
  // in, never blocking the FlightSchedule writes above if it's blank or
  // the RouteDurations sheet hasn't been set up yet.
  if (payload.durationMinutes !== '' && payload.durationMinutes !== null && payload.durationMinutes !== undefined) {
    saveRouteDuration_(org, dest, Number(payload.durationMinutes));
  }

  return {
    savedCount: payload.rows.length,
    offerCopy: isNewRoute && newRows.length > 0,
    org: org, dest: dest, dow: dow
  };
}

/**
 * Called only after the client's "copy to the other 6 days?" confirm
 * comes back yes. Copies whatever's currently saved for org/dest/sourceDow
 * onto every OTHER day of the week, each new row written with
 * confirmed = FALSE - these are inherited guesses, not independently
 * checked against that day's actual schedule, so they stay flagged until
 * someone opens that specific day here and saves it for real.
 */
function copyToOtherDays(org, dest, sourceDow) {
  org = String(org || '').trim().toLowerCase();
  dest = String(dest || '').trim().toLowerCase();
  sourceDow = normalizeDow_(sourceDow);

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(FLIGHT_SCHEDULE_SHEET_NAME);
  var data = sheet.getDataRange().getValues();

  var sourceRows = [];
  for (var i = 1; i < data.length; i++) {
    var r = data[i];
    var rOrg = String(r[FS_COL_ORG - 1] || '').toLowerCase();
    var rDest = String(r[FS_COL_DEST - 1] || '').toLowerCase();
    var rDow = normalizeDow_(r[FS_COL_DAY_OF_WEEK - 1]);
    if (rOrg === org && rDest === dest && rDow === sourceDow) {
      sourceRows.push({
        carrier: r[FS_COL_CARRIER - 1] || 'dl',
        flightNumber: r[FS_COL_FLIGHT_NUMBER - 1] || '',
        dep: r[FS_COL_DEP_TIME - 1],
        aircraftConfig: r[FS_COL_AIRCRAFT_CONFIG - 1] || ''
      });
    }
  }

  var targetDays = DOW_ABBREV_.filter(function (d) { return d !== sourceDow; });
  var copiedCount = 0;
  targetDays.forEach(function (day) {
    sourceRows.forEach(function (src) {
      var r = sheet.getLastRow() + 1;
      sheet.getRange(r, FS_COL_CARRIER).setValue(src.carrier);
      sheet.getRange(r, FS_COL_FLIGHT_NUMBER).setValue(src.flightNumber);
      sheet.getRange(r, FS_COL_ORG).setValue(org);
      sheet.getRange(r, FS_COL_DEST).setValue(dest);
      sheet.getRange(r, FS_COL_DAY_OF_WEEK).setValue(day);
      sheet.getRange(r, FS_COL_DEP_TIME).setValue(src.dep);
      sheet.getRange(r, FS_COL_AIRCRAFT_CONFIG).setValue(src.aircraftConfig);
      sheet.getRange(r, FS_COL_CONFIRMED).setValue(false);
      copiedCount++;
    });
  });

  return { copiedCount: copiedCount };
}

/**
 * One-time menu action. Adds the "confirmed" header to FlightSchedule
 * column H if it's not already there, backfills every existing data row
 * to FALSE (per the standing decision: the aircraft-config backlog is
 * genuinely suspect, so existing rows start unconfirmed rather than
 * assumed-good), and adds a conditional-formatting rule that highlights
 * flightNumber-through-aircraftConfig whenever confirmed is FALSE - a
 * visual marker that never touches the values themselves, so it can never
 * break the AircraftConfigs lookup the way a decorated value would.
 * Safe to re-run: only blank/non-boolean confirmed cells get touched.
 */
function setupFlightScheduleConfirmedColumn() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(FLIGHT_SCHEDULE_SHEET_NAME);
  var ui = SpreadsheetApp.getUi();
  if (!sheet) { ui.alert('No sheet named "FlightSchedule" found.'); return; }

  var header = sheet.getRange(1, FS_COL_CONFIRMED).getValue();
  if (String(header).trim().toLowerCase() !== 'confirmed') {
    sheet.getRange(1, FS_COL_CONFIRMED).setValue('confirmed');
  }

  var lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    var range = sheet.getRange(2, FS_COL_CONFIRMED, lastRow - 1, 1);
    var values = range.getValues();
    var changed = false;
    for (var i = 0; i < values.length; i++) {
      if (values[i][0] !== true && values[i][0] !== false) {
        values[i][0] = false;
        changed = true;
      }
    }
    if (changed) range.setValues(values);
  }

  var maxRows = Math.max(sheet.getMaxRows(), 1000);
  var formatRange = sheet.getRange(2, FS_COL_FLIGHT_NUMBER, maxRows - 1, FS_COL_CONFIRMED - FS_COL_FLIGHT_NUMBER + 1);
  var confirmedColLetter = columnLetter_(FS_COL_CONFIRMED);
  var rule = SpreadsheetApp.newConditionalFormatRule()
      .whenFormulaSatisfied('=$' + confirmedColLetter + '2=FALSE')
      .setBackground('#FFF2CC')
      .setRanges([formatRange])
      .build();
  sheet.setConditionalFormatRules([rule]);

  ui.alert('FlightSchedule "confirmed" column is set up. Existing rows start unconfirmed, per your call.');
}