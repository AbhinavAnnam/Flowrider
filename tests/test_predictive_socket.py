"""Real OS-Ken TCP + simulated OF1.3 switches/dataplane, NOT live Mininet.

A tiny synthetic RF fixture makes the routing trigger deterministic. This is
transport/forwarding verification, not a measurement of prediction quality.
"""
import json
from pathlib import Path
import pickle
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from os_ken.lib.packet import ethernet,packet
from os_ken.ofproto import ofproto_v1_3 as ofp,ofproto_v1_3_parser as parser
from prediction import FEATURES
from routing_state import read_routing_state
from topology_config import HOSTS,wired_links,topology_definition
from test_openflow_socket import SwitchPeer


class Fabric:
    def __init__(self,profile='six'):
        self.peers={};self.lock=threading.RLock();self.deliveries=[];self.errors=[]
        hosts=topology_definition(profile)[1]
        self.access={(h['switch'],h['port']):h['mac'] for h in hosts.values()}
        self.ports={}
        for l in wired_links(profile):
            self.ports[l['src'],l['src_port']]=(l['dst'],l['dst_port'])
            self.ports[l['dst'],l['dst_port']]=(l['src'],l['src_port'])

    def forward(self,dpid,src,dst,ingress=1):
        with self.lock:
            tag=0;path=[]
            for _ in range(12):
                if dpid in path:self.errors.append('forwarding loop');return None
                path.append(dpid)
                candidates=[r for r in self.peers[dpid].rules.values() if r['priority']==20 and
                    all(r['match'].get(k,v)==v for k,v in dict(in_port=ingress,eth_src=src,eth_dst=dst,vlan_vid=tag).items())]
                guards=[r for r in self.peers[dpid].rules.values() if r['priority']==40 and all(r['match'].get(k)==v for k,v in dict(in_port=ingress,eth_src=src,eth_dst=dst,vlan_vid=tag).items())]
                if not candidates or guards:return None
                rule=candidates[0];rule['packets']+=1;out=None
                for action in rule['actions']:
                    if isinstance(action,parser.OFPActionPushVlan):tag=0x1000
                    elif isinstance(action,parser.OFPActionSetField):tag=action.value
                    elif isinstance(action,parser.OFPActionPopVlan):tag=0
                    elif isinstance(action,parser.OFPActionOutput):out=action.port
                if (dpid,out) in self.access:
                    if tag or self.access[dpid,out]!=dst:self.errors.append('wrong host/tag leak');return None
                    self.deliveries.append(path);return path
                if (dpid,out) not in self.ports:return None
                dpid,ingress=self.ports[dpid,out]
            self.errors.append('hop limit');return None


class RoutingPeer(SwitchPeer):
    def __init__(self,dpid,port,fabric):
        self.fabric=fabric;self.rules={};self.origin=time.monotonic()
        fabric.peers[dpid]=self
        super().__init__(dpid,port)

    def flow_mod(self,body,xid):
        cookie,mask,table,command,idle,hard,priority,buffer,out,group,flags=struct.unpack_from('!QQBBHHHIIIH2x',body)
        match=parser.OFPMatch.parser(body,40)
        fields=dict(match.items());offset=40+(match.length+7)//8*8;instructions=[]
        while offset<len(body):
            inst=parser.OFPInstruction.parser(body,offset);instructions.append(inst);offset+=inst.len
        with self.fabric.lock:
            if command==ofp.OFPFC_DELETE:
                self.rules={k:r for k,r in self.rules.items() if (r['cookie']&mask)!=(cookie&mask) or not all(r['match'].get(k)==v for k,v in fields.items())}
            elif command==ofp.OFPFC_ADD:
                key=(priority,tuple(sorted(fields.items())))
                self.rules[key]=dict(cookie=cookie,match=fields,priority=priority,idle=idle,hard=hard,
                    instructions=instructions,actions=[a for i in instructions for a in i.actions],packets=0)

    def packet_out(self,body,xid):
        buffer,ingress,length=struct.unpack_from('!IIH',body)
        actions=[];offset=16
        while offset<16+length:
            action=parser.OFPAction.parser(body,offset);actions.append(action);offset+=action.len
        if any(isinstance(a,parser.OFPActionOutput) and a.port==ofp.OFPP_TABLE for a in actions):
            eth=packet.Packet(body[16+length:]).get_protocol(ethernet.ethernet)
            self.fabric.forward(self.dpid,eth.src,eth.dst,ingress)
        else:super().packet_out(body,xid)

    def multipart_request(self,subtype,body,xid):
        if subtype==ofp.OFPMP_FLOW:
            _,_,_,cookie,mask=struct.unpack_from(ofp.OFP_FLOW_STATS_REQUEST_0_PACK_STR,body,8)
            entries=[]
            with self.fabric.lock:
                for rule in self.rules.values():
                    if rule['cookie']&mask!=cookie&mask:continue
                    buf=bytearray(48);match=parser.OFPMatch(**rule['match']);match.serialize(buf,48)
                    offset=48+(match.length+7)//8*8
                    for inst in rule['instructions']:inst.serialize(buf,offset);offset+=inst.len
                    struct.pack_into(ofp.OFP_FLOW_STATS_0_PACK_STR,buf,0,len(buf),0,0,0,rule['priority'],rule['idle'],rule['hard'],0,rule['cookie'],rule['packets'],rule['packets']*60)
                    entries.append(bytes(buf))
            self.send(ofp.OFPT_MULTIPART_REPLY,struct.pack('!HH4x',subtype,0)+b''.join(entries),xid)
        elif subtype==ofp.OFPMP_PORT_STATS:
            elapsed=time.monotonic()-self.origin;stats=[]
            ports={1}|{p for d,p in self.fabric.ports if d==self.dpid}
            for port in sorted(ports):
                peer=self.fabric.ports.get((self.dpid,port));link={self.dpid,peer[0]} if peer else set()
                capacity=200 if link=={2,5} else 100
                utilization=.95 if link in ({1,2},{2,5}) else .05
                count=int(elapsed*capacity*utilization*1e6/8)
                stats.append(struct.pack(ofp.OFP_PORT_STATS_PACK_STR,port,100,100,count,count,0,0,0,0,0,0,0,0,int(elapsed),int(elapsed%1*1e9)))
            self.send(ofp.OFPT_MULTIPART_REPLY,struct.pack('!HH4x',subtype,0)+b''.join(stats),xid)


