import json
import pytest
from jev_agent import Agent, Policy
from purchase_runner import Halt, State
from test_purchase_runner import Clock, Driver, element, state, one_product


def test_policy_calls_real_adapter_even_for_one_candidate_and_redacts_page():
    class Model:
        calls=[]
        def choose(self,page,goal,history):
            assert 'PRIVATE' not in json.dumps(page)
            assert len(page['actions'])==2
            return {'choice':'0','latency_ms':2,'model':'test'}
    model=Model();p=Policy(model)
    offer={'key':'checkout','label':'结账'}
    assert p.decide(State('https://apple.com.cn/?PRIVATE','PRIVATE',[element('PRIVATE')]),[offer],{},'test') is offer
    assert len(p.calls)==1
    assert p.decide(state('PRIVATE'),[offer],{},'test') is offer
    assert len(p.calls)==2  # No cached model bypass.


def test_policy_failure_has_no_script_fallback():
    class Model:
        calls=[]
        def choose(self,*args):raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError):Policy(Model()).decide(state('test'),[{'label':'buy'}],{},'test')


def test_model_chooses_capacity_before_color_and_full_order_runs_once(tmp_path):
    products=[]
    for i,color in enumerate(['夜空色','星光白色']):
        p=one_product();p.update(name='iPhone Duo 512GB '+color,url=p['url']+'/'+str(i))
        p['options']=[{'name':'color','pattern':'^'+color+'$','expected':'^'+color+'$','goal':color},
                      {'name':'capacity','pattern':'^512GB$','expected':'^512GB$','goal':'512GB'}]
        products.append(p)
    names=[p['name'] for p in products]
    class Browser(Driver):
        def __init__(self):
            self.current=State('about:blank','blank',[]);self.items=[];self.index=None
            super().__init__(lambda:self.current)
        def open(self,url):
            if '/shop/bag' in url:self.current=state('你的购物袋中没有商品。',url=url)
            else:
                self.index=next(i for i,p in enumerate(products) if p['url']==url)
                self.current=State(url,names[self.index],[element(products[self.index]['options'][0]['goal'],'AXRadioButton',0),element('512GB','AXRadioButton',0),element('添加到购物袋')])
        def click(self,e):
            super().click(e);label=e['label']
            if e['role']=='AXRadioButton':e['value']=1
            elif label=='添加到购物袋':
                self.items.append(names[self.index]);self.current=state('查看购物袋',url=products[self.index]['url'])
            elif label=='查看购物袋':
                self.current=State('https://apple.com.cn/shop/bag','bag',[element(n,'AXHeading','2') for n in self.items]+[element('数量','AXPopUpButton','1') for _ in self.items]+[element('总计 RMB 35,998'),element('结账')])
            elif label=='结账':self.current=state('继续填写送货地址')
            elif label=='继续填写送货地址':self.current=state('继续选择付款方式')
            elif label=='继续选择付款方式':self.current=State('https://apple.com.cn/shop/checkout','payment',[element('支付宝','AXRadioButton',0),element('检查订单')])
            elif label=='检查订单':self.current=state(*names,'支付宝','总计 RMB 35,998','立即下单')
            elif label=='立即下单':self.current=state('等待付款','现在支付')
            elif label=='现在支付':self.current=State('https://example.alipay.com/pay','支付宝',[element('35998.00')])
            else:raise AssertionError(label)
    class Model:
        calls=[]
        def choose(self,page,goal,history):
            options=[a for a in page['actions'] if a['id']!='WAIT']
            picked=next((a for a in options if a['label'].startswith('512GB')),options[0])
            return {'choice':picked['id'],'latency_ms':1,'model':'fake-policy'}
    driver=Browser();clock=Clock();model=Model()
    e=Agent(driver,Policy(model),tmp_path/'j.json',{'products':products,'product_names':names,'max_total':36000,'expected_total':35998},clock,clock.sleep)
    assert e.loop(True)=='alipay_reached'
    assert driver.clicks[:2]==['512GB','夜空色']
    assert driver.clicks.count('添加到购物袋')==2 and driver.clicks.count('立即下单')==1
    assert len(model.calls)==len(driver.clicks)+3  # Includes initial bag and both product navigations.
    assert e.record['mutations']['submit']['status']=='confirmed'
