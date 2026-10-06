import json
import pytest
from purchase_runner import Engine, Halt, Jev, State

class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds

def element(label, role='AXButton', value=None):
    return {'label': label, 'role': role, 'value': value, 'element_token': 'test', 'enabled': True}

def state(*labels, url='apple.com.cn/shop/checkout'):
    return State(url, 'test', [element(label) for label in labels])

class Driver:
    def __init__(self, read, failure=False):
        self.reader, self.failure = read, failure
        self.clicks, self.reads, self.metrics = [], 0, []
    def read(self):
        self.reads += 1
        return self.reader()
    def click(self, e):
        self.clicks.append(e['label'])
        if self.failure: raise TimeoutError('connection dropped after dispatch')

class Picker:
    calls = []
    def pick(self, state, candidates, goal): return candidates[0]

@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    plan = {'products': [], 'product_names':['iPhone Duo 512GB 星光白色'],
            'expected_total':17999, 'max_total':17999, 'load_timeout_seconds':40}
    def make(driver, file='journal.json'):
        return Engine(driver, Picker(), tmp_path/file, plan, clock, clock.sleep)
    return clock, make

def test_same_snapshot_serves_two_stages(setup):
    clock, make = setup
    driver = Driver(lambda: state('ready'))
    engine = make(driver)
    engine.wait(lambda s:'ready' in s.text, 'first')
    engine.wait(lambda s:'ready' in s.text, 'second')
    assert driver.reads == 1

def test_twenty_second_load_does_not_hit_iteration_limit(setup):
    clock, make = setup
    driver = Driver(lambda: state('ready' if clock() >= 20 else 'loading'))
    result = make(driver).wait(lambda s:'ready' in s.text, 'slow server')
    assert result.text == 'ready'
    assert 20 <= clock() < 22
    assert driver.reads <= 41  # Bounded 500 ms polling; no busy loop.

def test_mutation_is_not_replayed_after_connection_loss(setup):
    clock, make = setup
    driver = Driver(lambda: state('添加到购物袋'), failure=True)
    engine = make(driver)
    with pytest.raises(TimeoutError):
        engine.action('add:0', '^添加到购物袋$', 'add', lambda s:'查看购物袋' in s.text, kind='add')
    assert engine.record['mutations']['add:0']['status'] == 'attempted'
    after = Driver(lambda: state('查看购物袋'))
    resumed = make(after)
    resumed.action('add:0', '^添加到购物袋$', 'add', lambda s:'查看购物袋' in s.text, kind='add')
    assert not after.clicks
    assert resumed.record['mutations']['add:0']['status'] == 'confirmed'

def test_ambiguous_submit_stays_single_attempt_after_resume(setup):
    clock, make = setup
    driver = Driver(lambda: state('立即下单'))
    engine = make(driver)
    with pytest.raises(Halt, match='timeout'):
        engine.action('submit', '^立即下单$', 'submit', lambda s:'等待付款' in s.text, kind='submit')
    assert driver.clicks == ['立即下单']
    resumed = make(driver)
    with pytest.raises(Halt, match='timeout'):
        resumed.action('submit', '^立即下单$', 'submit', lambda s:'等待付款' in s.text, kind='submit')
    assert driver.clicks == ['立即下单']

def test_pending_add_prevents_refresh_navigation(setup):
    clock, make = setup
    engine = make(Driver(lambda: state('loading')))
    engine.record['mutations']['add:0'] = {'status':'attempted','kind':'add'}
    with pytest.raises(Halt, match='pending_mutation'):
        engine.navigate('https://www.apple.com.cn/shop/bag', lambda s:True)

def test_auth_requires_human_without_mutation(setup):
    clock, make = setup
    driver = Driver(lambda: state('登录',url='secure10.www.apple.com.cn/shop/signIn'))
    with pytest.raises(Halt, match='auth_required'):
        make(driver).wait(lambda s:False, 'checkout')
    assert not driver.clicks

def test_wrong_model_option_fails_before_click(setup):
    clock, make = setup
    driver = Driver(lambda: state('花呗分期','支付宝'))
    with pytest.raises(Halt, match='inconsistent'):
        make(driver).action('pay','^(花呗分期|支付宝)$','Alipay',lambda s:False,
                            validator=lambda e:e['label']=='支付宝')
    assert not driver.clicks

