"""Jev chooses each next operation; native Cua executes within transaction guards."""
import argparse
import json
import re
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from purchase_runner import Cua, Engine, Halt, Jev, journal_lock, norm, semantic
from shipping_backup import ShippingBackup


class Policy:
    def __init__(self, model):
        self.model = model
        self.calls = model.calls

    def decide(self, state, offers, facts, goal):
        # No raw page text, account title, input values, tokens or shipping data.
        actions = []
        for i, offer in enumerate(offers):
            action = {'id':str(i), 'node':i, 'label':offer['label'], 'role':offer.get('element',{}).get('role','AXButton'), 'kind':'click',
                      'checked':'false', 'value':'0'}
            actions.append(action)
        actions.append({'id':'WAIT','node':len(actions),'label':'Wait for the current page to finish loading without refreshing','kind':'wait'})
        page = {'url':'https://www.apple.com.cn/', 'title':'Apple purchase workflow',
                'text':json.dumps(facts, ensure_ascii=False), 'actions':actions}
        answer = self.model.choose(page, goal, [])
        choice = answer['choice']
        self.calls.append({'ms':answer['latency_ms'], 'choice':choice,
                           'model':answer.get('model'), 'candidate_count':len(offers)})
        print(json.dumps({'status':'jev_decision','choice':choice,'model_ms':answer['latency_ms'],
                          'model':answer.get('model')},ensure_ascii=False),flush=True)
        if choice == 'WAIT': return None
        if choice not in {str(i) for i in range(len(offers))}:
            raise Halt('Jev did not choose a supported next action; no action executed')
        return offers[int(choice)]


