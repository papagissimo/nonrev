"""
Local web server for the SeatLoggingDialog - the direct replacement
for Apps Script's openEntryDialog(). Run this, then open the printed
localhost address in a browser tab.

    python server.py

Everything runs on your own machine: this process IS the "server" (always
listening, never called directly), your browser tab is the "client", and
"localhost" is the reserved address meaning "this same machine" - nothing
here ever leaves your Chromebook or touches the network.
"""

import os
import sqlite3

from flask import Flask, jsonify, request, send_from_directory

import SeatLoggingDialog
import FlightScheduleDialog
import ObservationsBrowser
import GraphObservations
import ServiceGrouping
import FloorEstimates
import PoolingSettingsDialog
import settings as settings_module

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'nonrev.db')
STATIC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 5057

app = Flask(__name__)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'Launcher.html')


@app.route('/log')
def log():
    return send_from_directory(STATIC_DIR, 'SeatLoggingDialog.html')


@app.route('/schedule')
def schedule():
    return send_from_directory(STATIC_DIR, 'FlightScheduleDialog.html')


@app.route('/observations')
def observations():
    return send_from_directory(STATIC_DIR, 'ObservationsBrowser.html')


@app.route('/graph')
def graph():
    return send_from_directory(STATIC_DIR, 'GraphObservations.html')


@app.route('/pooling')
def pooling():
    return send_from_directory(STATIC_DIR, 'PoolingSettingsDialog.html')


@app.route('/api/getLauncherSummary', methods=['GET'])
def api_get_launcher_summary():
    conn = get_conn()
    try:
        return jsonify(SeatLoggingDialog.get_launcher_summary(conn))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/refreshFloorEstimates', methods=['POST'])
def api_refresh_floor_estimates():
    """
    Recomputes floorEstimateCoefficients from scratch. Called once by
    SeatLoggingDialog.html on page load, and on demand from the manual
    button on Launcher.html - never from getNextBatch/cadence code (see
    FloorEstimates.py).
    """
    conn = get_conn()
    try:
        return jsonify(FloorEstimates.refresh_floor_estimates(conn))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getNextBatch', methods=['POST'])
def api_get_next_batch():
    body = request.get_json(silent=True) or {}
    skip_route_days = body.get('skipRouteDays')
    include_departed = body.get('includeDeparted', False)
    forced_route = body.get('forcedRoute')
    conn = get_conn()
    try:
        return jsonify(SeatLoggingDialog.get_next_batch(conn, skip_route_days, include_departed, forced_route))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveAndGetNextBatch', methods=['POST'])
def api_save_and_get_next_batch():
    body = request.get_json(force=True)
    payload = body['payload']
    include_departed = body.get('includeDeparted', False)
    forced_route = body.get('forcedRoute')
    conn = get_conn()
    try:
        return jsonify(SeatLoggingDialog.save_and_get_next_batch(conn, payload, include_departed, forced_route))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveSettings', methods=['POST'])
def api_save_settings():
    new_settings = request.get_json(force=True)
    conn = get_conn()
    try:
        settings_module.save_settings(conn, new_settings)
        return jsonify({'saved': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveOpenFullSettings', methods=['POST'])
def api_save_open_full_settings():
    new_settings = request.get_json(force=True)
    conn = get_conn()
    try:
        ServiceGrouping.save_open_full_settings(conn, new_settings)
        return jsonify({'saved': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getScheduleForRouteDay', methods=['POST'])
def api_get_schedule_for_route_day():
    body = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(FlightScheduleDialog.get_schedule_for_route_day(
            conn, body['org'], body['dest'], body['dow']
        ))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveScheduleForRouteDay', methods=['POST'])
def api_save_schedule_for_route_day():
    payload = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(FlightScheduleDialog.save_schedule_for_route_day(conn, payload))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/copyToOtherDays', methods=['POST'])
def api_copy_to_other_days():
    body = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(FlightScheduleDialog.copy_to_other_days(
            conn, body['org'], body['dest'], body['sourceDow']
        ))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getObservationFilterOptions', methods=['GET'])
def api_get_observation_filter_options():
    conn = get_conn()
    try:
        return jsonify(ObservationsBrowser.get_filter_options(conn))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getObservations', methods=['POST'])
def api_get_observations():
    body = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(ObservationsBrowser.get_observations(
            conn,
            sort_col=body.get('sortCol', 'checkTimestamp'),
            sort_dir=body.get('sortDir', 'desc'),
            limit=body.get('limit', 20),
            filters=body.get('filters', {}),
        ))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/deleteObservations', methods=['POST'])
def api_delete_observations():
    body = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(ObservationsBrowser.delete_observations(conn, body.get('observationIds', [])))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveFlightDayFlag', methods=['POST'])
def api_save_flight_day_flag():
    body = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(SeatLoggingDialog.save_flight_day_flag(
            conn, body['carrier'], body['depTime'], body['org'], body['dest'],
            body['flightDate'], body.get('flag', ''),
        ))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveRouteDayFlag', methods=['POST'])
def api_save_route_day_flag():
    body = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(SeatLoggingDialog.save_route_day_flag(
            conn, body['carrier'], body['org'], body['dest'], body['flightDate'], body.get('flag', ''),
        ))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getRouteOptions', methods=['GET'])
def api_get_route_options():
    conn = get_conn()
    try:
        return jsonify({'routes': GraphObservations.get_route_options(conn)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getGraphData', methods=['POST'])
def api_get_graph_data():
    body = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(GraphObservations.get_graph_data(
            conn, body['org'], body['dest'], body.get('daysOfWeek'),
            body.get('dateFrom'), body.get('dateTo'),
        ))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getExcludedDateRanges', methods=['GET'])
def api_get_excluded_date_ranges():
    conn = get_conn()
    try:
        return jsonify(PoolingSettingsDialog.get_excluded_date_ranges(conn))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveExcludedDateRanges', methods=['POST'])
def api_save_excluded_date_ranges():
    payload = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(PoolingSettingsDialog.save_excluded_date_ranges(conn, payload))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/getDeclineCurveSettings', methods=['GET'])
def api_get_decline_curve_settings():
    conn = get_conn()
    try:
        return jsonify(PoolingSettingsDialog.get_decline_curve_settings(conn))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveDeclineCurveSettings', methods=['POST'])
def api_save_decline_curve_settings():
    payload = request.get_json(force=True)
    conn = get_conn()
    try:
        return jsonify(PoolingSettingsDialog.save_decline_curve_settings(conn, payload))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


if __name__ == '__main__':
    print(f"SeatLoggingDialog running at http://localhost:{PORT}")
    app.run(host='localhost', port=PORT, debug=True)
