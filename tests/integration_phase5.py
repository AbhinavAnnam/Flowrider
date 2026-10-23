"""Opt-in LIVE Mininet acceptance check. Requires the Phase 5 controller.

Only real OpenFlow counters and the existing trained model can trigger a pass.
No forecast injection, training, global cleanup, or unrelated process termination.
"""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mininet.log import setLogLevel
from mininet_topology import run_network,command,preflight
from routing_state import read_routing_state
from topology_config import HOSTS
from integration_phase2 import eventually,require,ping


# Keep the original ten-second overhead allowance. -t covers transmission,
# not TCP control connection setup or the final server-results exchange.
CONNECT_SECONDS = 5
RESULT_SECONDS = 5
STOP_SECONDS = 3


def stop_process(process):
    """Flush iperf JSON/ping summaries first, then escalate and always reap."""
    result = dict(pid=process.pid, command=process.args, signals=[], errors=[])
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            if process.poll() is None:
                process.send_signal(sig)
                result['signals'].append(sig.name)
            process.wait(timeout=STOP_SECONDS)
            break
        except ProcessLookupError:
            continue
        except (OSError, subprocess.TimeoutExpired) as error:
            result['errors'].append(str(error))
    result.update(exit_code=process.poll(), cleaned_up=process.poll() is not None)
    return result