class Agent(Engine):
    def selected(self, state, option):
        return any(e.get('role')=='AXRadioButton' and re.search(option['expected'],semantic(e.get('label')))
                   and str(e.get('value'))=='1' for e in state.elements)

    def confirmed(self, key):
        return self.record['mutations'].get(key,{}).get('status')=='confirmed'

    def facts(self, state):
        i=self.record.get('agent_product')
        current = self.plan['products'][i] if i is not None else None
        return {'current_product':current['name'] if current else None,
                'requested_options':[{ 'goal':o['goal'], 'already_selected':self.selected(state,o)} for o in current['options']] if current else [],'products':self.plan['product_names'], 'budget':self.plan['max_total'],
                'confirmed_adds':[i for i in range(len(self.plan['products'])) if self.confirmed(f'add:{i}')],
                'bag_verified':bool(self.record.get('bag_verified')), 'alipay_selected':state.radio_selected('支付宝'),
                'page_kind':('alipay' if state.at_alipay else 'checkout' if '/shop/checkout' in state.url else
                             'bag' if '/shop/bag' in state.url else 'product' if '/shop/buy-iphone/' in state.url else 'other')}

    def offers(self, state, submit):
        u=urlsplit(state.url if '://' in state.url else 'https://'+state.url)
        if state.at_alipay: return []
        if not (u.hostname=='apple.com.cn' or (u.hostname or '').endswith('.apple.com.cn')):
            if self.record.get('agent_started'): raise Halt('Unexpected host; no purchase action executed')
        pending=[k for k,m in self.record['mutations'].items() if m['status']=='attempted' and m['kind'] in ('add','submit')]
        if pending: raise Halt('Unconfirmed transaction: reconcile before continuing')
        offers=[]
        def click(key, pattern, label, post, kind='checkout', validator=None):
            candidates=state.controls(pattern)
            if validator: candidates=[e for e in candidates if validator(e)]
            unique={norm(e.get('label')):e for e in candidates}
            for e in unique.values():
                offers.append({'key':key,'label':label+'：'+norm(e.get('label')), 'element':e,
                               'pattern':pattern,'post':post,'kind':kind,'validator':validator})
        if not self.record.get('agent_started'):
            return [{'key':'initial_bag','label':'打开购物袋检查是否为空','url':'https://www.apple.com.cn/shop/bag',
                     'ready':lambda s:s.bag_ready}]
        if '/shop/bag' in state.url and state.bag_ready:
            if not self.record.get('initial_bag_empty'):
                if '你的购物袋中没有商品' not in state.text: raise Halt('Initial bag not empty; preserved')
                self.record['initial_bag_empty']=True; self.persist()
            if all(self.confirmed(f'add:{i}') for i in range(len(self.plan['products']))):
                self.verify_bag(state)
                click('checkout','^结账$','结账已核对的两台手机',lambda s:'/shop/checkout' in s.url or s.needs_auth)
                return offers
        if '/shop/checkout' in state.url:
            if '等待付款' in state.text:
                match=re.search(r'W\d{8,}',state.text)
                if match:self.record['order']=match.group();self.persist()
                click('open_alipay','^现在支付$','打开支付宝付款页然后停止，不付款',self.payment_ready,'payment_redirect')
                return offers
            if self.shipping and self.shipping.can_restore(state):
                fields=self.shipping.inputs(state)
                required=set(self.shipping.fields)|set(self.shipping.contact)
                required.discard('附加详细地址')
                if any(k in required and not norm(e.get('value')) for k,e in fields.items()):
                    return [{'key':'restore_shipping','label':'从本机私有配置补齐空白收货必填项，不向模型发送字段内容'}]
            click('delivery','^继续填写送货地址$','沿用送货上门，继续地址',lambda s:bool(s.controls('^继续选择付款方式$')) or bool(self.shipping and self.shipping.can_restore(s)))
            click('address','^继续选择付款方式$','沿用官网已填写的地址，继续付款方式',lambda s:bool(s.controls('^支付宝$')))
            if not state.radio_selected('支付宝'):
                click('alipay','^支付宝$','选择支付宝全额付款',lambda s:s.radio_selected('支付宝'),'option')
            if state.radio_selected('支付宝'):
                click('review','^(检查订单|查看订单)$','进入订单确认页',lambda s:bool(s.controls('^(立即下单|提交订单|下订单)$')))
            if state.controls('^(立即下单|提交订单|下订单)$'):
                amount=state.total
                expected=Decimal(str(self.record.get('bag_verified',{}).get('total',self.plan['expected_total'])))
                if not all(n in state.text for n in self.plan['product_names']) or amount!=expected or amount>self.plan['max_total'] or '支付宝' not in state.text:
                    raise Halt('Final review mismatch; no order submitted')
                if not submit:return [{'key':'review_done','label':'已到订单确认页，停止，不提交'}]
                offers.clear()
                click('submit','^(立即下单|提交订单|下订单)$','提交一次已授权订单，之后只打开支付宝不付款',lambda s:'等待付款' in s.text or s.at_alipay,'submit')
            return offers
        # After a confirmed add, Jev can choose the next unpurchased product.
        can_navigate = ('/shop/bag' in state.url and state.bag_ready) or bool(state.controls('^查看购物袋$'))
        if can_navigate:
            for i,p in enumerate(self.plan['products']):
                if not self.confirmed(f'add:{i}'):
                    offers.append({'key':f'product:{i}','label':'打开选配 '+p['name'],'url':p.get('entry_url',p['url']),
                                   'product_index':i,'ready':lambda s,p=p:bool(s.controls(p['options'][0]['pattern'])) and p['name'] in norm(s.title)})
            if all(self.confirmed(f'add:{i}') for i in range(len(self.plan['products']))):
                click('view_bag','^查看购物袋$','查看已加入的两台手机，准备结账',lambda s:s.bag_ready,'navigation')
            return offers
        i=self.record.get('agent_product')
        if i is not None and not self.confirmed(f'add:{i}'):
            p=self.plan['products'][i]
            if '/shop/buy-iphone/' not in state.url:return []
            for option in p['options']:
                if not self.selected(state,option):
                    click(f'option:{i}:{option["name"]}',option['pattern'],option['goal'],
                          lambda s,o=option:self.selected(s,o),'option',
                          lambda e,o=option:bool(re.search(o['expected'],semantic(e.get('label')))))
            if all(self.selected(state,o) for o in p['options']):
                click(f'add:{i}','^添加到购物袋$','将 '+p['name']+' 加入购物袋一次',lambda s:bool(s.controls('^查看购物袋$')),'add')
        return offers

    def execute(self, offer):
        key=offer['key']
        if 'url' in offer:
            self.navigate(offer['url'],offer['ready'])
            self.record['agent_started']=True
            if 'product_index' in offer:self.record['agent_product']=offer['product_index']
            self.persist()
        elif key=='restore_shipping':self.shipping.restore(self)
        elif key=='review_done':return 'review_ready'
        else:
            self.action(key,offer['pattern'],offer['label'],offer['post'],kind=offer['kind'],
                        validator=offer['validator'],decision=offer['element'])
        return None

    def loop(self, submit):
        if submit and (self.plan.get('max_total') is None or self.plan['expected_total']>self.plan['max_total']):
            raise Halt('Authorized budget required')
        goal=('购买 '+ '、'.join(self.plan['product_names'])+'，各一台，无折抵、无AppleCare，支付宝全额付款。'
              +'根据当前页面和已完成事项选择最有效的下一步，不重复已完成动作。后续步骤的按钮尚未出现不表示受阻，先完成当前可执行选项。候选动作是当前可操作控件或已授权导航；'
              +('提交一次订单，打开支付宝就停止，绝不付款。' if submit else '停在订单确认页，不提交。'))
        for _ in range(100):
            self.cached=None
            state=self.observe(full=True)
            if state.needs_auth:
                self.wait_for_auth();continue
            if self.payment_ready(state): return 'alipay_reached'
            available=self.offers(state,submit)
            if not available:
                state=self.wait(lambda s:s.at_alipay or bool(self.offers(s,submit)), 'next actionable page')
                if self.payment_ready(state):return 'alipay_reached'
                available=self.offers(state,submit)
            decision=self.jev.decide(state,available,self.facts(state),goal)
            self.persist()
            if decision is None:
                self.pause(.5);continue
            result=self.execute(decision)
            if result:return result
        raise Halt('Decision limit reached; no automatic restart')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--plan',required=True);p.add_argument('--journal',required=True)
    p.add_argument('--window-id',type=int,required=True);p.add_argument('--submit',action='store_true')
    p.add_argument('--auth-wait-seconds',type=float,default=300);p.add_argument('--shipping-backup')
    a=p.parse_args()
    with journal_lock(a.journal):
        plan=json.loads(Path(a.plan).read_text())
        e=Agent(Cua(window_id=a.window_id),Policy(Jev()),a.journal,plan,auth_wait_seconds=a.auth_wait_seconds,
                shipping=ShippingBackup(a.shipping_backup) if a.shipping_backup else None)
        if e.record.get('status') in ('alipay_reached', 'order_cancelled_verified'):print('Already completed; no new purchase');return
        try:
            e.persist();e.driver.bind();e.finish(e.loop(a.submit))
        except (Exception,KeyboardInterrupt) as error:
            e.finish(str(error) or 'interrupted');raise SystemExit(2) from None

if __name__=='__main__':main()
