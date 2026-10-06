"""Local clock gate. An external scheduler must start this process in advance."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


def launch_state(now, release):
    if now < release - timedelta(minutes=30):
        return 'too_early'
    if now >= release + timedelta(minutes=30):
        return 'expired'
    return 'wait' if now < release else 'ready'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--window-id', type=int)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--submit', action='store_true', help='Explicitly enable order submission; never pays')
    args = parser.parse_args()
    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    plan = json.loads(plan_path.read_text())
    release = datetime.fromisoformat(plan['release_at'])
    if release.tzinfo is None:
        parser.error('release_at must include a time zone offset')
    state = launch_state(datetime.now(timezone.utc), release)
    if args.check:
        print(json.dumps({'release_at': release.isoformat(), 'state': state,
                          'max_total': plan['max_total'], 'purchase_started': False}))
        return 0
    if not args.window_id or args.window_id <= 0:
        parser.error('an observed, positive --window-id is required')
    if state in ('too_early', 'expired'):
        parser.error('outside the release window; no purchase started')
    if journal.exists() and json.loads(journal.read_text()).get('status') == 'alipay_reached':
        print('This journal already reached Alipay; no new action started.')
        return 0
    print(f'Waiting for {release.isoformat()}', flush=True)
    while (remaining := (release - datetime.now(timezone.utc)).total_seconds()) > 0:
        time.sleep(min(remaining, 0.1 if remaining < 2 else 1))
    if launch_state(datetime.now(timezone.utc), release) != 'ready':
        parser.error('release window expired while waiting')
    command = [sys.executable, str(Path(__file__).with_name('purchase_runner.py')),
               '--plan', str(plan_path), '--journal', str(journal), '--window-id', str(args.window_id)]
    if args.submit:
        command.append('--submit')
    return subprocess.call(command)


if __name__ == '__main__':
    sys.exit(main())
