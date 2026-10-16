"""Continuous Cua/Jev Apple runner. Default finish: order review, no submission."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import shutil
from dataclasses import dataclass
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

CLI = os.environ.get('CUA_DRIVER') or shutil.which('cua-driver') or str(Path.home() / '.local/bin/cua-driver')
ACTION_ROLES = {'AXButton', 'AXRadioButton', 'AXCheckBox', 'AXPopUpButton', 'AXLink'}

def norm(value):
    return ' '.join(str(value or '').split())

def semantic(label):
    return norm(label).split('RMB')[0].strip()

class Halt(RuntimeError):
    pass

@contextmanager
def journal_lock(path):
    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Halt('Another process is using this purchase journal') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)

@dataclass
class State:
    url: str
    title: str
    elements: list
    read_ms: int = 0
    complete: bool = True

    @property
    def text(self):
        return ' '.join(norm(e.get('label')) for e in self.elements)

    def controls(self, pattern):
        rx = re.compile(pattern)
        return [e for e in self.elements if e.get('enabled', True)
                and e.get('role') in ACTION_ROLES and rx.search(norm(e.get('label')))]

    def radio_selected(self, label):
        return any(e.get('role') == 'AXRadioButton' and semantic(e.get('label')) == semantic(label)
                   and str(e.get('value')) == '1' for e in self.elements)

    @property
    def needs_auth(self):
        return '/shop/signIn' in self.url or any(e.get('label') in ('密码', '验证码')
                                                and e.get('role') == 'AXTextField' for e in self.elements)

    @property
    def bag_ready(self):
        return '/shop/bag' in self.url and ('你的购物袋中没有商品' in self.text or
            any(e.get('role') == 'AXPopUpButton' and norm(e.get('label')) == '数量'
                for e in self.elements))

    @property
    def at_alipay(self):
        host = urlsplit(self.url if '://' in self.url else 'https://' + self.url).hostname or ''
        return host == 'alipay.com' or host.endswith('.alipay.com')

    @property
    def total(self):
        totals = set()
        labels = [norm(e.get('label')) for e in self.elements]
        for i, label in enumerate(labels):
            if '总计' not in label or '小计' in label:
                continue
            nearby = label if re.search(r'(?:RMB|¥|￥)\s*\d', label) else ' '.join(labels[i:i+3])
            match = re.search(r'(?:RMB|¥|￥)\s*([\d,]+(?:\.\d{1,2})?)', nearby)
            if match:
                totals.add(Decimal(match.group(1).replace(',', '')))
        return totals.pop() if len(totals) == 1 else None

class Cua:
    def __init__(self, session=None, window_id=None):
        self.session = session or f'jev-run-{os.getpid()}-{time.monotonic_ns()}'
        self.pid = self.window = None
        self.metrics = []
        self.requested_window = window_id
        self.address = None

    def call(self, name, data):
        start = time.perf_counter()
        proc = subprocess.run([CLI, 'call', name, json.dumps(data, ensure_ascii=False)],
                              capture_output=True, text=True, timeout=40)
        elapsed = round((time.perf_counter() - start) * 1000)
        self.metrics.append({'tool': name, 'ms': elapsed})
        if proc.returncode:
            raise Halt(f'Cua {name} failed; do not assume an action did not execute')
        result = json.loads(proc.stdout)
        if result.get('status') == 'refused' or result.get('isError') or result.get('error'):
            raise Halt(f'Cua {name} refused: {result.get("refusal", {}).get("code", "unknown")}')
        return result

    def bind(self):
        rows = self.call('list_windows', {})['windows']
        rows = [w for w in rows if w.get('app_name') == 'Google Chrome']
        if self.requested_window is not None:
            rows = [w for w in rows if w['window_id'] == self.requested_window]
        else:
            rows = [w for w in rows if w.get('title') and w.get('bounds', {}).get('width', 0) > 500
                    and w.get('bounds', {}).get('height', 0) > 300]
        if len(rows) != 1:
            raise Halt(f'Expected one Chrome work window, found {len(rows)}')
        self.pid, self.window = rows[0]['pid'], rows[0]['window_id']
        if rows[0].get('bounds', {}).get('width', 0) <= 500:
            self.call('bring_to_front', {'pid': self.pid, 'window_id': self.window, 'session': self.session})

    def open(self, url):
        need_read = not self.address or not self.metrics or self.metrics[-1]['tool'] != 'get_window_state'
        # Native Chrome navigation, like app menus, needs the exact work window
        # active; a foreground Return alone can leave the old document in place.
        self.call('bring_to_front', {'pid': self.pid, 'window_id': self.window,
                         'session': self.session})
        if need_read:
            self.read()
        if not self.address:
            self.read_full()
        if not self.address:
            raise Halt('Chrome address bar unavailable in the selected work window')
        target = {'pid': self.pid, 'window_id': self.window, 'session': self.session,
                  'element_token': self.address['element_token']}
        self.call('set_value', dict(target, value=url))
        # Native omnibox AXValue worked; background Return did not navigate in Chrome.
        self.call('press_key', dict(target, key='return', delivery_mode='foreground'))

    def read(self, full=False):
        d = self.call('get_window_state', {'pid': self.pid, 'window_id': self.window,
                      'session': self.session, 'include_screenshot': False,
                      'max_elements': 1600 if full else 600})
        self.metrics[-1]['read_mode'] = 'full' if full else 'short'
        web, elements = set(), []
        for e in d.get('elements', []):
            if e.get('role') == 'AXWebArea' or e.get('parent_index') in web:
                web.add(e['element_index'])
                elements.append(e)
        self.address = next((e for e in d.get('elements', [])
                    if e.get('label') == '地址和搜索栏'), None)
        url = self.address.get('value', '') if self.address else ''
        # The walk is depth-first. A native sibling after the final web node
        # proves the web subtree ended, even if trailing Chrome menus were cut.
        rows = d.get('elements', [])
        web_positions = [i for i, e in enumerate(rows) if e['element_index'] in web]
        web_complete = bool(web_positions and web_positions[-1] < len(rows) - 1 and self.address)
        return State(url, d.get('window_title', ''), elements, self.metrics[-1]['ms'],
                     full or d.get('elements_complete', False) or web_complete)

    def read_full(self):
        return self.read(full=True)

    def click(self, element):
        return self.call('click', {'pid': self.pid, 'window_id': self.window,
                         'session': self.session, 'element_token': element['element_token']})

    def fill_field(self, element, value):
        return self.call('set_value', {'pid': self.pid, 'window_id': self.window,
                         'session': self.session, 'element_token': element['element_token'], 'value': value})

class Jev:
    def __init__(self):
        if not os.environ.get('TYPESAFE_API_KEY'):
            raise Halt('Set TYPESAFE_API_KEY in the environment before running')
        os.environ.setdefault('TYPESAFE_MODEL', 'jev-1.13.0')
        from jev_choice.model import choose
        self.choose = choose
        self.calls = []

    def pick(self, state, candidates, goal):
        actions = [{'id': str(i), 'node': i, 'label': norm(e.get('label')), 'role': e['role'],
                    'kind': 'click', 'value': str(e.get('value', '')),
                    'checked': str(str(e.get('value')) == '1').lower()}
                   for i, e in enumerate(candidates)]
        page = {'url': 'https://www.apple.com.cn/', 'title': 'Apple purchase workflow',
                'text': '\n'.join(a['label'] for a in actions), 'actions': actions}
        answer = self.choose(page, goal, [])  # Never carry a previous product's completion history.
        self.calls.append({'ms': answer['latency_ms'], 'goal': goal})
        if answer['choice'] not in {a['id'] for a in actions}:
            raise Halt('Jev returned DONE/BLOCKED before the selected option was observed')
        result = candidates[int(answer['choice'])]
        return result

class Engine:
    def __init__(self, driver, jev, journal, plan, clock=time.monotonic, sleep=time.sleep,
                 auth_wait_seconds=0, shipping=None):
        self.driver, self.jev, self.path, self.plan = driver, jev, Path(journal), plan
        self.clock, self.sleep, self.cached = clock, sleep, None
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        self.record = json.loads(self.path.read_text()) if self.path.exists() else {
            'plan_hash': digest, 'mutations': {}, 'events': [], 'runs': []}
        if self.record['plan_hash'] != digest:
            raise Halt('Journal belongs to another purchase plan')
        self.run_started = clock()
        self.sleep_ms = 0
        self.auth_wait_seconds = max(0, auth_wait_seconds)
        self.auth_waited = 0
        self.shipping = shipping
        for previous in self.record['runs']:
            if previous['status'] == 'running':
                previous['status'] = 'interrupted_without_final_metrics'
        self.record['runs'].append({'started_at': time.time(), 'status': 'running'})

    def persist(self):
        # Persist partial metrics at existing checkpoints; interruption must not erase them.
        if self.record['runs'] and self.record['runs'][-1]['status'] == 'running':
            self.record['runs'][-1].update(
                elapsed_ms=round((self.clock()-self.run_started)*1000),
                driver_metrics=self.driver.metrics, sleep_ms=round(self.sleep_ms),
                auth_wait_ms=round(self.auth_waited*1000), jev_calls=self.jev.calls)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w') as stream:
            json.dump(self.record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)

    def observe(self, full=False):
        if self.cached is None:
            self.cached = self.driver.read_full() if full and hasattr(self.driver, 'read_full') else self.driver.read()
        if full and not self.cached.complete:
            self.cached = self.driver.read_full()
        return self.cached

    def pause(self, seconds):
        started = self.clock()
        self.sleep(max(0, seconds))
        self.sleep_ms += (self.clock() - started) * 1000

    def wait_for_auth(self):
        """Keep the same process/journal; the user completes credentials in Chrome."""
        remaining = self.auth_wait_seconds - self.auth_waited
        if remaining <= 0:
            raise Halt('auth_required: complete authentication in Chrome, then resume this journal')
        started = self.clock()
        event = {'key': 'auth_wait', 'run_index': len(self.record['runs']) - 1,
                 'status': 'waiting', 'elapsed_ms': round((started-self.run_started)*1000)}
        self.record['events'].append(event)
        self.persist()
        print(json.dumps({'status': 'auth_waiting', 'timeout_seconds': remaining,
                          'message': 'Complete authentication in this Chrome window; this process resumes automatically.'}), flush=True)
        try:
            while True:
                state = self.observe()
                if not state.needs_auth and not state.complete:
                    state = self.observe(full=True)
                if not state.needs_auth:
                    event['status'] = 'auth_screen_left'
                    return state
                left = remaining - (self.clock()-started)
                if left <= 0:
                    event['status'] = 'timeout'
                    raise Halt('auth_required: authentication wait expired; resume this same journal')
                self.pause(min(max(.05, .5-state.read_ms/1000), left))
                self.cached = None
        finally:
            elapsed = self.clock()-started
            self.auth_waited += elapsed
            if event['status'] == 'waiting':
                event['status'] = 'interrupted'
            event['duration_ms'] = round(elapsed*1000)
            self.persist()
            print(json.dumps({'status': event['status'], 'auth_wait_ms': event['duration_ms']}), flush=True)

    def wait(self, predicate, label, timeout=None, allow_auth=False):
        deadline = self.clock() + (timeout or self.plan.get('load_timeout_seconds', 120))
        next_full = self.clock()
        while True:
            state = self.observe()
            # A truncated snapshot proves presence, never absence. Search the full
            # surface immediately, then at least once a second while waiting.
            if not predicate(state) and not state.complete and self.clock() >= next_full:
                state = self.observe(full=True)
                next_full = self.clock() + 1
            if state.needs_auth and not allow_auth:
                started = self.clock()
                self.wait_for_auth()
                deadline += self.clock()-started
                continue
            if predicate(state):
                return state
            if state.title.strip() == 'Page Not Found - Apple':
                raise Halt('page_not_found: inspect the shopping bag before retrying any purchase action')
            if self.clock() >= deadline:
                raise Halt(f'timeout: {label}; no add or order submission was repeated')
            # One read serves both the previous action's postcondition and the next action.
            # Target a 500 ms sample cycle, including the cost of the AX read.
            self.pause(min(max(.05, .5 - state.read_ms / 1000), max(0, deadline-self.clock())))
            self.cached = None

    def navigate(self, url, ready):
        # Navigation is repeatable only before a purchase mutation is pending.
        pending = [m for m in self.record['mutations'].values() if m['status'] == 'attempted'
                   and m['kind'] in ('add', 'submit')]
        if pending:
            raise Halt('pending_mutation: inspect the current result before any navigation')
        self.driver.open(url)
        self.cached = None
        deadline = self.clock() + self.plan.get('load_timeout_seconds', 120)
        return self.product_wait(ready, {'url': url}, deadline)

    def product_wait(self, predicate, product, deadline):
        """Wait for live controls without reloading a queue or in-flight document."""
        if any(m['status'] == 'attempted' and m['kind'] in ('add', 'submit')
               for m in self.record['mutations'].values()):
            raise Halt('pending_mutation: product configuration prohibited')
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise Halt('timeout: product unavailable; no page refresh performed')
        return self.wait(predicate, 'product ready; no page refresh performed', timeout=remaining)

    def action(self, key, pattern, goal, postcondition, kind='option', validator=None, decision=None):
        # Durable intent is written before dispatch. Unknown outcomes never cause a second click.
        mutation = self.record['mutations'].get(key) if kind in ('add', 'submit') else None
        if mutation:
            state = self.wait(postcondition, f'reconcile {key}')
            mutation['status'] = 'confirmed'
            self.persist()
            return state
        state = self.wait(lambda s: bool(s.controls(pattern)), f'control {key}')
        if kind == 'option' and postcondition(state):
            return state  # Hydration may have selected it while we waited for the control.
        candidates = state.controls(pattern)
        if validator and not any(validator(e) for e in candidates) and not state.complete:
            state = self.observe(full=True)
            candidates = state.controls(pattern)
        # The authorized exact selection is already in the plan. Do not send
        # known-wrong alternatives to Jev and pay a request to rediscover it.
        if validator:
            candidates = [e for e in candidates if validator(e)]
            if not candidates:
                raise Halt(f'No option consistent with the purchase plan: {key}')
        # Duplicate top/bottom checkout buttons have identical meanings.
        unique = {norm(e.get('label')): e for e in candidates}
        chosen = decision if decision is not None else self.jev.pick(state, list(unique.values()), goal)
        if chosen not in candidates:
            raise Halt('Model selected an inconsistent or stale control')
        if validator and not validator(chosen):
            raise Halt(f'Jev selected an option inconsistent with the purchase plan: {key}')
        if kind != 'option':
            self.record['mutations'][key] = {'status': 'attempted', 'kind': kind,
                                             'label': norm(chosen.get('label'))}
            self.persist()
        started = self.clock()
        metric_start, sleep_start = len(self.driver.metrics), self.sleep_ms
        self.driver.click(chosen)
        self.cached = None
        state = self.wait(postcondition, f'result {key}')
        if kind != 'option':
            self.record['mutations'][key]['status'] = 'confirmed'
        event = {'key': key, 'action': norm(chosen.get('label')),
                 'run_index': len(self.record['runs']) - 1,
                 'driver_ms': sum(m['ms'] for m in self.driver.metrics[metric_start:]),
                 'sleep_ms': round(self.sleep_ms - sleep_start),
                 'action_and_wait_ms': round((self.clock()-started)*1000),
                 'elapsed_ms': round((self.clock()-self.run_started)*1000)}
        self.record['events'].append(event)
        if kind != 'option':
            self.persist()
        print(json.dumps(event, ensure_ascii=False), flush=True)
        return state

    def configure(self, index, product, resume=False, require_add=False):
        prefix = f'product:{index}'
        current = self.observe() if resume else None
        if any(m['status'] == 'attempted' and m['kind'] in ('add', 'submit')
               for m in self.record['mutations'].values()):
            raise Halt('pending_mutation: configuration navigation prohibited')
        using_entry = False
        if not current or product['url'].split('/shop/',1)[-1] not in current.url:
            entry = product.get('entry_url', product['url'])
            if 'entry_url' in product:
                target, canonical = urlsplit(entry), urlsplit(product['url'])
                if target.scheme != canonical.scheme or target.hostname.removeprefix('www.') != canonical.hostname.removeprefix('www.') \
                        or not target.path.startswith(canonical.path + '/'):
                    raise Halt('Configured entry URL is outside the approved product')
                using_entry = True
            self.driver.open(entry)
            self.cached = None
        deadline = self.clock() + self.plan.get('product_wait_seconds', 1800)
        waiter = lambda predicate, label: self.product_wait(predicate, product, deadline)
        # Reversible options are confirmed from the live page, never from a past journal.
        for key in list(self.record['mutations']):
            if key.startswith(prefix + ':'):
                del self.record['mutations'][key]
        state = waiter(lambda s: bool(s.controls(product['options'][0]['pattern']))
                       and (not using_entry or product['name'] in norm(s.title)), 'product ready')
        using_entry = False
        for option in product['options']:
            expected = option['expected']
            selected = lambda s, expected=expected: any(e.get('role') == 'AXRadioButton'
                and re.search(expected, semantic(e.get('label'))) and str(e.get('value')) == '1' for e in s.elements)
            if selected(state):
                continue
            state = self.action(prefix+':'+option['name'], option['pattern'], option['goal'], selected,
                                validator=lambda e, expected=expected: bool(re.search(expected, semantic(e.get('label')))))
        if require_add:
            state = waiter(lambda s: bool(s.controls(r'^添加到购物袋$')), 'available to add')
        if not all(any(e.get('role') == 'AXRadioButton'
                       and re.search(option['expected'], semantic(e.get('label')))
                       and str(e.get('value')) == '1' for e in state.elements)
                   for option in product['options']):
            raise Halt('Selected options changed; stopped without repeating clicks')
        return state

    def verify_bag(self, state):
        if not state.complete:
            state = self.observe(full=True)
        expected = self.plan['product_names']
        names = [norm(e.get('label')) for e in state.elements if e.get('role') == 'AXHeading'
                 and str(e.get('value')) == '2'
                 and norm(e.get('label')).startswith('iPhone ') and 'AppleCare' not in norm(e.get('label'))]
        quantities = [str(e.get('value')) for e in state.elements
                      if e.get('role') == 'AXPopUpButton' and norm(e.get('label')) == '数量']
        if sorted(names) != sorted(expected) or quantities != ['1'] * len(expected):
            raise Halt('Shopping bag product or quantity mismatch')
        amount = state.total
        cap = self.plan.get('max_total')
        if amount is None or amount <= 0 or (cap is not None and amount > cap):
            raise Halt('Shopping bag amount mismatch')
        self.record['bag_verified'] = {'products': expected, 'quantity_each': 1, 'total': float(amount)}
        self.persist()

    def payment_ready(self, state):
        amount = Decimal(str(self.record.get('bag_verified', {}).get('total', self.plan['expected_total'])))
        forms = {f'{amount:,.2f}', f'{amount:.2f}'}
        if amount == int(amount):
            forms.update({str(int(amount)), f'{int(amount):,}'})
        pattern = r'(?<![\d,.])(?:' + '|'.join(re.escape(v) for v in forms) + r')(?![\d,.])'
        return state.at_alipay and bool(re.search(r'支付宝|Alipay', state.title, re.I)) \
            and bool(re.search(pattern, state.text))

    def checkout(self, submit=False):
        if submit and (self.plan.get('max_total') is None or
                       self.plan['expected_total'] > self.plan['max_total']):
            raise Halt('An explicitly authorized price limit is required before submission')
        address_ready = lambda s: bool(self.shipping and self.shipping.can_restore(s))
        state = self.observe(full=True)
        if '/shop/bag' in state.url:
            state = self.wait(lambda s: s.bag_ready, 'shopping bag ready')
            self.verify_bag(state)
            state = self.action('checkout', r'^结账$', '结账当前购物袋',
                lambda s: bool(s.controls(r'^(继续填写送货地址|继续选择付款方式|支付宝|检查订单|立即下单)$')) or address_ready(s),
                kind='checkout')
        state = self.wait(lambda s: bool(s.controls(
            r'^(继续填写送货地址|继续选择付款方式|支付宝|检查订单|查看订单|立即下单|提交订单|下订单)$'))
            or address_ready(s) or '等待付款' in s.text or s.at_alipay, 'checkout content ready')
        # Known page controls, not a guessed count of polling iterations.
        stages = [
            ('delivery', r'^继续填写送货地址$', '使用送货上门', r'^继续选择付款方式$'),
            ('address', r'^继续选择付款方式$', '保留已有收货信息，继续选择付款方式', r'^支付宝$'),
            ('alipay', r'^(支付宝|微信支付|花呗分期|微信分付|信用卡.*)$', '选择支付宝全额付款，不分期', None),
            ('review', r'^(检查订单|查看订单)$', '检查订单', r'^(立即下单|提交订单|下订单)$')]
        for key, pattern, goal, next_pattern in stages:
            state = self.observe()
            if key == 'address' and address_ready(state):
                state = self.shipping.restore(self)
            if not state.controls(pattern) and not state.complete:
                state = self.observe(full=True)
            if state.controls(r'^(立即下单|提交订单|下订单)$') or '等待付款' in state.text or state.at_alipay:
                break
            if state.needs_auth:
                state = self.wait(lambda s: not s.needs_auth and bool(s.controls(
                    r'^(继续填写送货地址|继续选择付款方式|支付宝|检查订单|查看订单|立即下单|提交订单|下订单)$') or address_ready(s)),
                    'checkout after authentication')
            if not state.controls(pattern):
                continue  # Resume from the currently observed later page.
            if key == 'alipay':
                if state.radio_selected('支付宝'):
                    continue
                state = self.action(key, pattern, goal, lambda s: s.radio_selected('支付宝'),
                                    validator=lambda e: norm(e.get('label')) == '支付宝')
            else:
                state = self.action(key, pattern, goal,
                                    lambda s, next_pattern=next_pattern, key=key: bool(s.controls(next_pattern))
                                    or (key == 'delivery' and address_ready(s)), kind='checkout')
        state = self.wait(lambda s: bool(s.controls(r'^(立即下单|提交订单|下订单)$'))
                           or '等待付款' in s.text or s.at_alipay, 'order review or existing outcome')
        if state.at_alipay:
            self.wait(self.payment_ready, 'Alipay title and amount')
            return 'alipay_reached'
        if '等待付款' not in state.text:
            state = self.observe(full=True)
            amount = state.total
            expected_amount = Decimal(str(self.record.get('bag_verified', {}).get('total', self.plan['expected_total'])))
            if not all(name in state.text for name in self.plan['product_names']) \
                    or amount != expected_amount or (self.plan.get('max_total') is not None and amount > self.plan['max_total']) \
                    or '支付宝' not in state.text:
                raise Halt('Final review mismatch; no order submitted')
            if not submit:
                return 'review_ready'
            state = self.action('submit', r'^(立即下单|提交订单|下订单)$', '提交已核对的订单',
                                lambda s: '等待付款' in s.text or s.at_alipay, kind='submit')
        order = re.search(r'W\d{8,}', state.text)
        if order:
            self.record['order'] = order.group()
            self.persist()
        if not state.at_alipay:
            self.action('open_alipay', r'^现在支付$', '打开支付宝付款页面，不实际付款',
                        self.payment_ready, kind='payment_redirect')
        else:
            self.wait(self.payment_ready, 'Alipay title and amount')
        return 'alipay_reached'

    def finish(self, status):
        self.record['runs'][-1].update(status=status, elapsed_ms=round((self.clock()-self.run_started)*1000))
        if self.cached is not None:
            # Diagnose the last observed state without persisting URL tokens,
            # private paths, titles containing account data, or page contents.
            u = urlsplit(self.cached.url if '://' in self.cached.url else 'https://' + self.cached.url)
            route = next((name for prefix, name in (
                ('/shop/buy-iphone/', 'product'), ('/shop/bag', 'bag'),
                ('/shop/checkout', 'checkout'), ('/shop/signIn', 'auth'),
                ('/shop/sorry/', 'store_error')) if u.path.startswith(prefix)), 'other')
            self.record['runs'][-1]['last_observed_page'] = {
                'route_kind': route,
                'page_not_found': self.cached.title.strip() == 'Page Not Found - Apple',
                'maintenance_notice': '来得早，来得好。' in self.cached.text,
                'add_form_parameters_in_url': 'add-to-cart' in dict(parse_qsl(u.query)),
            }
        self.record['runs'][-1]['driver_metrics'] = self.driver.metrics
        self.record['runs'][-1]['jev_calls'] = self.jev.calls
        self.record['runs'][-1]['sleep_ms'] = round(self.sleep_ms)
        self.record['runs'][-1]['auth_wait_ms'] = round(self.auth_waited*1000)
        self.record['status'] = status
        self.record['driver_metrics'] = self.driver.metrics
        self.record['jev_calls'] = self.jev.calls
        self.persist()
        print(json.dumps({'status': status, 'elapsed_ms': self.record['runs'][-1]['elapsed_ms'],
                          'reads': sum(m['tool']=='get_window_state' for m in self.driver.metrics),
                          'jev_calls': len(self.jev.calls)}, ensure_ascii=False), flush=True)

    def checkpoint(self, phase, index=0):
        self.record.update(phase=phase, product_index=index)
        self.persist()

def run(a):
    plan = json.loads(Path(a.plan).read_text())
    driver, jev = Cua(window_id=a.window_id), Jev()
    shipping = None
    if getattr(a, 'shipping_backup', None):
        from shipping_backup import ShippingBackup
        shipping = ShippingBackup(a.shipping_backup)
    engine = Engine(driver, jev, a.journal, plan,
                    auth_wait_seconds=getattr(a, 'auth_wait_seconds', 0), shipping=shipping)
    if engine.record.get('status') == 'alipay_reached':
        print(json.dumps({'status': 'alipay_reached', 'already_completed': True}), flush=True)
        return
    try:
        engine.persist()
        driver.bind()
        # A human may have advanced checkout while a previous run was interrupted.
        # Inspect once on resume, never navigate back to a stale configuration phase.
        if len(engine.record['runs']) > 1:
            live = engine.observe(full=True)
            u = urlsplit(live.url if '://' in live.url else 'https://' + live.url)
            apple_checkout = (u.hostname == 'apple.com.cn' or
                              (u.hostname or '').endswith('.apple.com.cn')) and u.path.startswith('/shop/checkout')
            if apple_checkout or live.at_alipay:
                engine.checkpoint('checkout')
        if not a.resume_checkout and engine.record.get('phase') != 'checkout':
            phase = engine.record.get('phase', 'new')
            if phase == 'new' and a.stop_at != 'configure':
                state = engine.navigate('https://www.apple.com.cn/shop/bag', lambda s: s.bag_ready)
                if '你的购物袋中没有商品' not in state.text:
                    raise Halt('Bag is not empty; existing items are preserved')
            if phase == 'new':
                engine.checkpoint('configure')
            for index in range(engine.record.get('product_index', 0), len(plan['products'])):
                product = plan['products'][index]
                phase = engine.record.get('phase')
                if phase == 'configure':
                    engine.configure(index, product, resume=len(engine.record['runs']) > 1,
                                     require_add=a.stop_at != 'configure')
                    if a.stop_at == 'configure':
                        engine.checkpoint('configure', index + 1)
                        continue
                    engine.checkpoint('add', index)
                if engine.record['phase'] == 'add':
                    if f'add:{index}' not in engine.record['mutations']:
                        engine.configure(index, product, resume=True, require_add=True)
                    engine.action(f'add:{index}', r'^添加到购物袋$', '将已选配的手机加入购物袋',
                                  lambda s: bool(s.controls(r'^查看购物袋$')), kind='add')
                    engine.checkpoint('view_bag', index)
                if engine.record['phase'] == 'view_bag':
                    if index < len(plan['products']) - 1:
                        # Add outcome is confirmed; inspect the combined bag only once, at the end.
                        engine.checkpoint('configure', index + 1)
                        continue
                    engine.action(f'bag:{index}', r'^查看购物袋$', '查看购物袋，不添加配件',
                                  lambda s: '/shop/bag' in s.url and product['name'] in s.text, kind='navigation')
                    engine.checkpoint('configure', index + 1)
            if a.stop_at == 'configure':
                engine.finish('configuration_verified'); return
            if a.stop_at == 'bag':
                engine.verify_bag(engine.observe())
                engine.finish('bag_verified'); return
            engine.checkpoint('checkout')
        result = engine.checkout(submit=a.submit)
        engine.finish(result)
    except (Exception, KeyboardInterrupt) as error:
        engine.finish(str(error) or 'interrupted')
        raise SystemExit(2) from None

def main():
    # All command-line purchases use the model-led policy, including the old filename.
    from jev_agent import main as agent_main
    return agent_main()

if __name__ == '__main__':
    main()
