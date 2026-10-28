"""Verified static/predictive routing. All methods run on the OS-Ken app queue.

Two-version VLAN forwarding stages transit/egress entries before host ingress
cutover. Barriers order operations; exact flow-stat readback verifies entries.
This is per-packet versioning, not an atomic network-wide transaction.
"""
from collections import deque
from dataclasses import asdict, dataclass
import math
import time

from network_topology import calculate_shortest_path
from prediction import timestamp
from telemetry_store import utc_now

OWNED_COOKIE = 0x4652000000000000
EXACT_MASK = 0xffffffffffffffff


@dataclass(frozen=True)
class RoutingPolicy:
    min_improvement: float = .10
    cooldown: float = 30
    confirmations: int = 2
    prediction_max_age: float = 10
    operation_timeout: float = 3
    drain_seconds: float = 1
    audit_interval: float = 5
    max_pairs: int = 64

    def __post_init__(self):
        values=asdict(self)
        if any(not math.isfinite(v) or v<=0 for k,v in values.items() if k!='cooldown') or not math.isfinite(self.cooldown) or self.cooldown<0:
            raise ValueError('Routing policy values must be finite and positive; cooldown may be zero')
        if not 0<self.min_improvement<1 or self.confirmations!=int(self.confirmations) or self.max_pairs!=int(self.max_pairs):
            raise ValueError('Improvement must be in (0,1); confirmations/max_pairs must be integers')


