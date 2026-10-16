"""Local-only shipping fallback. Never log or send field values to a model."""
import json
import os
import stat
from pathlib import Path

from purchase_runner import Halt, norm

ADDRESS_LABELS = ('姓氏', '名字', '收货地址：', '详细地址', '附加详细地址', '收件人手机号码')
CONTACT_LABELS = ('电子邮件地址', '联系人手机号码')


class ShippingBackup:
    def __init__(self, path):
        path = Path(path).expanduser()
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise Halt('Shipping backup must be owned by this user and readable only by this user')
        data = json.loads(path.read_text())
        self.fields, self.contact = data.get('fields', {}), data.get('contact', {})
        if data.get('version') != 1 or set(self.fields) != set(ADDRESS_LABELS) or set(self.contact) != set(CONTACT_LABELS):
            raise Halt('Unsupported shipping backup schema')
        if not all(isinstance(v, str) for v in [*self.fields.values(), *self.contact.values()]):
            raise Halt('Invalid shipping backup fields')
        if not all(norm(v) for k, v in self.fields.items() if k != '附加详细地址') or not all(map(norm, self.contact.values())):
            raise Halt('Shipping backup is incomplete')
        self.region = self.fields['收货地址：'].split()
        if len(self.region) != 3:
            raise Halt('Unsupported shipping region format; manual input required')

    @staticmethod
    def inputs(state):
        result = {}
        for e in state.elements:
            if e.get('role') == 'AXTextField' and norm(e.get('label')) in (*ADDRESS_LABELS, *CONTACT_LABELS):
                label = norm(e['label'])
                if label in result:
                    raise Halt('Ambiguous shipping input; manual input required')
                result[label] = e
        return result

    def can_restore(self, state):
        return bool(self.inputs(state))

    def restore(self, engine):
        state = engine.observe(full=True)
        inputs = self.inputs(state)
        changed = 0
        if any(label in inputs for label in ADDRESS_LABELS):
            changed += self._fill(engine, self.fields, region=True)
            state = engine.observe(full=True)
            save = state.controls('^保存更改$')
            if save:
                if len(save) != 1:
                    raise Halt('Ambiguous shipping save control')
                engine.driver.click(save[0])
                engine.cached = None
                engine.wait(lambda s:not s.controls('^保存更改$') and
                            (bool(s.controls('^继续选择付款方式$')) or
                             any(k in self.inputs(s) for k in CONTACT_LABELS)),
                            'shipping address accepted')
        state = engine.observe(full=True)
        if any(label in self.inputs(state) for label in CONTACT_LABELS):
            changed += self._fill(engine, self.contact)
        if changed:
            # Only counts and timing metadata enter the purchase journal.
            event = {'key':'shipping_restored', 'fields_filled':changed,
                     'run_index':len(engine.record['runs'])-1,
                     'elapsed_ms':round((engine.clock()-engine.run_started)*1000)}
            engine.record['events'].append(event)
            engine.persist()
            print(json.dumps(event), flush=True)
        return engine.observe()

    def _fill(self, engine, expected, region=False):
        inputs = self.inputs(engine.observe(full=True))
        if not set(expected).issubset(inputs):
            raise Halt('Shipping form changed; manual input required')
        empty = [k for k in expected if not norm(inputs[k].get('value')) and norm(expected[k])]
        # No recovery is necessary for a filled contact form. Do not replace it.
        if not empty and not region:
            return 0
        if any(norm(e.get('value')) and norm(e.get('value')) != norm(expected[k])
               for k, e in inputs.items() if k in expected):
            raise Halt('Existing shipping values conflict with backup; nothing overwritten')
        for label in empty:
            inputs = self.inputs(engine.observe(full=True))
            field = inputs[label]
            if norm(field.get('value')):
                if norm(field['value']) != norm(expected[label]):
                    raise Halt('Shipping value changed during recovery; stopped')
                continue
            if label == '收货地址：':
                engine.driver.click(field)
                engine.cached = None
                for part in self.region:
                    def matches(s):
                        return [e for e in s.elements if e.get('role') == 'AXButton'
                                and e.get('enabled', True) and norm(e.get('label')) == part]
                    state = engine.wait(lambda s:len(matches(s)) == 1, 'saved region option', timeout=10)
                    engine.driver.click(matches(state)[0])
                    engine.cached = None
            else:
                engine.driver.fill_field(field, expected[label])
                engine.cached = None
            engine.wait(lambda s: label in self.inputs(s) and
                        norm(self.inputs(s)[label].get('value')) == norm(expected[label]),
                        'shipping field accepted', timeout=5)
        # Re-read immediately before saving; never mix a concurrent edit with backup data.
        engine.cached = None
        final = self.inputs(engine.observe(full=True))
        if any(k not in final or norm(final[k].get('value')) != norm(v) for k, v in expected.items()):
            raise Halt('Shipping recovery mismatch; manual input required')
        return len(empty)
