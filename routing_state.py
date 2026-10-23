"""Read-only Phase 5 route history API/CLI; snapshots are persisted controller state."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def read_routing_state(path, event_limit=30):
    if not 0<=event_limit<=500:raise ValueError('event_limit must be between 0 and 500')
    try:
        with sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro',uri=True,timeout=.5) as connection:
            session=connection.execute('SELECT session_id,source FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()
            if session is None:return dict(status='unavailable',reason='no_session',snapshot=None,events=[])
            row=connection.execute('SELECT record_json FROM routing_snapshots WHERE session_id=? ORDER BY id DESC LIMIT 1',(session[0],)).fetchone()
            events=[json.loads(r[0]) for r in connection.execute('SELECT record_json FROM routing_events WHERE session_id=? ORDER BY id DESC LIMIT ?',(session[0],event_limit))]
            snapshot=json.loads(row[0]) if row else None
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(snapshot['timestamp'].replace('Z','+00:00'))).total_seconds() if snapshot else None
            stale=age is None or not 0<=age<=5
            return dict(status='stale' if stale else 'available',stale=stale,snapshot_age_seconds=age,
                        session_id=session[0],source=session[1],snapshot=snapshot,events=events,
                        note='Persisted controller snapshot; stale entries do not prove a currently active route.')
    except (sqlite3.Error,OSError,ValueError,KeyError) as error:
        return dict(status='unavailable',stale=True,reason=str(error),snapshot=None,events=[])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--events',type=int,default=30)
    args=parser.parse_args()
    try:print(json.dumps(read_routing_state(args.database,args.events),indent=2,allow_nan=False))
    except ValueError as error:parser.error(str(error))


if __name__=='__main__':main()