def client_diagnostics(net,args,process,src,dst):
    """Capture the state before interrupting a stuck client; never wait forever."""
    result = dict(captured=time.time(), pid=process.pid, command=process.args,
                  routing_state=read_routing_state(args.database), sockets={})
    for name in ('status','wchan'):
        try:result[name]=(Path('/proc')/str(process.pid)/name).read_text()
        except OSError as error:result[name]=str(error)
    for host in (src,dst):
        diagnostic=None
        try:
            diagnostic=net[host].popen(['ss','-tunap'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            output,_=diagnostic.communicate(timeout=2)
            result['sockets'][host]=dict(exit_code=diagnostic.returncode,
                output=output.decode(errors='replace') if isinstance(output,bytes) else output)
        except (OSError,subprocess.SubprocessError) as error:
            result['sockets'][host]=dict(error=str(error))
        finally:
            if diagnostic is not None:
                result['sockets'][host]['cleanup']=stop_process(diagnostic)
                if diagnostic.stdout is not None:diagnostic.stdout.close()
    return result


def read_client_measurements(output,client,duration):
    """Keep raw files intact and reject missing/partial results, even on exit 0."""
    try:
        data=json.loads((output/client['stdout']).read_text())
        client['measurements']=data.get('end',{})
        client['iperf_error']=data.get('error')
        sent=client['measurements'].get('sum_sent',{})
        received=client['measurements'].get('sum_received',{})
        # iperf's timer can end a fraction of a second before the requested -t.
        complete=(not data.get('error') and data['start']['test_start']['duration']==duration
                  and sent['seconds']>=duration-.1 and received['seconds']>=duration-.1
                  and sent['bytes']>0 and received['bytes']>0)
        client['measurements_complete']=complete
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
        client.update(measurements_complete=False,measurement_error=str(error))
    if client['status']=='passed' and not client['measurements_complete']:
        client['status']='incomplete'
    try:client['stderr_tail']=(output/client['stderr']).read_text()[-8192:]
    except OSError as error:client['stderr_tail']=str(error)


def run_stage(net,args,stage,observe):
    clients=[];handles=[]
    duration=stage['duration_seconds'];index=stage['index'];rate=stage['rate_mbps']
    flows=stage.get('flows') or [('h1','h4',rate),('h3','h6',rate)]
    stage.update(status='running',clients=[])
    with ExitStack() as files:
        try:
            for src,dst,client_rate in flows:
                name=f'{index:02d}-{src}-{dst}'
                stdout=files.enter_context((args.output/f'{name}.json').open('w'))
                stderr=files.enter_context((args.output/f'{name}.stderr.log').open('w'))
                handles.extend((stdout,stderr))
                command=['iperf3','-c',net[dst].IP(),'-u','-b',f'{client_rate}M','-t',str(duration),
                         '--connect-timeout',str(CONNECT_SECONDS*1000),'-J']
                started=time.monotonic()
                process=net[src].popen(command,stdout=stdout,stderr=stderr)
                client=dict(source=src,destination=dst,rate_mbps=client_rate,pid=process.pid,command=process.args,
                    started=time.time(),started_monotonic=started,
                    deadline_monotonic=started+CONNECT_SECONDS+duration+RESULT_SECONDS,
                    stdout=Path(stdout.name).name,stderr=Path(stderr.name).name)
                clients.append((process,client));stage['clients'].append(client)
            until=max(client['started_monotonic']+duration for _,client in clients)
            while time.monotonic()<until:
                observe()
                time.sleep(min(.5,max(0,until-time.monotonic())))
            for process,client in clients:
                try:
                    code=process.wait(timeout=max(0,client['deadline_monotonic']-time.monotonic()))
                    client.update(status='passed' if code==0 else 'failed',exit_code=code)
                except subprocess.TimeoutExpired:
                    client['status']='timed_out'
                    client['diagnostics']=client_diagnostics(net,args,process,client['source'],client['destination'])
                    print(f"*** iperf3 timeout: {client['source']} -> {client['destination']} "
                          f"pid={process.pid}; see traffic-stages.json and {client['stderr']}",flush=True)
        except BaseException as error:
            stage['error']=f'{type(error).__name__}: {error}'
            raise
        finally:
            for process,client in clients:
                if 'status' not in client:client['status']='aborted'
                client['cleanup']=stop_process(process)
                client['exit_code']=process.poll()
                client['elapsed_seconds']=time.monotonic()-client['started_monotonic']
                if not client['cleanup']['cleaned_up']:client['status']='cleanup_failed'
            # Flush our handles before reading; SIGINT lets iperf flush its own.
            for handle in handles:handle.flush()
            for _,client in clients:
                read_client_measurements(args.output,client,duration)
            stage.update(finished=time.time(),client_exit_codes=[c['exit_code'] for _,c in clients],
                status='passed' if len(clients)==len(flows) and 'error' not in stage and
                    all(c['status']=='passed' for _,c in clients) else 'failed')


def route_for(state,src,dst):
    a,b=sorted((HOSTS[src]['mac'],HOSTS[dst]['mac']))
    return next((r for r in (state.get('snapshot') or {}).get('routes',[]) if r['source_mac']==a and r['destination_mac']==b and r['installed_route']),None)


def verify_path(net,args,route,require_alternative=True):
    installed=route['installed_route'];path=installed['path'];cookie=installed['cookie']
    if require_alternative:require(path!=route['baseline_path'],'ML route did not differ from baseline')
    src=int(route['source_mac'].split(':')[-1],16);dst=int(route['destination_mac'].split(':')[-1],16)
    require(ping(net,f'h{src}',f'h{dst}'),'Packet delivery failed on ML path')
    def positive_readback():
        state=read_routing_state(args.database);r=route_for(state,src,dst)
        if state.get('stale') or not r or r['installed_route']['cookie']!=cookie:return False
        observation=r.get('observed_route') or {}
        counts=[c for c in observation.get('counters',[]) if c['source_mac']==HOSTS[src]['mac']]
        return r if observation.get('cookie')==cookie and len(counts)==len(path) and all(c['packet_count']>0 for c in counts) else False
    end=time.monotonic()+12;verified=None
    while time.monotonic()<end:
        verified=positive_readback()
        if verified:break
        time.sleep(.25)
    require(bool(verified),'No positive per-hop OpenFlow readback on the applied path')
    # Inspect OVS independently of the persisted controller snapshot.
    dumps={}
    for dpid in path:
        output=command('ovs-ofctl','-O','OpenFlow13','dump-flows',f's{dpid}')
        lines=[line for line in output.splitlines() if f'cookie=0x{cookie:x},' in line and
            f"dl_src={HOSTS[src]['mac']}" in line and f"dl_dst={HOSTS[dst]['mac']}" in line]
        require(len(lines)==1 and re.search(r'n_packets=[1-9]\d*',lines[0]),f's{dpid} missing positive applied flow')
        dumps[f's{dpid}']=output
    return dict(route=verified,ovs_flow_dumps=dumps,delivery='ping_passed',
                verification='exact_flow_entries_and_positive_per_hop_counters_plus_host_delivery')


def verify_low_load_recovery(net,args):
    state=read_routing_state(args.database)
    link=next((l for l in (state.get('snapshot') or {}).get('topology',{}).get('links',[])
               if l['link_id']=='s1-s2'),{})
    measurement=link.get('measurement') or {};prediction=link.get('prediction') or {}
    require(not state.get('stale') and state.get('source')=='live_mininet' and link.get('fresh') and
            measurement.get('valid') and isinstance(measurement.get('utilization'),(int,float)) and
            0<=measurement['utilization']<.2 and prediction.get('status')=='valid' and
            isinstance(prediction.get('predicted_utilization'),(int,float)) and
            0<=prediction['predicted_utilization']<.8,
            'Low-load recovery was not observed with fresh measurements and forecasts')
    route=route_for(state,1,4);require(route is not None,'No installed route after congestion subsided')
    return dict(controller_snapshot=state,forwarding=verify_path(net,args,route,require_alternative=False),
                note='Hysteresis may retain the alternative; return to baseline is not forced.')


def checks(net,args):
    servers=[];pings=[];handles=[];proof=None;stages=[];down=False
    targeted=getattr(args,'scenario','full')=='targeted'
    try:
        eventually(lambda:ping(net,'h1','h4'),'initial bidirectional forwarding')
        require(net.pingAll(timeout='2')==0,'Initial pingall failed')
        for name in (('h2','h4') if targeted else ('h4','h6')):
            handle=(args.output/f'{name}-server.log').open('w');handles.append(handle)
            servers.append(net[name].popen(['iperf3','-s','-p','5201'],stdout=handle,stderr=subprocess.STDOUT))
        time.sleep(1);require(all(p.poll() is None for p in servers),'iperf3 server startup failed')
        for src,dst in (('h1','h4'),('h2','h5')):
            handle=(args.output/f'{src}-ping.log').open('w');handles.append(handle)
            process=net[src].popen(['ping','-n','-i','.2',net[dst].IP()],stdout=handle,stderr=subprocess.STDOUT)
            pings.append((src,process))
        # Several regimes exercise the saved model without assuming that any
        # fixed duration guarantees a congestion warning or a useful prediction.
        schedule=[(7,40),(45,20),(60,15),(70,15),(80,20),(95,35),(20,20),(7,20),(90,30),(95,35)]
        if targeted:schedule=[(7,30),(45,15),(70,15),(85,60),(7,35)]
        def observe():
            nonlocal proof
            state=read_routing_state(args.database)
            require(state.get('source')=='live_mininet','Expected real Mininet provenance')
            if proof is None and not state.get('stale'):
                for src,dst in (((1,4),) if targeted else ((1,4),(3,6))):
                    route=route_for(state,src,dst)
                    if route and route['installed_route']['reason']=='predicted_congestion':
                        proof=verify_path(net,args,route)
                        proof['controller_snapshot']=state
                        (args.output/'applied-ml-route.json').write_text(json.dumps(proof,indent=2))
                        print('*** VERIFIED actual ML-selected forwarding path',flush=True)
                        break
        for index,(rate,duration) in enumerate(schedule):
            stage=dict(index=index,rate_mbps=rate,duration_seconds=duration,started=time.time(),status='running')
            if targeted:stage['flows']=[('h1','h2',rate),('h1','h4',5)]
            stages.append(stage)
            (args.output/'traffic-stages.json').write_text(json.dumps(stages,indent=2))
            print(f"*** LIVE stage {index}: {stage.get('flows') or 'two UDP flows at '+str(rate)+'M'} for {duration}s",flush=True)
            try:run_stage(net,args,stage,observe)
            finally:(args.output/'traffic-stages.json').write_text(json.dumps(stages,indent=2))
            require(stage['status']=='passed',f'iperf3 traffic stage {index} failed; see traffic-stages.json')
        # End the loss observation before the intentional physical failure.
        for _,process in pings:
            require(stop_process(process)['cleaned_up'],'Continuous ping cleanup failed')
        for handle in handles:handle.flush()
        ping_results={}
        for src in ('h1','h2'):
            output=(args.output/f'{src}-ping.log').read_text()
            found=re.search(r'(\d+(?:\.\d+)?)% packet loss',output)
            require(found is not None,f'Missing ping summary for {src}')
            loss=float(found.group(1));ping_results[src]=dict(packet_loss_percent=loss,duplicates='DUP!' in output)
            require(loss<5 and 'DUP!' not in output,f'{src} experienced excessive loss or duplicate delivery')
        require(proof is not None,'No real RF-triggered, verified alternative occurred. Do not claim Phase 5 live acceptance.')
        recovery=None
        if targeted:
            recovery=verify_low_load_recovery(net,args)
            (args.output/'low-load-recovery.json').write_text(json.dumps(recovery,indent=2))
        else:
            net.configLinkStatus('s2','s5','down');down=True
            eventually(lambda:ping(net,'h2','h5'),'baseline recovery around failed backbone')
            net.configLinkStatus('s2','s5','up');down=False
            eventually(lambda:ping(net,'h2','h5'),'backbone restoration')
        require(net.pingAll(timeout='2')==0,'Final connectivity regression')
        return dict(source='live_mininet',scenario=getattr(args,'scenario','full'),applied_route=proof,
            continuous_ping=ping_results,backbone_failure_recovery=not targeted,low_load_recovery=recovery,final_pingall_loss=0,
            network_wide_atomic_update=False,zero_loss_guarantee=False)
    finally:
        cleanup=dict(processes=[],errors=[])
        if down:
            try:net.configLinkStatus('s2','s5','up')
            except Exception as error:cleanup['errors'].append(str(error))
        for process in servers+[p for _,p in pings]:
            cleanup['processes'].append(stop_process(process))
        for handle in handles:
            try:handle.close()
            except OSError as error:cleanup['errors'].append(str(error))
        cleanup['cleaned_up']=not cleanup['errors'] and all(p['cleaned_up'] for p in cleanup['processes'])
        (args.output/'cleanup.json').write_text(json.dumps(cleanup,indent=2))
        if not cleanup['cleaned_up'] and sys.exc_info()[0] is None:
            raise RuntimeError('Owned process cleanup failed; see cleanup.json')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-ip',default='127.0.0.1')
    parser.add_argument('--controller-port',type=int,default=16653)
    parser.add_argument('--bandwidth-scale',type=float,default=.1)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scenario',choices=('full','targeted'),default='full')
    args=parser.parse_args()
    if not shutil.which('iperf3'):parser.error('iperf3 is required; no packages will be installed')
    args.output.mkdir(parents=True,exist_ok=True);setLogLevel('info')
    result=dict(status='failed_live_mininet',source='live_mininet')
    def interrupted(signum,frame):
        # Let finally blocks finish even if the launcher receives another signal.
        signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise SystemExit(128+signum)
    previous={sig:signal.signal(sig,interrupted) for sig in (signal.SIGINT,signal.SIGTERM)}
    try:
        run_network(args,checks=lambda net:result.update(checks(net,args)))
        preflight(args.controller_ip,args.controller_port)
        result.update(status='passed_targeted_live_mininet' if args.scenario=='targeted' else 'passed_live_mininet',
                      scoped_cleanup_verified=True)
    except BaseException as error:
        result.update(status='failed_live_mininet',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        (args.output/'acceptance.json').write_text(json.dumps(result,indent=2))
        for sig,handler in previous.items():signal.signal(sig,handler)
    print('*** LIVE PHASE 5 ACCEPTANCE AND SCOPED CLEANUP PASSED',flush=True)


if __name__=='__main__':main()