def test_review_mode_never_submits(setup):
    clock, make = setup
    review = state('iPhone Duo 512GB 星光白色','总计 RMB 17,999','支付宝','立即下单')
    driver = Driver(lambda:review)
    assert make(driver).checkout(submit=False) == 'review_ready'
    assert not driver.clicks

def test_wrong_final_amount_never_submits(setup):
    clock, make = setup
    review = state('iPhone Duo 512GB 星光白色','总计 RMB 19,999','支付宝','立即下单')
    driver = Driver(lambda:review)
    with pytest.raises(Halt, match='Final review mismatch'):
        make(driver).checkout(submit=True)
    assert not driver.clicks

def test_changed_plan_refuses_old_journal(setup):
    clock, make = setup
    engine = make(Driver(lambda:state('ready')))
    engine.persist()
    saved = json.loads(engine.path.read_text())
    saved['plan_hash']='other-plan'
    engine.path.write_text(json.dumps(saved))
    with pytest.raises(Halt, match='another purchase plan'):
        make(Driver(lambda:state('ready')))

def test_unique_button_does_not_call_jev():
    jev = Jev.__new__(Jev)
    jev.choose = lambda *a: (_ for _ in ()).throw(AssertionError('unnecessary model request'))
    button = element('继续填写送货地址')
    assert jev.pick(state(), [button], 'continue') is button

def test_bag_title_does_not_mean_body_loaded():
    loading = State('apple.com.cn/shop/bag', '购物袋 - Apple', [])
    assert not loading.bag_ready
    loading.elements = [element('你的购物袋中没有商品。', 'AXHeading')]
    assert loading.bag_ready

def test_checkout_waits_for_loaded_stage_before_deciding(setup):
    clock, make = setup
    review = state('iPhone Duo 512GB 星光白色','总计 RMB 17,999','支付宝','立即下单')
    driver = Driver(lambda: review if clock() >= 10 else state('loading'))
    assert make(driver).checkout(submit=False) == 'review_ready'
    assert 10 <= clock() < 12
    assert not driver.clicks

def test_submission_requires_authorized_price_limit(setup):
    clock, make = setup
    driver = Driver(lambda: state('立即下单'))
    engine = make(driver)
    engine.plan['max_total'] = None
    with pytest.raises(Halt, match='price limit'):
        engine.checkout(submit=True)
    assert driver.reads == 0 and not driver.clicks

class RefreshDriver(Driver):
    def __init__(self, clock, available_at, refresh_cost=0):
        self.clock, self.available_at, self.refresh_cost = clock, available_at, refresh_cost
        self.refreshes = []
        self.url = 'https://www.apple.com.cn/shop/buy-iphone/iphone-duo'
        self.selected = False
        super().__init__(self.page)
    def page(self):
        controls = [] if self.clock() < self.available_at else [element('夜空色', 'AXRadioButton', int(self.selected))]
        return State(self.url, 'Duo', controls)
    def open(self, url): self.url = url
    def refresh(self):
        self.refreshes.append(self.clock())
        self.clock.sleep(self.refresh_cost)
        self.selected = False
    def click(self, e):
        super().click(e)
        self.selected = True

def one_product():
    return {'url':'https://www.apple.com.cn/shop/buy-iphone/iphone-duo',
            'options':[{'name':'color','pattern':'^夜空色$','expected':'^夜空色$','goal':'夜空色'}]}

def test_one_second_refresh_stops_as_soon_as_option_available(setup):
    clock, make = setup
    driver = RefreshDriver(clock, available_at=2.5)
    engine = make(driver)
    engine.configure(0, one_product())
    assert driver.refreshes == [1, 2]
    assert clock() == 3 and driver.clicks == ['夜空色']

def test_ready_page_selects_without_wait_or_refresh(setup):
    clock, make = setup
    driver = RefreshDriver(clock, available_at=0)
    make(driver).configure(0, one_product())
    assert clock() == 0 and not driver.refreshes
    assert driver.clicks == ['夜空色']

