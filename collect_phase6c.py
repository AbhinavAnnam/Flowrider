"""Root-only Phase 6C collector; launched by scripts/run_phase6c.py."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

from phase6c import (check_queue, forwarding_proof, parse_ping,
                    validate_collection, validate_plan, write_json, expected_tc, bottleneck_interfaces)


def utc():
    return datetime.now(timezone.utc).isoformat()


def interface_tc(interface,kind):
    # Host-side access interfaces live in host namespaces, not the root namespace.
    process=interface.node.popen(['tc','-s',kind,'show','dev',interface.name],
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=dict(os.environ,LC_ALL='C'))
    try:
        output,_=process.communicate(timeout=15)
    except BaseException:
        process.kill();process.communicate(timeout=3)
        raise
    finally:
        if process.stdout is not None:process.stdout.close()
    if process.returncode:raise RuntimeError(f'TC inspection failed for {interface.name}: {output}')
    return output


def capture_tc(net, plan, apply=False):
    from topology_config import check_tc_output
    records=[];expected=expected_tc(plan);bottlenecks=bottleneck_interfaces(plan)
    for link in net.links:
        interfaces=(link.intf1,link.intf2)
        for interface in interfaces:
            rate,delay,queue=expected[interface.name]
            if interface.name in bottlenecks and apply:
                interface.config(bw=rate,delay=f'{delay}ms',max_queue_size=queue)
            qdisc=interface_tc(interface,'qdisc')
            classes=interface_tc(interface,'class')
            check_tc_output(qdisc,classes,rate,delay)
            if queue is not None:check_queue(qdisc,queue)
            records.append(dict(interface=interface.name,bandwidth_mbps=rate,delay_ms=delay,
                queue_packets=queue,qdisc=qdisc,classes=classes,timestamp=utc()))
    return records


def record_forwarding(stage,route,source,destination,prior_counts):
    """A verified zero readback is a baseline, never positive forwarding proof."""
    installed=route.get('installed_route') or {};observed=route.get('observed_route') or {}
    path=installed.get('path',[]);cookie=installed.get('cookie')
    if not path or cookie is None or observed.get('cookie')!=cookie:return
    for sender,receiver,field in ((source,destination,'forwarding'),
                                  (destination,source,'bidirectional_forwarding')):
        host=int(sender[1:]);mac=f'00:00:00:00:00:{host:02x}'
        rows=[c for c in observed.get('counters',[]) if c['source_mac']==mac]
        counts={c['dpid']:c['packet_count'] for c in rows}
        if (len(rows)!=len(path) or set(counts)!=set(path) or
                any(type(v) is not int or v<0 for v in counts.values())):continue
        previous=prior_counts.setdefault((sender,receiver,cookie),counts)
        if forwarding_proof(route,host) and all(counts[d]>previous[d] for d in counts):
            stage[field][f'{source}-{destination}']=dict(timestamp=utc(),route=route,
                starting_counters=previous,ending_counters=counts)


def collect(net,args,result,plan):
    from mininet_topology import command
    from routing_state import read_routing_state
    from phase3 import sessions
    sys.path.insert(0,str(Path(__file__).parent/'tests'))
    from integration_phase2 import eventually, require
    from integration_phase5 import run_stage, stop_process, route_for
    pairs=plan['flow_pairs']
    bottleneck=set(map(int,plan['bottleneck']['link_id'].replace('s','').split('-')))

    available=sessions(args.database)
    require(len(available)==1,'Each trial requires a fresh one-session database')
    session=available[0];config=json.loads(session['config_json'])
    require(session['source']=='live_mininet' and config['routing_mode']==args.routing_mode,'Controller provenance/mode mismatch')
    require(config['trial_id']==args.trial_id and config['traffic_configuration']==plan,'Controller/collector trial plan mismatch')
    require(config.get('experiment_link_capacities_mbps')==plan['link_capacities_mbps'],'Telemetry capacity override missing')
    result['session_id']=session['session_id']
    tc=capture_tc(net,plan,apply=True)
    write_json(args.output/'applied-tc.json',tc);result['tc_verified']=True
    require(net.pingAll(timeout='2')==0,'Initial connectivity loss')
    def initialized():
        state=read_routing_state(args.database)
        if state.get('stale'):return False
        return all((r:=route_for(state,int(src[1:]),int(dst[1:]))) and forwarding_proof(r,int(src[1:]))
            and r['installed_route']['path']==r['baseline_path']
            and forwarding_proof(r,int(dst[1:]))
            and any({u,v}==bottleneck for u,v in zip(r['baseline_path'],r['baseline_path'][1:]))
            for src,dst in pairs)
    eventually(initialized,'verified bidirectional initial forwarding over the shared bottleneck')
    result['initial_state']=read_routing_state(args.database)
    servers=[];probes=[];handles=[];stages=result['stages'];start=time.monotonic()
    traffic=args.output/'traffic';traffic.mkdir()
    try:
        for dst in sorted({dst for _,dst in pairs}):
            handle=(traffic/f'{dst}-server.log').open('w');handles.append(handle)
            servers.append(net[dst].popen(['iperf3','-s'],stdout=handle,stderr=subprocess.STDOUT))
        time.sleep(1)
        require(all(p.poll() is None for p in servers),'iperf server startup failed')
        for index,planned in enumerate(plan['stages']):
            stage=dict(planned,index=index,duration_seconds=planned['seconds'],probes=[],forwarding={},
                bidirectional_forwarding={},flows=[(src,dst,planned['rate_mbps']) for src,dst in pairs],
                started_timestamp=utc(),actual_elapsed_seconds=time.monotonic()-start)
            stages.append(stage);probes=[]
            stage['traffic_started']=time.time()
            stage['traffic_started_monotonic']=time.monotonic()
            try:
                for src,dst in pairs:
                    file=traffic/f'{index:02d}-{src}-{dst}.ping.log'
                    handle=file.open('w');handles.append(handle)
                    count=round(planned['seconds']/plan['probe_interval_seconds'])
                    started=time.time()
                    started_monotonic=time.monotonic()
                    command_line=['ping','-D','-n','-i',str(plan['probe_interval_seconds']),
                        '-W',str(plan['probe_timeout_seconds']),'-c',str(count),net[dst].IP()]
                    process=net[src].popen(command_line,stdout=handle,stderr=subprocess.STDOUT,
                        env=dict(os.environ,LC_ALL='C'))
                    metadata=dict(source=src,destination=dst,file=file.name,command=command_line,
                        started=started,scheduled_last_send=started+(count-1)*plan['probe_interval_seconds'],
                        started_monotonic=started_monotonic,
                        scheduled_last_send_monotonic=started_monotonic+(count-1)*plan['probe_interval_seconds'],
                        expected_count=count,complete=False)
                    stage['probes'].append(metadata);probes.append((process,handle,metadata))
                last_capture=0
                prior_counts={}
                def observe():
                    nonlocal last_capture
                    state=read_routing_state(args.database)
                    if not state.get('stale'):
                        for src,dst in pairs:
                            route=route_for(state,int(src[1:]),int(dst[1:]))
                            if route:record_forwarding(stage,route,src,dst,prior_counts)
                    if time.monotonic()-last_capture>=5:
                        last_capture=time.monotonic()
                        record=dict(timestamp=utc(),stage=index,tc={},ovs={})
                        for name in bottleneck_interfaces(plan):
                            record['tc'][name]=command('tc','-s','qdisc','show','dev',name)
                        for switch in net.switches:
                            record['ovs'][switch.name]=command('ovs-ofctl','-O','OpenFlow13','dump-flows',switch.name)
                        with (args.output/'forwarding-tc.jsonl').open('a') as handle:
                            handle.write(json.dumps(record)+'\n')
                observe()
                run_stage(net,SimpleNamespace(output=traffic,database=args.database),stage,observe)
                # A version can refresh after the last periodic dump. Capture
                # its final readback/counters before closing this stage window.
                last_capture=0
                observe()
                stage['traffic_finished']=time.time()
                stage['traffic_finished_monotonic']=time.monotonic()
            finally:
                stage.setdefault('traffic_finished',time.time())
                stage.setdefault('traffic_finished_monotonic',time.monotonic())
                for process,handle,probe in probes:
                    try:
                        if stage.get('status')!='passed':
                            probe['error']='Traffic stage aborted or failed'
                            probe['exit_code']=stop_process(process)['exit_code']
                        else:
                            remaining=max(0,probe['started_monotonic']+planned['seconds']+plan['probe_timeout_seconds']+2-time.monotonic())
                            probe['exit_code']=process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:probe['error']='RTT probe timed out'
                    finally:
                        probe['cleanup']=stop_process(process);handle.flush()
                        try:
                            probe.update(parse_ping((traffic/probe['file']).read_text(),probe['expected_count']))
                            probe['complete']='error' not in probe and probe['exit_code'] in (0,1)
                        except (ValueError,KeyError) as error:probe['error']=str(error)
                stage['finished_timestamp']=utc()
                stage['flows']=[dict(source=c['source'],destination=c['destination'],iperf_json=c['stdout'])
                    for c in stage.get('clients',[])]
                write_json(args.output/'collection.json',result)
                write_json(traffic/'traffic.json',dict(source='live_mininet',session_id=session['session_id'],
                    routing_mode=args.routing_mode,trial_id=args.trial_id,topology_version=plan['topology_version'],
                    seed=plan['seed'],bandwidth_scale=plan['bandwidth_scale'],requested_traffic_seconds=plan['seconds'],
                    elapsed_seconds=time.monotonic()-start,stages=stages))
            require(stage['status']=='passed','Incomplete iperf stage; inspect saved diagnostics')
            require(all(p['complete'] for p in stage['probes']),'Incomplete RTT measurement')
        require(net.pingAll(timeout='2')==0,'Final connectivity loss')
        write_json(args.output/'final-tc.json',capture_tc(net,plan))
        result['initial_pingall_loss']=result['final_pingall_loss']=0
        # Keep ports connected until final traffic intervals and +15s targets
        # mature. Teardown would otherwise replace those samples with inactive.
        time.sleep(plan.get('telemetry_tail_seconds',0))
    finally:
        cleanups=[stop_process(p) for p in servers]+[stop_process(p) for p,_,_ in probes]
        for handle in handles:handle.close()
        write_json(args.output/'process-cleanup.json',cleanups)
        result['owned_processes_cleaned']=all(p['cleaned_up'] for p in cleanups) and all(
            c.get('cleanup',{}).get('cleaned_up',False) for s in stages
            for c in s.get('clients',[])+s.get('probes',[]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--controller-ip',default='127.0.0.1')
    parser.add_argument('--controller-port',type=int,default=16653)
    args=parser.parse_args();args.output=args.output.resolve()
    manifest=json.loads((args.output/'trial.json').read_text());plan=manifest['traffic_configuration'];validate_plan(plan)
    args.database=Path(manifest['database']);args.trial_id=manifest['trial_id']
    args.routing_mode=manifest['routing_mode'];args.bandwidth_scale=plan['bandwidth_scale']
    args.topology=plan.get('topology','six');args.queue_packets=plan['bottleneck']['queue_packets']
    if (args.output/'collection.json').exists():parser.error('Refusing to reuse an existing collector output')
    result=dict(status='failed',source='live_mininet',started_timestamp=utc(),stages=[])
    def interrupted(signum,frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,interrupted)
    from mininet_topology import run_network,preflight
    try:
        run_network(args,checks=lambda net:collect(net,args,result,plan))
        preflight(args.controller_ip,args.controller_port)
        result['scoped_cleanup_verified']=True
        errors=validate_collection(plan,result)
        if errors:raise RuntimeError('; '.join(errors))
        result['status']='collected'
    except BaseException as error:
        result['error']=f'{type(error).__name__}: {error}'
        # run_network has already executed its scoped finally/net.stop.
        try:
            preflight(args.controller_ip,args.controller_port)
            result['scoped_cleanup_verified']=True
        except Exception as cleanup_error:result['cleanup_error']=str(cleanup_error)
        raise
    finally:
        result['finished_timestamp']=utc()
        write_json(args.output/'collection.json',result)


if __name__=='__main__':main()
