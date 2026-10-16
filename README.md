# Jev 决策 + Cua 执行：苹果中国官网购买工作流

Jev 根据当前页面的去隐私状态选择下一步，Cua 在本机 Chrome 中执行。Python 只提供允许的操作、等待条件、商品与预算约束和防重复提交。即使候选操作只有一个，也调用 Jev；无模型缓存、唯一候选捷径或模型失败后的脚本代选。

默认到订单确认页；显式 `--submit` 才提交一笔订单并打开支付宝。**不实际付款，不刷新页面，不自动重发点击。** 这是苹果网站专用的受约束智能体，不是可操作任意网页的通用模型。

## 分工

|组件|职责|
|---|---|
|Jev `jev-1.13.0`|根据当前产品、已选项、已确认加购及允许动作，选择导航、选配、加购、结账、提交或等待|
|Cua Driver|读取原生无障碍树，执行模型选定动作；通常不截图|
|本地约束层|排除未授权商品和动作，核对数量、金额和支付宝域名，记录交易意图；不会替模型选择下一步|
|用户|完成验证码、Touch ID等认证；自行决定最终付款|

下一步依据实时页面，不按固定步骤序号点击。多个合法选项同时可用时，Jev决定顺序。已满足的选项不再作为候选。没有可操作控件时本地只读等待；排队、空白加载和选配水合均不重载页面。模型选择WAIT会等待；BLOCKED或异常会停止并保存进度。

## 安装

需要 Python 3.12+、macOS Chrome，以及已安装并获得系统权限的 [Cua Driver](https://github.com/trycua/cua)。实测驱动版本0.27.0。Linux CI仅验证模拟驱动，不代表支持Linux实际桌面。

```sh
git clone https://github.com/feariangod/jev-apple-checkout.git
cd jev-apple-checkout
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
cua-driver permissions status
cua-driver call list_windows '{}'
```

通过终端或密码管理器设置你自己的 `TYPESAFE_API_KEY`。可选 `TYPESAFE_MODEL`、`CUA_DRIVER`。程序不读取Codex全局配置或其他应用凭据；`.env.example`不会自动加载。无需Browser Harness、Playwright或额外文字生成模型。

## 连续运行

先确认官网实际配置、价格、数量和授权预算，再将 `plans/*.example.json` 复制到自己的计划文件。示例商品路径和日期是历史数据，不保证仍有效。保持电脑唤醒解锁、Chrome登录，起始购物袋必须为空。`WINDOW_ID`替换为刚观察到的Chrome窗口编号。

```sh
# 默认只到确认页
python jev_agent.py --plan plans/duo-two.example.json \
  --journal run/purchase.json --window-id WINDOW_ID

# 只有明确授权创建真实订单时才加 --submit
python jev_agent.py --plan plans/duo-two.example.json \
  --journal run/purchase.json --window-id WINDOW_ID --submit
```

不需要先跑一遍选配测试才能正式运行。一次正常路径是：模型选择检查空购物袋、选择要配置的商品和可执行选项、各加购一次、查看合并购物袋、结账、沿用地址、支付宝、检查订单、提交、打开支付宝。每个选择都来自一次Jev响应，本地约束可拒绝不合法选择。

`auth_waiting`出现时，操作员立即通知用户。用户在苹果页面认证后同一进程自动续跑，默认最多累计等待300秒，可用 `--auth-wait-seconds`设置。只监控简短输出，不由助手逐屏接管、不并行操纵同一窗口。

## 停止与恢复

模型或页面失败就保留同一journal，修复具体原因后用同一命令恢复。加购/提交已尝试而结果未确认时停止，必须先检查真实订单或购物袋；不能新建journal绕过，也不能自动删除未确认意图。订单完成后同一journal直接退出，不再次操作。

正常人工认证可以同进程接续；人工改变购物袋、切页或操作订单时应先停止执行器，再核实状态。不假设任意人工改变都能自动修复。不同journal的锁不互斥，禁止两个进程同时操作同一窗口或购物袋。

`purchase_runner.py`保留共用驱动、验证原语及历史回归测试，正式入口是 `jev_agent.py` / `jev-apple-checkout`。旧每秒刷新参数、刷新重选和丢失点击后重点击不再执行。

## 地址与隐私

官网地址和联系方式完整时直接继续，不要求用户重复确认。可通过 `--shipping-backup /outside/repository/shipping.json`提供本机私有备用信息；模型仅决定是否请求补填，实际字段值由本机绑定到原生控件，不发送给模型。配置必须归当前用户所有且权限0600。区域控件或非空内容冲突则停止，不猜地址。

备用格式为 `version: 1`，`fields`包含姓氏、名字、收货地址：、详细地址、附加详细地址、收件人手机号码；`contact`包含电子邮件地址、联系人手机号码。省市区用空格分隔为三段。不得保存掩码作为完整信息。公开仓库不含任何真实备用配置。

模型只收到商品计划、已选配置的布尔状态、脱敏动作标签及已完成操作；不收到原始页面全文、账户标题、地址、输入框值、订单号或带令牌URL。journal仍可能包含订单号，属于本机私有文件，不能上传。不要公开截图、付款链接、原始日志、API密钥或浏览器状态。

## 定时启动

外部调度器提前启动本地入口；它不是后台服务。确保日期、时区、预算及购买授权正确。

```sh
python run_at_release.py --plan plans/duo-two.example.json --journal run/release.json --check
python run_at_release.py --plan plans/duo-two.example.json --journal run/release.json \
  --window-id WINDOW_ID --submit
```

首次导航到计划页面由Jev决策，开售时间只代表允许开始，不保证官网已开放。官方排队不刷新；超时不循环重启。计时入口拒绝在开售前超过30分钟或开售后30分钟才启动；已有进程的等待另有期限，不保证完成时间。

## 验证与实测口径

每次模型响应打印 `jev_decision`，journal记录模型版本、延迟及调用数，不保留请求全文。报告必须区分总墙钟时间、执行时间、认证等待、修复间隔及人工操作。

之前110.749秒的两台Duo测试是确定性脚本+Cua，Jev调用0次，不能作为本版本Jev决策成绩。模拟测试验证模型决定不同选配顺序、唯一候选仍调用模型、失败无脚本后备、地址隐私及一次提交。测试通过不等于官网始终可购买。

源代码采用MIT许可证，第三方署名见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

最新独立实测：**115.804秒到支付宝，17次真实Jev调用，0刷新、1次提交，未付款**。其中认证等待11.812秒、模型请求合计13.737秒，均已包含在总耗时内。详细口径见 [实测记录](docs/benchmark.md)。
