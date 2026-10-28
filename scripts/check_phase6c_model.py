"""Frozen RF feature compatibility and synthetic sensitivity, never performance data."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from phase6c import write_json
from prediction import FEATURES, Predictor, feature_for, load_model


def diagnose(model):
    bundle=load_model(model);predictor=Predictor(bundle);probes=[]
    now=datetime(2026,10,28,tzinfo=timezone.utc)
    for capacity in (20,100):
        for regime,values in [('idle',[.02]*4),('low',[.1]*4),('rising',[.1,.3,.6,.95]),
                              ('saturated',[1.]*4),('recovery',[1.,.6,.3,.1])]:
            history=[dict(link_id='s2-s4' if capacity==20 else 's1-s3',session_id='synthetic-compatibility-only',
                source='live_mininet',valid=True,link_active=True,status='valid',
                observation_timestamp=(now+timedelta(seconds=i*5)).isoformat(),monotonic_elapsed_time=i*5,
                capacity_mbps=capacity,utilization=u,tx_mbps=u*capacity,rx_mbps=0)
                for i,u in enumerate(values)]
            prediction=predictor.predict(history)
            if prediction['status']!='valid':raise ValueError(f'Frozen model rejected topology features: {prediction}')
            probes.append(dict(capacity_mbps=capacity,regime=regime,features=feature_for(history),
                predicted_utilization=prediction['predicted_utilization'],
                congestion_predicted=prediction['congestion_predicted']))
    ranges=None
    feature_file=Path(model).with_name('features.jsonl')
    if feature_file.is_file():
        training=[json.loads(line) for line in feature_file.read_text().splitlines()]
        training=[r for r in training if r.get('session_id') in bundle['metadata'].get('train_sessions',[])]
        if training:
            ranges={feature:dict(minimum=min(r['features'][i] for r in training),
                                maximum=max(r['features'][i] for r in training)) for i,feature in enumerate(FEATURES)}
    if ranges:
        for probe in probes:
            probe['outside_training_features']=[feature for feature,value in zip(FEATURES,probe['features'])
                if not ranges[feature]['minimum']<=value<=ranges[feature]['maximum']]
    return dict(status='schema_compatible_generalization_unverified',source='synthetic_diagnostic_not_live_measurements',
        model_sha256=hashlib.sha256(Path(model).read_bytes()).hexdigest(),metadata=bundle['metadata'],
        training_feature_ranges=ranges,probes=probes,
        explanation='No switch IDs or topology size are model features. Utilization uses actual physical Mbps. '
          'Capacity and absolute TX/RX Mbps are features, so changing capacity can shift the input distribution. '
          'Finite predictions establish schema compatibility only. The unchanged 80% congestion gate, '
          'two confirmations and 30s cooldown may prevent rerouting even when dynamic Dijkstra recommends a detour. '
          'Live target-matched accuracy and verified route activations decide generalization; no retraining or reactive substitution.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',type=Path,default=ROOT/'data/research-20261015T180515Z/evaluation/model.pkl')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Refusing to overwrite diagnostics')
    write_json(args.output,diagnose(args.model))
    print(f'Frozen-model compatibility diagnostics: {args.output}')


if __name__=='__main__':main()