def test_slow_cua_refresh_does_not_add_an_extra_one_second_sleep(setup):
    clock, make = setup
    driver = RefreshDriver(clock, available_at=3.5, refresh_cost=1.4)
    make(driver).configure(0, one_product())
    assert driver.refreshes == [1, 2.4]
    assert clock() == pytest.approx(3.8)

def test_refresh_deadline_stops_without_indefinite_loop(setup):
    clock, make = setup
    driver = RefreshDriver(clock, available_at=999)
    engine = make(driver); engine.plan['product_wait_seconds'] = 3
    with pytest.raises(Halt, match='timeout: product'):
        engine.configure(0, one_product())
    assert driver.refreshes == [1, 2]

def test_refresh_never_runs_on_checkout_or_auth(setup):
    clock, make = setup
    for url in ['https://www.apple.com.cn/shop/checkout','https://secure10.www.apple.com.cn/shop/signIn']:
        driver = RefreshDriver(clock, available_at=999); driver.url=url
        engine=make(driver)
        with pytest.raises(Halt):
            engine.product_wait(lambda s:False, one_product(), clock()+5)
        assert not driver.refreshes

def test_old_option_journal_cannot_block_reselection(setup):
    clock, make = setup
    driver = RefreshDriver(clock, available_at=0)
    engine = make(driver)
    engine.record['mutations']['product:0:color']={'kind':'option','status':'confirmed'}
    engine.configure(0, one_product(), resume=True)
    assert driver.clicks == ['夜空色'] and clock() == 0

def test_parallel_process_is_refused_before_browser_actions(tmp_path):
    from purchase_runner import journal_lock
    with journal_lock(tmp_path/'purchase.json'):
        with pytest.raises(Halt, match='Another process'):
            with journal_lock(tmp_path/'purchase.json'):
                pytest.fail('duplicate purchase process entered')

def test_alipay_domain_must_be_real_not_a_substring():
    assert state(url='https://excashier.alipay.com/pay').at_alipay
    assert not state(url='https://alipay.com.evil.example/pay').at_alipay
    assert not state(url='https://apple.com.cn/?return=alipay.com').at_alipay

def test_total_is_read_from_total_label_not_any_matching_price(setup):
    clock, make = setup
    review=state('iPhone Duo 512GB 星光白色','RMB 17,999','总计','RMB 18,001','支付宝','立即下单')
    driver=Driver(lambda:review)
    with pytest.raises(Halt,match='Final review mismatch'):
        make(driver).checkout(submit=True)
    assert not driver.clicks

def test_bag_accepts_price_change_within_budget(setup):
    clock, make = setup
    s=State('apple.com.cn/shop/bag','购物袋',[
        element('iPhone Duo 512GB 星光白色','AXHeading','2'),element('数量','AXPopUpButton','1'),
        element('总计','AXStaticText'),element('RMB 17,998','AXStaticText'), element('iPhone Duo 专用保护壳','AXHeading','3')])
    engine=make(Driver(lambda:s)); engine.verify_bag(s)
    assert engine.record['bag_verified']['total']==17998

