# demo：本地演示服务

```
api-regress demo [--host HOST] [--port PORT] [--write-suites DIR]
```

不联网也能试用 api-regress：在本机启动一个内置的演示服务，再用自带的两个 suite 跑 `run`。

- `/v1/...` 是**基线**，`/v2/...` 是**候选**。v2 在 v1 的基础上故意改坏了几处，覆盖常见的回归类型。
- 数据是固定的，**不能配置**。它只是演示和自测用的样例服务，不是 Mock 功能：要模拟自己的接口，请用自己的测试环境。
- 只用 Python 标准库，没有额外依赖。只响应 GET / HEAD。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址。只在本机试用时不要改成 `0.0.0.0` |
| `--port` | `18765` | 监听端口，`0` 表示由系统分配。端口被占用时报错并退出，退出码 3 |
| `--write-suites DIR` | | 把示例 suite（`pass.yaml`、`regress.yaml`）写到 `DIR` 后退出，**不启动服务**。目录里已有同名文件时一个都不写，退出码 2 |

服务在前台运行，Ctrl+C 停止。每个请求记一行 INFO 日志（只有请求行和状态码）。

## 三步跑通

```bash
# 1. 写出示例 suite
api-regress demo --write-suites demo/

# 2. 另开一个终端，启动服务
api-regress demo

# 3. 跑对比
api-regress run demo/pass.yaml      # 两侧都是 /v1：全部通过，退出码 0
api-regress run demo/regress.yaml   # /v1 对 /v2：有 breaking，退出码 1
```

端口不是 18765 时，suite 通过 `DEMO_URL` 取地址：

```bash
api-regress demo --port 9000
DEMO_URL=http://127.0.0.1:9000 api-regress run demo/regress.yaml
```

## 接口

所有接口都在 `/v1` 和 `/v2` 下面，例如 `http://127.0.0.1:18765/v1/products`。业务接口统一返回：

```json
{"code": 0, "msg": "ok", "request_id": "…", "server_time": "…", "data": {…}}
```

`request_id`、`server_time` 每次请求都不一样，用来演示「建议 ignore」。数字按原文输出，`12.50` 末尾的 0 会保留。

| 接口 | 返回 | v2 改了什么 | `regress.yaml` 里报出的 finding |
|---|---|---|---|
| `GET/HEAD /health` | `{"status": "UP"}`；HEAD 只有状态码 | 无 | 无 |
| `GET /products?limit=&skip=` | 分页商品列表 `data.products` | 删了 `stock`；加了 `brand`；一个商品的 `price` 从 `89.90` 变成 `89.9`；一个商品的 `dimensions` 变成 `null` | `field_removed`、`field_added`、`scale_changed`、`type_changed` |
| `GET /products/{id}` | 单个商品（id 1–3），不存在时 404 | 同上 | （`pass.yaml` 用它演示 asserts） |
| `GET /orders/10086` | 订单详情，带 `updated_at`（每次都变）；其他 id 返回 404 | `order_id` 变成字符串；`amount` 从 `12.50` 变成 `11.50`；`status` 从 `PAID` 变成 `paid`；`items` 顺序反了，一个元素加了 `gift`；`coupon` 从 `null` 变成对象 | `type_changed`、`value_changed`（配了 `on_value_change`）、`field_added`、`assert_failed`；顺序变化（keyed）和 `coupon`（`nullable`）不报 |
| `GET /users` | 响应体本身是数组 | 少了 id=3；`email` 改名为 `mail` | `element_missing`、`field_removed`、`field_added` |
| `GET /inventory` | 库存 | HTTP 200，但 `code` 是 1001 | `business_error` |
| `GET /reports/daily` | 日报 | 返回 500 | `status_changed` |
| `GET /config` | 配置，响应头 `X-Api-Version: 1` | 响应头变成 `2` | `header_changed`（配了 `compare_headers`） |
| `GET /legacy` | 纯文本 `OK` | 无 | 无；请求状态是 `unsupported` |
| `GET /status/{code}` | 指定状态码（200–599），204 没有 body | 无 | 自己写 suite 时用 |
| `GET /delay/{ms}` | 等待后返回，最多 10000 毫秒 | 无 | 自己写 suite 时用，例如试 `timeout`、`retries` |

路径不以 `/v1`、`/v2` 开头，或者接口不存在时返回 404。

## 两个示例 suite

- **`pass.yaml`**：两侧都是 `/v1`。没有 finding，退出码 0；同时能看到：
  - `request_id`、`server_time` 出现在「建议 ignore」里，`updated_at` 已经用 `ignore` 忽略；
  - `order-not-found` 两侧都是 404，给出 `both_non_2xx` 提示；
  - `legacy` 不是 JSON，状态为 `unsupported`。
- **`regress.yaml`**：基线 `/v1`，候选 `/v2`，报出上表最后一列的 finding，退出码 1。

两个 suite 都可以直接改：加请求、改规则，观察结果怎么变。字段含义见 [suite.md](suite.md)，结果怎么看见 [run.md](run.md)。

### 试快照流程

`regress.yaml` 也能演示"只有一个环境"的流程（见 [snapshot.md](snapshot.md)）：`snapshot record` 只请求基线（`/v1`），`snapshot compare` 只请求候选（`/v2`）。

```bash
api-regress snapshot record  demo/regress.yaml -d snap/                  # 7 条都写入，退出码 0
api-regress snapshot compare demo/regress.yaml -d snap/ --out out/       # 和 run 一样报出上表的 finding，退出码 1
api-regress snapshot accept  out/ -d snap/                               # inventory（business_error）、daily-report（500）被拒，退出码 4
api-regress snapshot compare demo/regress.yaml -d snap/ --out out/       # 只剩这两条的 finding
```

## 常见问题

- **`curl` 访问 demo 拿到的不是 JSON**：开了 `HTTP_PROXY` 等代理变量时，curl 可能把本机请求也发给代理。用 `curl --noproxy '*' http://127.0.0.1:18765/v1/health`。
- **启动时报端口被占用**：换一个端口（`--port`），并按上面的方法设置 `DEMO_URL`。
