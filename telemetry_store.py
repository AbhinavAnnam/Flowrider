"""Append-only, transactionally committed research records with explicit provenance."""

import json
import math
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime, timezone

TABLES = ('raw_ports', 'directions', 'observations', 'predictions')
SOURCES = ('live_mininet', 'simulated_openflow', 'synthetic')
SCHEMA_VERSION = 1


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class TelemetryStore:
    def __init__(self, path, source, config):
        if source not in SOURCES:
            raise ValueError('Unknown provenance')
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('CREATE TABLE IF NOT EXISTS sessions ('
            'session_id TEXT PRIMARY KEY, started_timestamp TEXT NOT NULL, source TEXT NOT NULL, '
            'schema_version INTEGER NOT NULL, config_json TEXT NOT NULL)')
        for table in TABLES:
            self.connection.execute(f'CREATE TABLE IF NOT EXISTS {table} ('
                'id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), '
                'observation_timestamp TEXT NOT NULL, link_id TEXT, record_json TEXT NOT NULL)')
            self.connection.execute(f'CREATE INDEX IF NOT EXISTS {table}_session_link '
                                    f'ON {table}(session_id, link_id, id)')
        self.session_id = uuid.uuid4().hex
        self.source = source
        self.last_elapsed = {}
        with self.connection:
            self.connection.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',
                (self.session_id, utc_now(), source, SCHEMA_VERSION, json.dumps(config, allow_nan=False)))

    def write_batch(self, raw_ports, directions, observations, predictions):
        # Validate/serialize the entire batch before starting an atomic write.
        batches = []
        ordering = self.last_elapsed.copy()
        for table, rows in zip(TABLES, (raw_ports, directions, observations, predictions)):
            encoded = []
            for row in rows:
                record = dict(row, session_id=self.session_id, source=self.source, schema_version=SCHEMA_VERSION)
                if table == 'observations' and record.get('valid'):
                    if not record.get('link_active') or not all(
                            isinstance(record.get(k), (int, float)) and math.isfinite(record[k])
                            for k in ('utilization', 'tx_mbps', 'rx_mbps', 'capacity_mbps')):
                        raise ValueError('Incomplete observation cannot be marked valid')
                timestamp = record['observation_timestamp']
                if datetime.fromisoformat(timestamp.replace('Z', '+00:00')).utcoffset() is None:
                    raise ValueError('Observation timestamps must have a UTC offset')
                if 'monotonic_elapsed_time' in record and record['monotonic_elapsed_time'] < 0:
                    raise ValueError('Negative session elapsed time')
                if 'monotonic_elapsed_time' in record:
                    key = (table, record.get('link_id'), record.get('source_dpid'), record.get('source_port'))
                    elapsed = record['monotonic_elapsed_time']
                    if elapsed <= ordering.get(key, -1):
                        raise ValueError('Out-of-order/duplicate observation within a session stream')
                    ordering[key] = elapsed
                encoded.append((self.session_id, timestamp, record.get('link_id'),
                                json.dumps(record, allow_nan=False)))
            batches.append(encoded)
        with self.connection:
            for table, rows in zip(TABLES, batches):
                self.connection.executemany(f'INSERT INTO {table} '
                    '(session_id,observation_timestamp,link_id,record_json) VALUES (?,?,?,?)', rows)
        self.last_elapsed = ordering

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def read_rows(path, table, session_id=None):
    if table not in TABLES:
        raise ValueError('Unknown table')
    with sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True) as connection:
        query = f'SELECT record_json FROM {table}'
        args = ()
        if session_id:
            query += ' WHERE session_id=?'
            args = (session_id,)
        query += ' ORDER BY id'
        return [json.loads(row[0]) for row in connection.execute(query, args)]