def test_full_two_product_flow_skips_intermediate_bag_and_stops_at_alipay(tmp_path,monkeypatch):
    import purchase_runner as runner
    from types import SimpleNamespace
    products=[dict(one_product(),name='iPhone Duo 512GB '+color,
                  url='https://www.apple.com.cn/shop/buy-iphone/iphone-duo/'+str(i),
                  options=[{'name':'color','pattern':'^'+color+'$','expected':'^'+color+'$','goal':color}])
              for i,color in enumerate(['夜空色','星光白色'])]
    names=[p['name'] for p in products]
    class Flow(Driver):
        def __init__(self):
            self.current=state('你的购物袋中没有商品。',url='https://www.apple.com.cn/shop/bag')
            self.items=[]; self.product=None
            super().__init__(lambda:self.current)
        def bind(self): pass
        def open(self,url):
            if '/buy-iphone/' in url:
                self.product=next(p for p in products if p['url']==url)
                self.current=State(url,'配置',[element(self.product['options'][0]['goal'],'AXRadioButton',0),element('添加到购物袋')])
        def click(self,e):
            super().click(e); label=e['label']
            if e['role']=='AXRadioButton' and label!='支付宝': e['value']=1
            elif label=='添加到购物袋':
                self.items.append(self.product['name']); self.current=state('查看购物袋',url=self.product['url'])
            elif label=='查看购物袋':
                self.current=State('https://www.apple.com.cn/shop/bag','购物袋',
                    [element(n,'AXHeading','2') for n in self.items]+[element('数量','AXPopUpButton','1') for n in self.items]
                    +[element('总计 RMB 35,998','AXStaticText'),element('结账')])
            elif label=='结账': self.current=state('继续填写送货地址')
            elif label=='继续填写送货地址': self.current=state('继续选择付款方式')
            elif label=='继续选择付款方式':
                self.current=State('https://secure10.www.apple.com.cn/shop/checkout','付款',[element('支付宝','AXRadioButton',0),element('检查订单')])
            elif label=='支付宝': e['value']=1
            elif label=='检查订单': self.current=state(*names,'总计 RMB 35,998','支付宝','立即下单')
            elif label=='立即下单': self.current=state('等待付款','W1234567890','现在支付')
            elif label=='现在支付': self.current=State('https://excashier.alipay.com/pay','支付宝 - 收银台',[element('35998.00','AXStaticText')])
            else: raise AssertionError(label)
    driver=Flow()
    monkeypatch.setattr(runner,'Cua',lambda **kw:driver)
    monkeypatch.setattr(runner,'Jev',Picker)
    plan=tmp_path/'plan.json'; plan.write_text(json.dumps({'products':products,'product_names':names,'expected_total':35998,'max_total':36000}))
    journal=tmp_path/'purchase.json'
    args=SimpleNamespace(plan=str(plan),journal=str(journal),window_id=123,stop_at='review',resume_checkout=False,submit=True)
    runner.run(args)
    assert driver.clicks.count('查看购物袋')==1
    assert driver.clicks.count('添加到购物袋')==2
    assert driver.clicks.count('立即下单')==1
    assert json.loads(journal.read_text())['status']=='alipay_reached'
    before=list(driver.clicks)
    runner.run(args)  # Re-running the same journal never creates a second order.
    assert driver.clicks==before

def test_refresh_while_waiting_for_add_reselects_lost_options(setup):
    clock, make = setup
    class Resetting(RefreshDriver):
        def __init__(self):
            super().__init__(clock, available_at=0)
            self.capacity=False
        def page(self):
            controls=[element('夜空色','AXRadioButton',int(self.selected))]
            if self.selected:
                controls.append(element('512GB','AXRadioButton',int(self.capacity)))
            if self.refreshes and self.capacity:
                controls.append(element('添加到购物袋'))
            return State(self.url,'Duo',controls)
        def click(self,e):
            self.clicks.append(e['label'])
            if e['label']=='夜空色':self.selected=True
            else:self.capacity=True
    driver=Resetting();product=one_product()
    product['options'].append({'name':'capacity','pattern':'^512GB$','expected':'^512GB$','goal':'512GB'})
    result=make(driver).configure(0,product,require_add=True)
    assert driver.refreshes==[1]
    assert driver.clicks==['夜空色','512GB','夜空色']
    assert result.radio_selected('夜空色') and result.radio_selected('512GB')

def test_pending_submit_forbids_product_refresh(setup):
    clock, make = setup
    driver=RefreshDriver(clock,available_at=999);engine=make(driver)
    engine.record['mutations']['submit']={'status':'attempted','kind':'submit'}
    with pytest.raises(Halt,match='pending_mutation'):
        engine.product_wait(lambda s:False,one_product(),clock()+10)
    assert not driver.refreshes

def test_read_latency_is_budgeted_before_refresh_tick(setup):
    clock, make = setup
    class WithReadCost(RefreshDriver):
        def read(self):
            clock.sleep(.2)
            result=super().read();result.read_ms=200
            return result
    driver=WithReadCost(clock,available_at=2.5)
    make(driver).configure(0,one_product())
    assert len(driver.refreshes)==2
    assert driver.refreshes[1]-driver.refreshes[0]==pytest.approx(1.02)

