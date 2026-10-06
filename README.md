# Jev + Cua 苹果中国官网购买工作流

在本机已登录的 Chrome 中，用 Jev 选择页面选项、Cua 执行操作、Python 连续控制选配和结账。默认停在订单确认页；显式传入 `--submit` 才会提交订单并打开支付宝，**不会实际付款**。

这是苹果网站专用执行器，并非上游 Jev Agent 的完整复制。支持 macOS 原生 Cua；未验证 Windows/Linux 的真实浏览器操作。测试在其他系统上只运行模拟驱动。

## 组件与分工

| 组件 | 分工 |
|---|---|
| Jev `jev-1.13.0` | 通过 TypeSafe API，在当前页面候选控件中选择；相同候选和目标缓存结果 |
| Cua Driver | 通过 macOS 无障碍树读取、点击和导航已登录的 Chrome，通常不截屏 |
| `purchase_runner.py` | 连续执行、等待、预算与商品检查、一次提交、进度恢复 |
| `run_at_release.py` | 提前运行后按系统时钟等待开售时间，不是后台调度服务 |
| Codex 或其他操作员 | 提前启动程序、处理登录/验证码及异常，不逐次接管点击 |
| 用户 | 完成必要认证，最终在支付宝自行付款 |

不需要 Browser Harness、Playwright、额外文字生成模型或 TypeSafe MCP 服务。API 密钥必须由你自己提供。模型看到的是当前候选控件与目标，程序不主动发送收货地址、密码或验证码。

## 安装

需要 Python 3.12+、本机 Chrome、已安装并获 macOS 所需权限的 `cua-driver`。驱动请从 [Cua 官方项目](https://github.com/trycua/cua) 按当前安装说明获取。本机实测驱动版本为 0.27.0；新版本兼容性需自行验证。此项目不安装、升级驱动或修改系统权限。

```sh
git clone https://github.com/feariangod/jev-apple-checkout.git
cd jev-apple-checkout
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cua-driver --help
cua-driver permissions status
python -m pytest -q
```

通过当前终端或密码管理器设置 `TYPESAFE_API_KEY`。可选环境变量：`TYPESAFE_MODEL`（默认 `jev-1.13.0`）、`CUA_DRIVER`（驱动可执行文件路径）。`.env.example` 仅作格式参考，不会自动加载；不要提交真实 `.env`。程序不会读取 Codex 全局凭据文件。

## 准备计划

`plans/*.example.json` 是历史测试配置示例，包含商品路径、页面标签、预算和 SKU 直达链接，**不保证未来仍有效或有库存**。先核对实际官网，再复制并编辑自己的计划。价格、开售日期、数量均须由操作员确认。改变计划后不能继续使用旧计划的交易进度。

每次运行前：保持电脑唤醒且解锁；打开并登录苹果中国商店；准备收货地址；确认购物袋为空。列出窗口并选定准确的 Chrome 窗口 ID：

```sh
cua-driver call list_windows '{}'
```

以下 `WINDOW_ID` 必须替换为本次观察到的编号，不使用历史窗口编号。

## 执行

先只测选配，不加入购物袋：

```sh
python purchase_runner.py --plan plans/pro-two.example.json \
  --journal run/config-test.json --window-id WINDOW_ID --stop-at configure
```

完整选配、加购、进入订单确认页，**不提交订单**：

```sh
python purchase_runner.py --plan plans/pro-two.example.json \
  --journal run/purchase.json --window-id WINDOW_ID
```

只有明确决定新建真实订单时，才使用以下命令。它会提交计划中的一笔订单，并停在支付宝，不付款：

```sh
python purchase_runner.py --plan plans/pro-two.example.json \
  --journal run/purchase.json --window-id WINDOW_ID --submit
```

真实路径：SKU 链接 → 选配第一台 → 加购 → 选配第二台 → 加购 → 查看一次购物袋 → 自动检查商品/数量/预算 → 结账 → 已保存地址 → 支付宝 → 检查订单 → 提交一次 → 打开支付宝 → 停止。

已选中的项目直接跳过；明确且唯一的按钮直接点击；需要选项判断才调用 Jev。同一页面读取服务于前后步骤。不同测试模式各用独立测试 journal；一旦开始某笔真实购买，所有恢复必须沿用该购买 journal，不能另建进度重试。

## 登录与故障恢复

遇到登录/验证码时在苹果页面完成认证，再用**同一计划、同一 journal**重跑同一命令。登录过商店不保证安全结账不再认证。勿输出密码、验证码或原始个人页面数据。

加购和提交前持久化意图；结果不明确时只核实结果，不盲目重发。不要删除 journal、手动改成“成功”、启动另一份购买。进程锁仅防止同一 journal 并发；操作员必须避免不同 journal/任务同时操作同一购物袋或窗口。

## 定时开售

提前检查登录与窗口；用 Codex 线程自动化等调度器在开售前约 10 分钟启动下列进程。此仓库不会创建自动化或常驻任务，也不会自动保持电脑唤醒。

```sh
python run_at_release.py --plan plans/duo-two.example.json \
  --journal run/release-purchase.json --check

# 真正执行：必须提前确认日期、预算及购买授权
python run_at_release.py --plan plans/duo-two.example.json \
  --journal run/release-purchase.json --window-id WINDOW_ID --submit
```

`release_at` 必须含时区。提前超过 30 分钟或晚于开售 30 分钟拒绝新启动；睡眠导致错过窗口时也拒绝补买。计时精度、网络、库存和认证均不能保证。`--submit` 未传入时仍只到确认页。

商品尚不可选时目标每秒刷新；已可选后开始选配。结账、认证、未知提交结果及支付宝不周期刷新。高拥堵下每秒刷新可能打断慢响应，可调整计划的 `refresh_interval_seconds`。

## 实测边界

历史两台 Pro 完整测试：从执行器进程启动到支付宝 83.188 秒，无登录暂停，已提交、未付款。Duo 只验证过选配和未开放预购状态，不能将其约 10–15 秒的成绩当成完整下单成绩。原始订单、个人运行日志均未包含在仓库。

Cua 曾出现窗口定位异常、菜单作用于其他活动窗口及空响应。执行器在导航/刷新前置前目标窗口，但这不是完全消除驱动问题的保证。发生异常先检查准确窗口和现有结果，再恢复；不为测速重复提交订单。修改此仓库不代表原机上的定时任务已自动切换到新版本。

实现、示例与模拟测试可审查；测试通过不等于真实网站随时可购买。上游代码来源与许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证与贡献

工作流以 MIT 许可证提供，可修改复用；第三方代码保留原始署名。提交 Issue 时请先删除订单号、收货信息、支付链接和 API 密钥，不要上传原始浏览器快照或 journal。