class RoutingManager:
    def __init__(self,topology,datapaths,policy=None,clock=time.monotonic,record=None,
                 release=None,source='live_mininet',scale=.1,routing_mode='predictive_ml',trial_id=None):
        if routing_mode not in ('baseline','predictive_ml'):raise ValueError('Unknown routing mode')
        self.routing_mode,self.trial_id=routing_mode,trial_id
        self.topology,self.datapaths=topology,datapaths
        self.policy,self.clock=policy or RoutingPolicy(),clock
        self.record,self.release=record or (lambda *a:None),release or (lambda *a:None)
        self.source,self.scale=source,scale
        self.epoch=0;self.pairs={};self.requests={};self.commands={}
        self.command_order=deque();self.retired={};self.vlans=set();self.serial=0
        self.measurements={};self.predictions={};self.telemetry_time=None
        self.updated=utc_now();self.topology_updated=self.updated
        self.event_sequence=0

    @staticmethod
    def key(a,b):return tuple(sorted((a,b)))

    def _cookie(self):
        self.serial+=1
        return OWNED_COOKIE|self.serial

    def _event(self,kind,pair=None,**fields):
        self.updated=utc_now();self.event_sequence+=1
        row=dict(kind=kind,timestamp=self.updated,sequence=self.event_sequence,topology_epoch=self.epoch,
                 source=self.source,routing_mode=self.routing_mode,trial_id=self.trial_id,
                 topology_version=self.topology.topology_version,route_id=pair['route_id'] if pair else None,**fields)
        try:self.record('event',row)
        except Exception:pass  # Recording failure cannot alter forwarding state.
        self.publish()

    def publish(self):
        try:self.record('snapshot',self.snapshot())
        except Exception:pass

    def _new_pair(self,a,b):
        key=self.key(a,b)
        pair=dict(key=key,route_id='pair:'+':'.join(key),installed=None,observed=None,
                  recommended=None,baseline=None,transaction=None,versions=[],guard_cookie=self._cookie(),
                  guarded=False,status='uninstalled',last_change=-math.inf,candidate=None,
                  count=0,packets=deque(maxlen=16),retry_at=0,last_audit=0)
        self.pairs[key]=pair
        return pair

    def _baseline(self,pair):
        source,target=(self.topology.hosts.get(m) for m in pair['key'])
        if not source or not target or not all(self.topology.port_up(*p) for p in (source,target)):return None
        return self.topology.path(source[0],target[0])

    def _valid_path(self,pair,path):
        baseline=self._baseline(pair)
        if not baseline or not path or path[0]!=baseline[0] or path[-1]!=baseline[-1] or len(path)!=len(set(path)):return False
        graph=self.topology.active_graph()
        return all(n in self.datapaths and n in graph for n in path) and all(graph.has_edge(a,b) for a,b in zip(path,path[1:]))

    def _rules(self,pair,path,cookie,vlan):
        rules=[]
        for src,dst,nodes in ((pair['key'][0],pair['key'][1],path),(pair['key'][1],pair['key'][0],list(reversed(path)))):
            for i,dpid in enumerate(nodes):
                ingress=self.topology.hosts[src][1] if i==0 else self.topology.graph[dpid][nodes[i-1]]['ports'][dpid]
                out=self.topology.hosts[dst][1] if i==len(nodes)-1 else self.topology.graph[dpid][nodes[i+1]]['ports'][dpid]
                # A route is a bidirectional unit: UDP can keep one direction
                # busy while its TCP control/reverse rules are idle for >30s.
                # Bound lifetime with the existing hard timeout and refresh it
                # as a whole in tick(), rather than expiring individual hops.
                rules.append(dict(dpid=dpid,cookie=cookie,priority=20,idle_timeout=0,hard_timeout=120,
                    match=dict(in_port=ingress,eth_src=src,eth_dst=dst,vlan_vid=0 if i==0 else 0x1000|vlan),
                    out_port=out,ingress=i==0,push=i==0 and len(nodes)>1,pop=i>0 and i==len(nodes)-1,vlan=vlan))
        return rules

    def _guards(self,pair):
        return [dict(dpid=self.topology.hosts[src][0],cookie=pair['guard_cookie'],priority=40,
                    idle_timeout=0,hard_timeout=0,match=dict(in_port=self.topology.hosts[src][1],
                    eth_src=src,eth_dst=dst,vlan_vid=0),out_port=None,push=False,pop=False)
                for src,dst in (pair['key'],tuple(reversed(pair['key']))) if src in self.topology.hosts]

    @staticmethod
    def actions(rule,dp):
        p=dp.ofproto_parser;actions=[]
        if rule['push']:actions += [p.OFPActionPushVlan(),p.OFPActionSetField(vlan_vid=0x1000|rule['vlan'])]
        if rule['pop']:actions.append(p.OFPActionPopVlan())
        if rule['out_port'] is not None:actions.append(p.OFPActionOutput(rule['out_port']))
        return actions

    def ensure(self,a,b,packet=None):
        key=self.key(a,b)
        if a==b or a not in self.topology.hosts or b not in self.topology.hosts:return False
        if key not in self.pairs and len(self.pairs)>=self.policy.max_pairs:return False
        pair=self.pairs.get(key) or self._new_pair(a,b)
        if packet is not None:pair['packets'].append(packet)
        pair['baseline']=self._baseline(pair)
        if not pair['baseline']:return False
        if pair['transaction'] or self.clock()<pair['retry_at']:return True
        if pair['status']=='quarantined':self._recover(pair,'retry_cleanup');return True
        if not pair['installed']:self._begin(pair,pair['baseline'],'initial_baseline')
        elif packet is not None:
            # A valid host PacketIn on an allegedly installed route is evidence
            # of an expired/missing ingress rule, not permission to bypass it.
            self._recover(pair,'unexpected_packet_in')
        return True

    def _begin(self,pair,path,reason):
        if not self._valid_path(pair,path):return False
        vlan=next((n for n in range(1,4095) if n not in self.vlans),None)
        if vlan is None:self._event('installation_rejected',pair,reason='vlan_pool_exhausted');return False
        self.vlans.add(vlan)
        version=dict(path=list(path),cookie=self._cookie(),vlan=vlan,reason=reason,
                     requested_timestamp=utc_now(),rules=[])
        version['rules']=self._rules(pair,path,version['cookie'],vlan)
        pair['versions'].append(version)
        context=dict(type='install',pair=pair,version=version,old=pair['installed'],phase='stage',pending=set())
        pair['transaction']=context;pair['status']='staging'
        self._event('installation_started',pair,path=path,cookie=version['cookie'],vlan=vlan,reason=reason)
        self._add(context,[r for r in version['rules'] if not r['ingress']])
        if pair['transaction'] is context:self._barriers(context,'stage_barrier',{r['dpid'] for r in version['rules'] if not r['ingress']})
        return True

    def _send(self,context,dp,message):
        dp.set_xid(message);key=(dp.id,message.xid)
        self.commands[key]=(dp,context);self.command_order.append(key)
        while len(self.command_order)>4096:self.commands.pop(self.command_order.popleft(),None)
        dp.send_msg(message)

    def _add(self,context,rules):
        try:
            for rule in rules:
                dp=self.datapaths[rule['dpid']];p,o=dp.ofproto_parser,dp.ofproto
                self._send(context,dp,p.OFPFlowMod(datapath=dp,cookie=rule['cookie'],priority=rule['priority'],
                    idle_timeout=rule['idle_timeout'],hard_timeout=rule['hard_timeout'],flags=o.OFPFF_SEND_FLOW_REM,
                    match=p.OFPMatch(**rule['match']),instructions=[p.OFPInstructionActions(o.OFPIT_APPLY_ACTIONS,self.actions(rule,dp))]))
        except Exception:self._fail(context,'flowmod_send_failed')

    def _delete(self,context,versions):
        try:
            for version in versions:
                for dpid in {r['dpid'] for r in version['rules']}:
                    dp=self.datapaths[dpid];p,o=dp.ofproto_parser,dp.ofproto
                    self._send(context,dp,p.OFPFlowMod(datapath=dp,cookie=version['cookie'],cookie_mask=EXACT_MASK,
                        table_id=0,command=o.OFPFC_DELETE,out_port=o.OFPP_ANY,out_group=o.OFPG_ANY,match=p.OFPMatch()))
        except Exception:self._fail(context,'delete_send_failed')

    def _wait(self,context,phase,kind,operations):
        context.update(phase=phase,pending=set(),deadline=self.clock()+self.policy.operation_timeout)
        messages=[]
        try:
            for dpid,cookie,expected in operations:
                dp=self.datapaths[dpid];p,o=dp.ofproto_parser,dp.ofproto
                msg=p.OFPBarrierRequest(dp) if kind=='barrier' else p.OFPFlowStatsRequest(dp,table_id=0,
                    cookie=cookie,cookie_mask=EXACT_MASK,match=p.OFPMatch())
                dp.set_xid(msg);key=(dpid,msg.xid)
                self.commands[key]=(dp,context);self.command_order.append(key)
                while len(self.command_order)>4096:self.commands.pop(self.command_order.popleft(),None)
                self.requests[key]=dict(context=context,dp=dp,kind=kind,expected=expected,rows=[])
                context['pending'].add(key);messages.append((dp,msg))
            for dp,msg in messages:dp.send_msg(msg)
        except Exception:self._fail(context,'request_send_failed');return
        if not messages:self._advance(context)

    def _barriers(self,context,phase,dpids):self._wait(context,phase,'barrier',[(d,None,[]) for d in sorted(dpids)])

    def _read(self,context,phase,version,rules=None):
        rules=version['rules'] if rules is None else rules
        self._wait(context,phase,'stats',[(d,version['cookie'],[r for r in rules if r['dpid']==d])
            for d in sorted({r['dpid'] for r in version['rules']})])

    def _cancel(self,context):
        for key in list(context.get('pending',())):self.requests.pop(key,None)
        context['pending']=set()

    def _request(self,msg,kind):
        key=(msg.datapath.id,msg.xid);request=self.requests.get(key)
        if not request or request['kind']!=kind or request['dp'] is not msg.datapath or self.datapaths.get(msg.datapath.id) is not msg.datapath:return None
        if self.clock()>request['context']['deadline']:
            self._fail(request['context'],'operation_timeout');return None
        return key,request

    def _ack(self,key,request):
        self.requests.pop(key,None);context=request['context'];context['pending'].discard(key)
        if not context['pending']:self._advance(context)

    def barrier_reply(self,msg):
        result=self._request(msg,'barrier')
        if not result:return False
        self._ack(*result);return True

    @staticmethod
    def _action_signature(instructions):
        # JSON contains derived lengths only after codec round-tripping. Compare
        # canonical wire encodings so identical SetField actions match readback.
        encoded=bytearray();offset=0
        for instruction in instructions:
            # OVS omits empty APPLY_ACTIONS on drop rules. This is equivalent
            # to no instructions; other instructions/actions remain exact.
            if instruction.type==4 and not instruction.actions:continue
            instruction.serialize(encoded,offset)
            offset+=instruction.len
        return bytes(encoded)

    def stats_reply(self,msg):
        result=self._request(msg,'stats')
        if not result:return False
        key,request=result;request['rows'].extend(msg.body)
        if len(request['rows'])>len(request['expected']):self._fail(request['context'],'unexpected_flow_entries');return True
        if msg.flags & msg.datapath.ofproto.OFPMPF_REPLY_MORE:return True
        rows=request['rows'];remaining=list(request['expected']);counters=[]
        for row in rows:
            matched=None
            for rule in remaining:
                dp=msg.datapath;p=dp.ofproto_parser
                expected=[p.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS,self.actions(rule,dp))]
                if (row.cookie==rule['cookie'] and row.priority==rule['priority'] and row.table_id==0 and
                    dict(row.match.items())==rule['match'] and row.idle_timeout==rule['idle_timeout'] and
                    row.hard_timeout==rule['hard_timeout'] and self._action_signature(row.instructions)==self._action_signature(expected)):
                    matched=rule;break
            if matched is None:
                self._event('flow_verification_mismatch',request['context']['pair'],
                    phase=request['context']['phase'],dpid=msg.datapath.id,
                    expected=request['expected'],observed=dict(cookie=row.cookie,priority=row.priority,
                    table_id=row.table_id,match=dict(row.match.items()),idle_timeout=row.idle_timeout,
                    hard_timeout=row.hard_timeout,instructions=[i.to_jsondict() for i in row.instructions]))
                self._fail(request['context'],'flow_readback_mismatch');return True
            remaining.remove(matched)
            counters.append(dict(dpid=msg.datapath.id,source_mac=matched['match']['eth_src'],
                packet_count=row.packet_count,byte_count=row.byte_count,out_port=matched['out_port']))
        if remaining:self._fail(request['context'],'missing_flow_entries');return True
        request['context'].setdefault('counters',[]).extend(counters)
        self._ack(key,request);return True

    def error(self,msg):
        key=(msg.datapath.id,getattr(msg,'xid',None))
        request=self.requests.get(key)
        known=self.commands.get(key)
        if request and request['dp'] is msg.datapath:self._fail(request['context'],'openflow_error');return True
        if known and known[0] is msg.datapath:
            context=known[1]
            if self._current_context(context):self._fail(context,'openflow_error')
            return True
        return False

    def _current_context(self,context):
        return self.retired.get(context.get('cookie')) is context if context['type']=='retire' else context['pair']['transaction'] is context

    def _advance(self,context):
        pair=context['pair'];phase=context['phase']
        if context['type']=='retire':
            if phase=='retire_barrier':self._read(context,'retire_read',context['version'],[])
            else:
                version=context['version'];self.vlans.discard(version['vlan'])
                if version in pair['versions']:pair['versions'].remove(version)
                self.retired.pop(version['cookie'],None);self._event('obsolete_rules_removed',pair,cookie=version['cookie'])
            return
        if phase=='stage_barrier':
            version=context['version'];self._read(context,'stage_read',version,[r for r in version['rules'] if not r['ingress']])
        elif phase=='stage_read':
            if not self._valid_path(pair,context['version']['path']):self._fail(context,'path_changed');return
            if context['version']['reason']=='predicted_congestion' and self._weighted() is None:
                self._fail(context,'predictions_expired_before_cutover');return
            pair['installed']=pair['observed']=None;pair['status']='cutover'
            self._add(context,[r for r in context['version']['rules'] if r['ingress']])
            if pair['transaction'] is context:self._barriers(context,'activate_barrier',{r['dpid'] for r in context['version']['rules'] if r['ingress']})
        elif phase=='activate_barrier':
            context['counters']=[];self._read(context,'activate_read',context['version'])
        elif phase=='activate_read':
            if pair['guarded']:self._release_guards(context,'release')
            else:self._activated(context)
        elif phase=='release_barrier':self._read(context,'release_read',dict(cookie=pair['guard_cookie'],rules=self._guards(pair)),[])
        elif phase=='release_read':pair['guarded']=False;self._activated(context)
        elif phase=='guard_barrier':self._read(context,'guard_read',dict(cookie=pair['guard_cookie'],rules=self._guards(pair)))
        elif phase=='guard_read':
            pair['guarded']=True
            versions=pair['versions'];self._delete(context,versions)
            if pair['transaction'] is context:self._barriers(context,'cleanup_barrier',{r['dpid'] for v in versions for r in v['rules']})
        elif phase=='cleanup_barrier':
            self._wait(context,'cleanup_read','stats',[(d,v['cookie'],[]) for v in pair['versions'] for d in sorted({r['dpid'] for r in v['rules']})])
        elif phase=='cleanup_read':
            for version in pair['versions']:self.vlans.discard(version['vlan'])
            pair['versions']=[];pair['transaction']=None
            baseline=self._baseline(pair)
            if context.get('idle') and not pair['packets'] or not baseline:
                pair['transaction']=context;self._release_guards(context,'idle')
            else:
                path=context.get('repair_path')
                self._begin(pair,path if path and self._valid_path(pair,path) else baseline,
                            'fallback_after_'+context['reason'])
        elif phase=='idle_barrier':self._read(context,'idle_read',dict(cookie=pair['guard_cookie'],rules=self._guards(pair)),[])
        elif phase=='idle_read':
            pair.update(transaction=None,guarded=False,status='uninstalled',retry_at=self.clock()+1)
            self._event('route_deactivated',pair,reason=context['reason'])
        elif phase=='audit_read':
            self._observed(pair,context['version'],context.get('counters',[]));pair['transaction']=None
            self.publish()

    def _release_guards(self,context,prefix):
        pair=context['pair'];guards=self._guards(pair)
        self._delete(context,[dict(cookie=pair['guard_cookie'],rules=guards)])
        if pair['transaction'] is context:self._barriers(context,prefix+'_barrier',{r['dpid'] for r in guards})

    def _observed(self,pair,version,counters):
        pair['observed']=dict(path=version['path'],cookie=version['cookie'],method='openflow_flow_stats',
            verified_timestamp=utc_now(),counters=counters,end_to_end_delivery_verified=False,
            all_rule_counters_positive=bool(counters) and all(r['packet_count']>0 for r in counters))
        pair['last_audit']=self.clock()

    def _activated(self,context):
        pair=context['pair'];version=context['version'];old=context.get('old')
        version.update(activated_timestamp=utc_now(),activated_monotonic=self.clock())
        # Refresh changes a rule version, not the selected route. Only a real
        # installation/path change should restart the routing cooldown.
        last_change=(pair['last_change'] if old and old['path']==version['path'] and
                     version['reason']=='refresh_hard_timeout' else self.clock())
        pair.update(installed=version,status='active',transaction=None,last_change=last_change,candidate=None,count=0)
        self._observed(pair,version,context.get('counters',[]))
        if old:self.retired[old['cookie']]=dict(type='retire',pair=pair,version=old,cookie=old['cookie'],
            phase='queued',due=self.clock()+self.policy.drain_seconds,pending=set())
        self._event('route_activated',pair,path=version['path'],previous_path=old['path'] if old else None,
                    cookie=version['cookie'],vlan=version['vlan'],reason=version['reason'],verification='openflow_flow_stats')
        packets=list(pair['packets']);pair['packets'].clear()
        for packet in packets:
            try:self.release(packet)
            except Exception:self._event('packet_release_failed',pair)

    def _fail(self,context,reason):
        if not self._current_context(context):return
        self._cancel(context);pair=context['pair']
        if context['type']=='retire':
            context.update(phase='queued',due=self.clock()+5);self._event('cleanup_retry',pair,reason=reason);return
        if context['phase'].startswith(('guard','cleanup','release','idle')):
            pair.update(installed=None,observed=None,transaction=None,status='quarantined',retry_at=self.clock()+5)
            self._event('route_quarantined',pair,reason=reason);return
        self._event('installation_failed',pair,reason=reason,phase=context['phase'])
        self._recover(pair,reason)

    def _recover(self,pair,reason,idle=False):
        installed=pair['installed']
        repair_path=(list(installed['path']) if installed and reason in
            ('flow_expired','missing_transit_rule','unexpected_packet_in','missing_flow_entries') else None)
        if pair['transaction']:self._cancel(pair['transaction'])
        for cookie,ctx in list(self.retired.items()):
            if ctx['pair'] is pair:self._cancel(ctx);del self.retired[cookie]
        pair.update(installed=None,observed=None,status='recovering',candidate=None,count=0)
        context=dict(type='recover',pair=pair,phase='guard',reason=reason,idle=idle,
                     repair_path=repair_path,pending=set())
        pair['transaction']=context
        self._event('baseline_recovery',pair,reason=reason)
        self._add(context,self._guards(pair))
        if pair['transaction'] is context:self._barriers(context,'guard_barrier',{r['dpid'] for r in self._guards(pair)})

    def flow_removed(self,msg):
        for pair in self.pairs.values():
            installed=pair['installed']
            if installed and installed['cookie']==msg.cookie:
                self._recover(pair,'flow_expired',idle=msg.reason==msg.datapath.ofproto.OFPRR_IDLE_TIMEOUT);return True
        return False

    def transit_miss(self,a,b,vlan):
        pair=self.pairs.get(self.key(a,b))
        if pair and pair['installed'] and pair['installed']['vlan']==vlan:
            self._recover(pair,'missing_transit_rule')

    def topology_changed(self):
        # The controller immediately follows this with its established owned-
        # cookie delete + all-switch barriers. No route remains claimed active.
        self.epoch+=1;self.topology_updated=utc_now()
        self.requests.clear();self.retired.clear();self.predictions={};self.measurements={};self.telemetry_time=None
        self.vlans.clear()
        for pair in self.pairs.values():
            pair.update(installed=None,observed=None,transaction=None,versions=[],guarded=False,status='topology_invalidated',candidate=None,count=0)
            pair['recommended']=None;pair['retry_at']=0
            pair['packets'].clear();pair['baseline']=self._baseline(pair)
        self._event('topology_changed',connected_switches=sorted(self.topology.switches))

    def telemetry(self,frame):
        if frame.get('epoch')!=self.epoch or frame.get('source')!=self.source:return False
        now=frame.get('monotonic_time')
        if not isinstance(now,(int,float)) or not math.isfinite(now) or not 0<=self.clock()-now<=self.policy.prediction_max_age:return False
        if self.telemetry_time is not None and now<=self.telemetry_time:return False
        self.telemetry_time=now
        self.measurements={r['link_id']:dict(r) for r in frame['observations']}
        self.predictions={r['link_id']:dict(r) for r in frame['predictions']}
        self._evaluate();self.publish();return True

    def _weighted(self):
        if self.routing_mode=='baseline':
            graph=self.topology.active_graph()
            for _,_,edge in graph.edges(data=True):edge['ml_weight']=edge['base_delay']
            return graph
        if self.telemetry_time is None or not 0<=self.clock()-self.telemetry_time<=self.policy.prediction_max_age:return None
        graph=self.topology.active_graph()
        versions=set()
        for u,v,edge in graph.edges(data=True):
            link=f's{min(u,v)}-s{max(u,v)}';p=self.predictions.get(link,{});o=self.measurements.get(link,{})
            value=p.get('predicted_utilization')
            try:
                age=(timestamp(utc_now())-timestamp(p['observation_timestamp'])).total_seconds()
                horizon=(timestamp(p['target_timestamp'])-timestamp(p['observation_timestamp'])).total_seconds()
                matching=(p['observation_timestamp']==o['observation_timestamp'] and p['session_id']==o['session_id'])
            except (ValueError,KeyError,TypeError):return None
            if (not o.get('valid') or not o.get('link_active') or p.get('status')!='valid' or not p.get('model_version') or
                p.get('source')!=self.source or o.get('source')!=self.source or not matching or
                not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1 or
                not 0<=age<=self.policy.prediction_max_age or horizon!=15 or p.get('prediction_horizon_seconds')!=15):return None
            versions.add((p['session_id'],p['model_version']))
            edge['ml_weight']=edge['base_delay']*(1+25*value**4)
        if len(versions)>1:return None
        return graph

    @staticmethod
    def _cost(graph,path):return sum(graph[u][v]['ml_weight'] for u,v in zip(path,path[1:]))

    def _evaluate(self):
        graph=self._weighted()
        for pair in list(self.pairs.values()):
            baseline=self._baseline(pair);pair['baseline']=baseline
            if not baseline:continue
            result=calculate_shortest_path(graph,baseline[0],baseline[-1],'ml_weight') if graph is not None else None
            path=result['path'] if result else baseline
            pair['recommended']=dict(path=path,policy='baseline' if self.routing_mode=='baseline' else
                'predicted' if result else 'baseline_fallback',timestamp=utc_now(),
                weighted_cost=self._cost(graph,path) if result else None)
            if self.routing_mode=='baseline':continue
            if pair['transaction'] or not pair['installed']:continue
            installed=pair['installed']['path']
            if graph is None:
                pair['candidate']=None;pair['count']=0
                if installed!=baseline:self._begin(pair,baseline,'fallback_unavailable_predictions')
                continue
            if installed==path or not self._valid_path(pair,path):pair['candidate']=None;pair['count']=0;continue
            if self.clock()-pair['last_change']<self.policy.cooldown:pair['candidate']=None;pair['count']=0;continue
            old_cost=self._cost(graph,installed);gain=(old_cost-self._cost(graph,path))/old_cost if old_cost>0 else 0
            congested=any(self.predictions[f's{min(u,v)}-s{max(u,v)}']['predicted_utilization']>=.8 for u,v in zip(installed,installed[1:]))
            if gain<self.policy.min_improvement or not congested:pair['candidate']=None;pair['count']=0;continue
            if pair['candidate']==path:pair['count']+=1
            else:pair['candidate']=path;pair['count']=1
            self._event('reroute_candidate',pair,path=path,relative_improvement=gain,confirmation_count=pair['count'])
            if pair['count']>=self.policy.confirmations:self._begin(pair,path,'predicted_congestion')

    def tick(self):
        for context in {id(r['context']):r['context'] for r in list(self.requests.values())}.values():
            if self.clock()>context['deadline']:self._fail(context,'operation_timeout')
        for context in list(self.retired.values()):
            if context['phase']=='queued' and self.clock()>=context['due']:
                context['phase']='retire_delete'
                self._delete(context,[context['version']])
                if context['phase']=='retire_delete':self._barriers(context,'retire_barrier',{r['dpid'] for r in context['version']['rules']})
        # Staleness fallback must also work if telemetry stops publishing entirely.
        if self._weighted() is None:
            for pair in self.pairs.values():
                baseline=self._baseline(pair)
                if pair['installed'] and not pair['transaction'] and baseline and pair['installed']['path']!=baseline:
                    self._begin(pair,baseline,'fallback_stale_predictions')
        for pair in list(self.pairs.values()):
            if pair['transaction']:continue
            if pair['status']=='quarantined' and self.clock()>=pair['retry_at']:
                self._recover(pair,'retry_cleanup');continue
            version=pair['installed']
            if not version:continue
            if self.clock()-version['activated_monotonic']>=100:
                self._begin(pair,version['path'],'refresh_hard_timeout');continue
            if self.clock()-pair['last_audit']>=self.policy.audit_interval:
                context=dict(type='audit',pair=pair,version=version,phase='audit',pending=set(),counters=[])
                pair['transaction']=context;self._read(context,'audit_read',version)
        self.publish()

    def snapshot(self):
        now=self.clock();links=[];weighted=self._weighted()
        for u,v,edge in sorted(self.topology.graph.edges(data=True)):
            link=f's{min(u,v)}-s{max(u,v)}';p=self.predictions.get(link);o=self.measurements.get(link)
            links.append(dict(link_id=link,source_dpid=u,destination_dpid=v,ports={str(d):port for d,port in edge['ports'].items()},
                active=edge['active'],capacity_mbps=edge['capacity']*self.scale,base_delay_ms=edge['base_delay'],
                routing_weight=weighted[u][v]['ml_weight'] if weighted is not None and weighted.has_edge(u,v) else None,
                measurement=o,prediction=p,fresh=self.telemetry_time is not None and 0<=now-self.telemetry_time<=self.policy.prediction_max_age))
        routes=[]
        for pair in self.pairs.values():
            installed=pair['installed'];tx=pair['transaction']
            public={k:installed[k] for k in ('path','cookie','vlan','reason','activated_timestamp')} if installed else None
            routes.append(dict(route_id=pair['route_id'],source_mac=pair['key'][0],destination_mac=pair['key'][1],
                status=pair['status'],baseline_path=pair['baseline'],recommended_route=pair['recommended'],
                installed_route=public,observed_route=pair['observed'],
                transaction_phase=tx['phase'] if tx else None,guarded=pair['guarded']))
        return dict(schema_version=1,timestamp=utc_now(),source=self.source,
            routing_mode=self.routing_mode,trial_id=self.trial_id,topology_version=self.topology.topology_version,
            automatic_rerouting=self.routing_mode=='predictive_ml',
            topology_epoch=self.epoch,topology_updated_timestamp=self.topology_updated,route_updated_timestamp=self.updated,
            policy=asdict(self.policy),topology=dict(connected_switches=sorted(self.topology.switches),
                switches=[dict(dpid=i,ports=[dict(port=p,up=up) for (d,p),up in sorted(self.topology.ports.items()) if d==i]) for i in sorted(self.topology.switches)],
                links=links,hosts=[dict(mac=m,dpid=a[0],port=a[1],active=self.topology.port_up(*a)) for m,a in sorted(self.topology.hosts.items())]),routes=routes)