def test_verified_entry_skips_preselected_options(setup):
    clock, make = setup
    class EntryDriver(RefreshDriver):
        def open(self,url):
            self.url=url;self.selected=True
        def page(self):
            s=super().page();s.title='购买 iPhone Duo 512GB 夜空色 - Apple';return s
    product=one_product();product.update(name='iPhone Duo 512GB 夜空色',entry_url=product['url']+'/known-sku/a')
    driver=EntryDriver(clock,available_at=0)
    make(driver).configure(0,product)
    assert driver.url==product['entry_url'] and not driver.clicks

def test_entry_outside_approved_product_is_rejected(setup):
    clock, make = setup
    product=one_product();product['entry_url']='https://evil.example/shop/buy-iphone/iphone-duo/x'
    driver=RefreshDriver(clock,available_at=0)
    with pytest.raises(Halt,match='outside the approved product'):
        make(driver).configure(0,product)
    assert not driver.clicks

def test_late_preselection_skips_jev_and_click(setup):
    clock, make = setup
    driver=Driver(lambda:State('apple.com.cn/shop/buy-iphone/iphone-duo','Duo',
        [] if clock()<1 else [element('不折抵换购','AXRadioButton',1)]))
    engine=make(driver)
    class NoPicker:
        calls=[]
        def pick(self,*args):raise AssertionError('already selected: model request is redundant')
    engine.jev=NoPicker()
    engine.action('trade','^不折抵换购$','no trade',lambda s:s.radio_selected('不折抵换购'))
    assert not driver.clicks

def test_no_refresh_during_slow_following_option_load(setup):
    clock, make = setup
    class Slow(RefreshDriver):
        capacity=False
        def page(self):
            elements=[element('夜空色','AXRadioButton',int(self.selected))]
            if clock()>=3:elements.append(element('512GB','AXRadioButton',int(self.capacity)))
            return State(self.url,'Duo',elements)
        def click(self,e):
            self.clicks.append(e['label'])
            if e['label']=='夜空色':self.selected=True
            else:self.capacity=True
    driver=Slow(clock,0);product=one_product()
    product['options'].append({'name':'capacity','pattern':'^512GB$','expected':'^512GB$','goal':'512GB'})
    make(driver).configure(0,product)
    assert not driver.refreshes and driver.clicks==['夜空色','512GB']

def test_lost_reversible_click_reselects_once_without_refresh(setup):
    clock, make = setup
    class LostFirst(RefreshDriver):
        def click(self,e):
            self.clicks.append(e['label'])
            self.selected=len(self.clicks)>1
    driver=LostFirst(clock,0);engine=make(driver)
    engine.configure(0,one_product())
    assert driver.clicks==['夜空色','夜空色'] and not driver.refreshes
    assert engine.record['events'][-1]['reversible_retries']==1

def test_short_snapshot_missing_target_falls_back_without_reclick(setup):
    clock, make = setup
    class Partial(Driver):
        full_reads = 0
        def read(self):
            s = super().read(); s.complete = False; return s
        def read_full(self):
            self.full_reads += 1
            return state('ready')
    driver = Partial(lambda: state('truncated'))
    assert make(driver).wait(lambda s: 'ready' in s.text, 'target outside prefix').text == 'ready'
    assert driver.full_reads == 1 and not driver.clicks

def test_late_control_outside_short_prefix_is_discovered(setup):
    clock, make = setup
    class Partial(Driver):
        def read(self):
            s=super().read();s.complete=False;return s
        def read_full(self):
            return state('ready' if clock() >= 3.1 else 'loading')
    driver=Partial(lambda:state('loading'))
    make(driver).wait(lambda s:'ready' in s.text,'deep control')
    assert 3.1 <= clock() <= 4.1

def test_fast_wait_reacts_within_half_second_and_budgets_read_cost(setup):
    clock, make = setup
    class Timed(Driver):
        def read(self):
            clock.sleep(.12)
            s=super().read();s.read_ms=120;return s
    driver=Timed(lambda:state('ready' if clock() >= 3.15 else 'loading'))
    engine=make(driver)
    engine.wait(lambda s:'ready' in s.text,'newly ready')
    assert 3.15 <= clock() < 3.65
    assert driver.reads <= 8
    assert engine.sleep_ms > 0

