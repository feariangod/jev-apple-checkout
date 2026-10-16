import json
import pytest
from purchase_runner import Engine, Halt, State
from shipping_backup import ShippingBackup, ADDRESS_LABELS, CONTACT_LABELS
from test_purchase_runner import Clock, Driver, Picker, element, state


@pytest.fixture
def backup(tmp_path):
    data={'version':1,'fields':dict(zip(ADDRESS_LABELS,['测试姓','测试名','测试省 测试市 测试区','测试街道1号','测试楼层','13000000000'])),
          'contact':dict(zip(CONTACT_LABELS,['test@example.invalid','13000000000']))}
    p=tmp_path/'shipping.json';p.write_text(json.dumps(data));p.chmod(0o600)
    return ShippingBackup(p),p,data


class ShippingDriver(Driver):
    def __init__(self, backup, empty=True, contact_empty=True):
        self.backup=backup
        self.fields=[element(k,'AXTextField','' if empty else v) for k,v in backup.fields.items()]
        self.contact=[element(k,'AXTextField','' if contact_empty else v) for k,v in backup.contact.items()]
        self.editor=True;self.region_stage=None;self.writes=[]
        super().__init__(self.page)
    def page(self):
        rows=list(self.fields if self.editor else self.contact)
        if self.region_stage is not None: rows.append(element(self.backup.region[self.region_stage]))
        rows.append(element('保存更改' if self.editor else '继续选择付款方式'))
        return State('apple.com.cn/shop/checkout','address',rows)
    def fill_field(self,e,value):
        self.writes.append(e['label']);e['value']=value
    def click(self,e):
        super().click(e)
        if e['label']=='收货地址：':self.region_stage=0
        elif self.region_stage is not None:
            self.region_stage+=1
            if self.region_stage==3:
                next(e for e in self.fields if e['label']=='收货地址：')['value']=self.backup.fields['收货地址：']
                self.region_stage=None
        elif e['label']=='保存更改':self.editor=False
        else:raise AssertionError('unexpected click')


def make_engine(tmp_path,driver,backup):
    clock=Clock()
    return Engine(driver,Picker(),tmp_path/'run.json',{'expected_total':1},clock,clock.sleep,shipping=backup)


def test_full_empty_address_and_contact_restored_without_leaking_values(tmp_path,backup,capsys):
    b,p,data=backup;d=ShippingDriver(b);e=make_engine(tmp_path,d,b)
    b.restore(e)
    assert d.clicks==['收货地址：',*b.region,'保存更改']
    assert len(d.writes)==7
    assert all(x['value']==b.contact[x['label']] for x in d.contact)
    journal=e.path.read_text();stdout=capsys.readouterr().out
    assert json.loads(journal)['events'][-1]['fields_filled']==8
    assert all(v not in journal+stdout for v in [*b.fields.values(),*b.contact.values()])


def test_populated_address_summary_needs_no_writes(tmp_path,backup):
    b,p,data=backup;d=ShippingDriver(b,empty=False,contact_empty=False);d.editor=False
    e=make_engine(tmp_path,d,b);b.restore(e)
    assert d.clicks==[] and d.writes==[] and e.record['events']==[]


def test_conflicting_partial_address_is_not_mixed_with_backup(tmp_path,backup):
    b,p,data=backup;d=ShippingDriver(b);d.fields[0]['value']='不同收件人'
    with pytest.raises(Halt,match='conflict'):
        b.restore(make_engine(tmp_path,d,b))
    assert d.clicks==[] and d.writes==[]


def test_public_readable_backup_rejected(backup):
    b,p,data=backup;p.chmod(0o644)
    with pytest.raises(Halt,match='only by this user'):ShippingBackup(p)


def test_missing_region_choice_stops_without_guess_or_submit(tmp_path,backup):
    b,p,data=backup;d=ShippingDriver(b)
    original=d.reader
    d.reader=lambda:State('apple.com.cn/shop/checkout','address',[
        x for x in original().elements if x['label'] not in b.region])
    with pytest.raises(Halt,match='saved region option'):
        b.restore(make_engine(tmp_path,d,b))
    assert d.clicks==['收货地址：']


def test_changed_field_schema_stops_without_saving(tmp_path,backup):
    b,p,data=backup;d=ShippingDriver(b);d.fields.pop()
    with pytest.raises(Halt,match='form changed'):
        b.restore(make_engine(tmp_path,d,b))
    assert not d.clicks and not d.writes


def test_checkout_fills_blank_address_before_advancing(tmp_path,backup):
    b,p,data=backup
    class Flow(ShippingDriver):
        stage='delivery'
        def page(self):
            if self.stage=='delivery':return state('继续填写送货地址')
            if self.stage=='address':
                s=super().page()
                if self.editor:
                    s.elements.append(dict(element('继续选择付款方式'),enabled=False))
                return s
            if self.stage=='payment':return State('apple.com.cn/shop/checkout','payment',[
                element('支付宝','AXRadioButton',1),element('检查订单')])
            return state('iPhone Test','总计 RMB 1','支付宝','立即下单')
        def click(self,e):
            if e['label']=='继续填写送货地址':
                self.clicks.append(e['label']);self.stage='address'
            elif e['label']=='继续选择付款方式':
                assert all(x['value'] for x in self.contact)
                self.clicks.append(e['label']);self.stage='payment'
            elif e['label']=='检查订单':
                self.clicks.append(e['label']);self.stage='review'
            else:super().click(e)
    d=Flow(b);e=make_engine(tmp_path,d,b);e.plan['product_names']=['iPhone Test']
    assert e.checkout(submit=False)=='review_ready'
    assert d.clicks.count('保存更改')==1 and '立即下单' not in d.clicks
