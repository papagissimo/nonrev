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
    return send_from_directory(STATIC_DIR, 'SeatLoggingDialog.html')


@app.route('/api/getNextBatch', methods=['POST'])
def api_get_next_batch():
    skip_route_keys = (request.get_json(silent=True) or {}).get('skipRouteKeys', [])
    conn = get_conn()
    try:
        return jsonify(SeatLoggingDialog.get_next_batch(conn, skip_route_keys))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/saveAndGetNextBatch', methods=['POST'])
def api_save_and_get_next_batch():
    body = request.get_json(force=True)
    payload = body['payload']
    skip_route_keys = body.get('skipRouteKeys', [])
    conn = get_conn()
    try:
        return jsonify(SeatLoggingDialog.save_and_get_next_batch(conn, payload, skip_route_keys))
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


if __name__ == '__main__':
    print(f"SeatLoggingDialog running at http://localhost:{PORT}")
    app.run(host='localhost', port=PORT, debug=True)