def test_short_bag_does_not_hide_extra_item(setup):
    clock, make = setup
    prefix=State('apple.com.cn/shop/bag','购物袋',[
        element('iPhone Duo 512GB 星光白色','AXHeading','2'),
        element('数量','AXPopUpButton','1'),element('总计 RMB 17,999','AXStaticText')],complete=False)
    full=State(prefix.url,prefix.title,prefix.elements + [element('iPhone Duo 512GB 夜空色','AXHeading','2'),
        element('数量','AXPopUpButton','1')])
    driver=Driver(lambda:prefix);driver.read_full=lambda:full
    engine=make(driver);engine.cached=prefix
    with pytest.raises(Halt,match='product or quantity'):
        engine.verify_bag(prefix)

def test_short_snapshot_must_not_trigger_product_reload(setup):
    clock, make = setup
    class Partial(RefreshDriver):
        def read(self):
            s=State(self.url,'Duo',[],complete=False);return s
        def read_full(self):
            return State(self.url,'Duo',[element('夜空色','AXRadioButton',1)])
    driver=Partial(clock,0)
    result=make(driver).product_wait(lambda s:s.radio_selected('夜空色'),one_product(),clock()+5)
    assert result.radio_selected('夜空色') and not driver.refreshes

def test_expected_choice_outside_prefix_is_loaded_before_jev(setup):
    clock, make = setup
    partial=State('apple.com.cn/shop/buy-iphone/iphone-duo','Duo',
                  [element('256GB','AXRadioButton',0)],complete=False)
    correct=element('512GB','AXRadioButton',0)
    full=State(partial.url,partial.title,[correct])
    driver=Driver(lambda:partial)
    driver.read_full=lambda:full
    def select(e):
        driver.clicks.append(e['label']);correct['value']=1;driver.reader=lambda:full
    driver.click=select
    make(driver).action('size','^(256GB|512GB)$','512GB',lambda s:s.radio_selected('512GB'),
                        validator=lambda e:e['label']=='512GB')
    assert driver.clicks==['512GB']

def test_sku_hydration_is_not_interrupted_by_refresh(setup):
    clock, make = setup
    class Hydrating(RefreshDriver):
        def open(self,url):self.url=url
        def page(self):
            e=element('夜空色','AXRadioButton',1);e['enabled']=clock()>=2
            return State(self.url,'iPhone Duo 512GB 夜空色',[e])
    p=one_product();p.update(name='iPhone Duo 512GB 夜空色',entry_url=p['url']+'/known/a')
    driver=Hydrating(clock,0);make(driver).configure(0,p)
    assert clock()==2 and not driver.refreshes and not driver.clicks

def test_native_menu_truncation_does_not_make_web_incomplete():
    from purchase_runner import Cua
    c=Cua();c.pid=1;c.window=2
    base=[{'element_index':0,'role':'AXTextField','label':'地址和搜索栏','value':'apple.com.cn'},
          {'element_index':1,'role':'AXWebArea'},
          {'element_index':2,'parent_index':1,'role':'AXButton','label':'继续'}]
    def response(rows):
        c.metrics.append({'tool':'get_window_state','ms':100})
        return {'elements':rows,'elements_complete':False}
    c.call=lambda *a:response(base)
    assert not c.read().complete
    c.call=lambda *a:response(base+[{'element_index':3,'role':'AXMenuBar'}])
    assert c.read().complete

def test_explicit_full_observation_does_not_read_twice(setup):
    clock, make=setup
    driver=Driver(lambda:(_ for _ in ()).throw(AssertionError('redundant short read')))
    driver.read_full=lambda:state('complete')
    assert make(driver).observe(full=True).text=='complete'

def test_refresh_fronts_exact_window_before_app_menu():
    from purchase_runner import Cua
    c=Cua();c.pid=12;c.window=34;calls=[]
    c.call=lambda name,data:calls.append((name,data))
    c.refresh()
    assert [name for name,_ in calls]==['bring_to_front','invoke_menu']
    assert all(data['pid']==12 and data['window_id']==34 for _,data in calls)
