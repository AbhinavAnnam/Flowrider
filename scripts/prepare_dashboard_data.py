"""Prepare a portable dashboard data package outside original evidence. No lab runs."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dashboard_evidence import build_package


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',type=Path,required=True)
    parser.add_argument('--recovery',type=Path,help='Optional Phase 5 evidence directory with telemetry.sqlite')
    parser.add_argument('--output',type=Path,default=ROOT/'data/demo-dashboard/six-switch.json')
    args=parser.parse_args();output=args.output.resolve()
    for source in (args.experiment,args.recovery):
        if source and (output==source.resolve() or source.resolve() in output.parents):parser.error('Portable output must be outside original evidence')
    if output.exists():parser.error('Refusing to overwrite an existing portable package; choose a fresh output path')
    package=build_package(args.experiment,args.recovery)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as handle:json.dump(package,handle,allow_nan=False,separators=(',',':'))
    print(f'Prepared {output}: four revalidated trials; recovery {package["recovery"]["status"]}')


if __name__=='__main__':main()