class PredictiveSocketTests(unittest.TestCase):
    def test_automatic_counter_rf_dijkstra_flowmod_readback_and_packet_delivery(self):
        import sklearn
        from sklearn.ensemble import RandomForestRegressor
        with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);db=root/'state.sqlite';model=root/'fixture.pkl'
            # Deterministic simulated fixture; never saved over the real model.
            low=[.05]*5+[0,0,5,5,100];high=[.95]*5+[0,0,95,95,100]
            forest=RandomForestRegressor(n_estimators=4,random_state=42,max_features=1.).fit([low]*8+[high]*8,[.05]*8+[.95]*8)
            metadata=dict(source='simulated_openflow',schema_version=1,features=list(FEATURES),horizon_seconds=15,
                sklearn_version=sklearn.__version__,threshold=.8,model_version='simulated-routing-test')
            with model.open('wb') as handle:pickle.dump(dict(model=forest,metadata=metadata),handle)
            fabric=Fabric();peers=[]
            with tempfile.TemporaryFile(mode='w+') as log:
                process=subprocess.Popen([sys.executable,'-u','controller.py','--ofp-listen-host','127.0.0.1',
                    '--ofp-tcp-listen-port',str(port),'--telemetry-enabled','--telemetry-source','simulated_openflow',
                    '--telemetry-db',str(db),'--telemetry-model',str(model),'--routing-mode','predictive_ml',
                    '--routing-cooldown','0'],stdout=log,stderr=subprocess.STDOUT)
                def wait(check,message,seconds=10):
                    end=time.monotonic()+seconds
                    while time.monotonic()<end:
                        result=check()
                        if result:return result
                        if process.poll() is not None:break
                        time.sleep(.08)
                    log.seek(0);self.fail(message+'\n'+log.read())
                def active(a=1,b=4):
                    state=read_routing_state(db)
                    return next((r for r in (state.get('snapshot') or {}).get('routes',[]) if r['source_mac']==HOSTS[a]['mac'] and r['destination_mac']==HOSTS[b]['mac'] and r['installed_route']),None)
                try:
                    deadline=time.monotonic()+7
                    while True:
                        try:peers=[RoutingPeer(1,port,fabric)];break
                        except ConnectionRefusedError:
                            if time.monotonic()>deadline:raise
                            time.sleep(.05)
                    peers += [RoutingPeer(i,port,fabric) for i in range(2,7)]
                    self.assertTrue(all(p.ready.wait(5) for p in peers));time.sleep(.2)
                    peers[0].inject(1,4)
                    route=wait(active,'No verified baseline route')
                    self.assertEqual(route['installed_route']['path'],[1,2,5,4])
                    self.assertEqual(fabric.forward(1,HOSTS[1]['mac'],HOSTS[4]['mac']),[1,2,5,4])
                    peers[1].inject(2,3);other=wait(lambda:active(2,3),'Unrelated route missing')['installed_route']['cookie']
                    route=wait(lambda: (r if (r:=active()) and r['installed_route']['reason']=='predicted_congestion' else None),
                        'RF forecasts did not automatically activate an alternative',seconds=38)
                    self.assertEqual(route['installed_route']['path'],[1,6,5,4])
                    self.assertEqual(fabric.forward(1,HOSTS[1]['mac'],HOSTS[4]['mac']),[1,6,5,4])
                    self.assertEqual(fabric.forward(4,HOSTS[4]['mac'],HOSTS[1]['mac']),[4,5,6,1])
                    self.assertEqual(active(2,3)['installed_route']['cookie'],other)
                    self.assertEqual(fabric.forward(2,HOSTS[2]['mac'],HOSTS[3]['mac']),[2,3])
                    model.unlink()
                    route=wait(lambda: (r if (r:=active()) and 'fallback' in r['installed_route']['reason'] else None),
                        'Model-unavailable baseline fallback missing',seconds=12)
                    self.assertEqual(route['installed_route']['path'],[1,2,5,4])
                    self.assertEqual(fabric.forward(1,HOSTS[1]['mac'],HOSTS[4]['mac']),[1,2,5,4])
                    self.assertFalse(fabric.errors)
                    self.assertTrue(all(not p.errors for p in peers))
                    self.assertTrue(fabric.deliveries)
                finally:
                    for p in peers:p.close()
                    process.terminate()
                    try:process.wait(5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(5)
                    log.seek(0);output=log.read()
                    self.assertNotIn('Traceback',output,output)
                    self.assertNotIn('ERROR:',output,output)
