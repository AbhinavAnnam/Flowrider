"""Opt-in, root-only routing-mode smoke test; no training or performance claims."""
import argparse
import json
from pathlib import Path
import sys
import signal

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mininet_topology import preflight,run_network
from routing_state import read_routing_state
from telemetry_store import utc_now
from integration_phase2 import eventually,ping,require
from integration_phase5 import route_for,verify_path


def checks(net,args,result):
    def state():
        value=read_routing_state(args.database)
        require(value.get('source')=='live_mininet' and not value.get('stale'),'Expected fresh live Mininet routing state')
        require(value['snapshot'].get('routing_mode')==args.routing_mode,'Controller routing mode differs from smoke test')
        return value
    snapshot=state()['snapshot']
    result.update(trial_id=snapshot['trial_id'],topology_version=snapshot['topology_version'])
    eventually(lambda:ping(net,'h1','h4'),'initial forwarding')
    require(net.pingAll(timeout='2')==0,'Initial pingall reported loss')
    def verified_route():
        current=state();route=route_for(current,1,4)
        if route and route.get('observed_route'):return route
        return False
    eventually(verified_route,'installed and observed route')
    route=verified_route()
    if args.routing_mode=='baseline':
        require(route['installed_route']['path']==route['baseline_path'],'Static route differs from base-delay Dijkstra')
        require(all(l['routing_weight']==l['base_delay_ms'] for l in state()['snapshot']['topology']['links'] if l['active']),
                'Static weights differ from base delay')
    result['initial_forwarding']=verify_path(net,args,route,require_alternative=False)
    down=False
    try:
        net.configLinkStatus('s2','s5','down');down=True
        eventually(lambda:ping(net,'h1','h4'),'forwarding around failed backbone')
        eventually(lambda:(r:=verified_route()) and [2,5] not in [sorted(e) for e in zip(
            r['installed_route']['path'],r['installed_route']['path'][1:])],'route excludes failed backbone')
        result['failure_forwarding']=verify_path(net,args,verified_route(),require_alternative=False)
        net.configLinkStatus('s2','s5','up');down=False
        eventually(lambda:ping(net,'h1','h4'),'forwarding after backbone restoration')
        if args.routing_mode=='baseline':
            eventually(lambda:(r:=verified_route()) and r['installed_route']['path']==[1,2,5,4],
                       'restored base-delay shortest path')
        result['restored_forwarding']=verify_path(net,args,verified_route(),require_alternative=False)
        require(net.pingAll(timeout='2')==0,'Final pingall reported loss')
        result.update(initial_pingall_loss=0,final_pingall_loss=0,controller_snapshot=state())
    finally:
        if down:net.configLinkStatus('s2','s5','up')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--routing-mode',required=True,choices=('baseline','predictive_ml'))
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--controller-ip',default='127.0.0.1')
    parser.add_argument('--controller-port',type=int,default=16653)
    parser.add_argument('--bandwidth-scale',type=float,default=.1)
    args=parser.parse_args()
    if not args.database.is_file():parser.error('Start the controller with a fresh database first')
    if args.output.exists():parser.error('Use a new output directory; evidence is never overwritten')
    args.output.mkdir(parents=True)
    result=dict(status='failed',source='live_mininet',routing_mode=args.routing_mode,
        started_timestamp=utc_now(),traffic_configuration=dict(generator='integration_phase6',protocol='ICMP',
            stages=['initial_pingall','s2-s5_down_h1-h4','s2-s5_up_h1-h4','final_pingall'],randomness=None))
    def interrupted(signum,frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise SystemExit(128+signum)
    previous=signal.signal(signal.SIGTERM,interrupted)
    try:
        run_network(args,checks=lambda net:checks(net,args,result))
        preflight(args.controller_ip,args.controller_port)
        result.update(status='passed_live_mode_smoke',scoped_cleanup_verified=True)
    except BaseException as error:
        result['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        result['finished_timestamp']=utc_now()
        (args.output/'smoke.json').write_text(json.dumps(result,indent=2))
        signal.signal(signal.SIGTERM,previous)


if __name__=='__main__':main()
