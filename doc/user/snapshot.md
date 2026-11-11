# 快照：snapshot record / compare / accept

只有一个环境时用：发版前把基线的响应**录成快照**，发版后用快照代替实时的基线，和候选对比。

```
api-regress snapshot record  <suite.yaml> -d DIR [--clean] [--baseline-url URL] [--only NAME ...]
api-regress snapshot compare <suite.yaml> -d DIR [--out DIR] [--fail-on LEVEL] [--format FORMATS] [--candidate-url URL] [--only NAME ...]
api-regress snapshot accept  <run-dir>    -d DIR [--only NAME ...]
```

两侧都在线时用顶层的 `run`（见 [run.md](run.md)）；和快照有关的命令都在 `snapshot` 下。`-d` / `--dir` 指定快照目录，必填。三个子命令都支持 `--log-level`、`--log-file`。本页下文的 `record` / `compare` / `accept` 指 `snapshot record` / `snapshot compare` / `snapshot accept`。典型流程：

```bash
api-regress snapshot record  suite.yaml -d snap/                 # 发版前：录基线
# …… 发版 ……
api-regress snapshot compare suite.yaml -d snap/                 # 发版后：和快照对比
api-regress snapshot accept  api-regress-out/<时间戳> -d snap/ --only order-detail   # 差异是预期的：更新基线
```

| 命令 | 需要的环境变量 | 发出的请求 |
|---|---|---|
| `record` | `targets.baseline` 和共用部分（`defaults`、`requests`） | 每条请求对基线串行请求 2 次 |
| `compare` | `targets.candidate` 和共用部分 | 每条请求对候选请求 1 次 |
| `accept` | 无（不读 suite） | 不发请求 |

所以同一份 suite 里，`record` 时不用设置候选的 token，`compare` 时不用设置基线的 token。两侧可以是同一个地址。

## record：录基线

每条请求对基线**串行请求 2 次**，**按请求合并**写进快照：

- **哪些会写入**：按 [run.md](run.md) 的请求状态规则只对基线判定。`ok` 和 `unsupported`（不是 JSON，只比状态码）写入；网络错误、401 / 403 / 429、5xx、不满足 `success` 的**不写入，保留快照里原来的 sample**。
- **写几个 sample**：通常 2 个。第 2 次请求失败、不是 JSON 或不满足 `success` 时只写第 1 个，并给出 `second_sample_unavailable` 提示。
- **其他请求原样保留**：`--only` 以外的请求、快照里有但 suite 里已经没有的请求都不动。
- **`--clean`**：先清空快照，新快照只包含本次写入的请求（没写入的请求也就不在快照里了）。快照损坏或版本不兼容时，用 `--clean` 重录。
- **一条都没写入**：快照不变，`--clean` 也一样（基线不可用或 token 过期时不会把好的快照清空，日志里有 WARNING）；快照不存在时也不会创建。
- **不稳定字段建议**：和 `run` 一样，基线两次的值不同的字段会列出来，并附上可以直接粘贴的 YAML。

stdout：

```
recorded 2 | not recorded 1
  order-list: inconclusive（基线返回 401，检查基线的 token 或限流）
  [second_sample_unavailable] order-detail: 基线第 2 次请求失败（ConnectError: …），本次没有不稳定字段建议
建议 ignore（1）：
  config: server_time（基线两次的值不同）
    - path: server_time
      ignore: true
snapshot: snap/
exit 4: 1 条请求没有写入快照
```

没写入的请求各占一行，附上原因；然后是 warning、建议。

## compare：和快照对比

基线读快照，候选请求 1 次，然后和 `run` 一样逐条对比，输出格式、run 目录、`result.json`、报告（`--format`，见 [report.md](report.md)）、退出码都和 [run.md](run.md) 相同（`result.json` 的 `command` 为 `compare`）。快照里有 2 个 sample 时照常给出不稳定字段建议；`accept` 进来的请求只有 1 个 sample，没有建议。

每条请求先看快照：

| 情况 | 请求状态 | 提示 |
|---|---|---|
| 快照里没有这条请求 | `inconclusive` | 快照里没有这条请求，请 snapshot record 或 snapshot accept |
| 指纹不一致 | `inconclusive` | 快照里这条请求的指纹不一致……，请 snapshot record 或 snapshot accept |

候选网络错误时仍然是 `error`。这两种 `inconclusive` 的请求**照样请求候选**，响应写进 run 目录的 `candidate/`，所以可以直接 `accept`。`--dir` 指向的目录不存在时，所有请求都按"快照里没有"处理。

### 请求指纹

快照里每条请求都记着一个指纹，由 suite 里**替换 `${VAR}` 之前**的这几项算出来：`method`、`path`、`query`（排序后）、`defaults.headers` 和请求级 `headers` 的**名字**。

- **不参与**：`targets` 里的 `base_url` 和 `headers`、请求头的值。所以换环境、换 token，指纹不变；`run` 和 `compare` 可以共用同一份 suite。
- **会让指纹变化**：改了 path 或 query、加减了请求头。这时 compare 报"指纹不一致"，用 `snapshot record --only NAME` 重录，或者确认候选正确后 `accept`。

## accept：把候选响应收进快照

读 run 目录（`run` 或 `compare` 的都行）里的候选响应，**按请求合并**进快照，可以用 `--only` 限定范围。快照不存在时新建。被接受的请求在快照里只有 1 个 sample（来源记为 `accept`），需要不稳定字段建议时用 `snapshot record --only NAME` 重录。

被接受的候选响应会成为新的基线，所以除了请求状态，还要检查候选本身。按顺序，第一条匹配的就是拒绝原因：

| # | 情况 | 结果 |
|---|---|---|
| 1 | 请求是 `error` | 拒绝 |
| 2 | 请求是 `inconclusive`，但不是因为快照缺失或指纹不一致（例如 token 失效、基线不可用） | 拒绝 |
| 3 | run 目录里没有这条请求的候选响应，或候选是网络错误 | 拒绝 |
| 4 | 候选返回 401 / 403 / 429 | 拒绝：检查候选的 token 或限流 |
| 5 | 候选返回 5xx | 拒绝：不能作为基线 |
| 6 | 候选不满足 `success`（包括报了 `business_error`，以及状态码变了、候选的 body 不满足 `success`） | 拒绝 |
| 7 | 其他：`ok`、`unsupported`、只因为快照而 `inconclusive` | 接受 |

所以新增的请求、改了 query 的请求，`compare` 一次后就可以直接 `accept`。判定只用 run 目录里的信息，不需要 suite 和环境变量。`result.json` 里的请求名不符合 name 的写法（例如被改成 `../x`）时，整个 run 目录按损坏处理，退出码 3，什么都不写。

stdout：

```
accepted 2 | rejected 1
  daily-report: 拒绝（候选返回 500，不能作为基线）
snapshot: snap/
exit 4: 1 条请求没有写入快照
```

## 退出码

| 码 | record | compare | accept |
|---|---|---|---|
| 0 | 全部写入 | 同 `run` | 全部接受 |
| 1 | — | 存在达到 `--fail-on` 级别的 finding | — |
| 2 | suite 有问题、缺基线的环境变量、`--only` 的 name 不存在、覆盖地址不合法、快照版本不兼容 | 同左（缺候选的环境变量） | `--only` 的 name 在 run 目录里不存在、快照版本不兼容 |
| 3 | 快照目录不能用、快照损坏、写入失败、内部错误 | 快照目录不能用、快照损坏、`--out` 不能用、写 run 目录或报告失败 | run 目录不是 api-regress 写的或损坏、快照目录不能用、快照损坏、写入失败 |
| 4 | 至少有一条请求没有写入 | 至少有一条请求是 `error` 或 `inconclusive` | 至少有一条请求被拒绝 |

同时满足多个时按 **2 > 3 > 1 > 4 > 0** 取值。快照目录不能用、损坏、版本不兼容都在**发请求之前**检查，一个请求都不会发。

"快照损坏"指 `manifest.json` 读不出来或格式不对、manifest 列出的请求文件不存在；`compare` 还会检查要用到的请求文件能不能解析。`record` / `accept` 不解析没变的请求文件，按字节原样复制：某个请求文件坏了时，`snapshot record --only NAME` 重录它就能修好。

## 快照目录

```
snap/
  manifest.json
  requests/
    order-detail.json
    order-list.json
```

- **`manifest.json`**：`schema_version`（当前 1）、`tool_version`、`fingerprint_version`、`updated_at`，以及 `requests`：每条请求的 `fingerprint`、`recorded_at`、`source`（`record` / `accept`）、`samples`（sample 数）。
- **`requests/<name>.json`**：`name`、`fingerprint`、`fingerprint_version` 和 `samples`，格式和 run 目录的 `baseline/<name>.json` 相同：URL、状态码、`Content-Type` 和 `compare_headers` 列出的响应头、body 原文（不是 UTF-8 时存 base64，字段名 `body_b64`）、耗时、重试次数。
- **不保存请求头**，token 不会落盘；其他响应头（如 `Set-Cookie`）也不保存。但**响应体原样保存**，不要把快照提交或上传到公开的地方。
- **版本**：`schema_version` 不是当前工具支持的版本时退出码 2，用 `snapshot record --clean` 重录；`fingerprint_version` 变了（工具升级改了指纹算法）时，所有请求都按指纹不一致处理。
- **请求 name 只差大小写**：在 macOS、Windows 默认的文件系统上，`Order-List.json` 和 `order-list.json` 是同一个文件，会互相覆盖。不要这样起名。

### 目录的保护与原子写入

- `--dir` 指向已存在的非空目录时，它必须是快照（含 `manifest.json`），否则拒绝写入（退出码 3），以免 `--dir .` 之类的误用把无关文件换掉。
- 写入是**原子的**：先在同级目录写好完整的新快照（`.snap.tmp-*`），再把旧快照改名为 `snap.old`、新快照改名为 `snap`，最后删掉 `snap.old`。写到一半出错或中断，原来的快照不受影响。新快照就位后删除 `snap.old` 失败（例如权限问题）只记 WARNING 日志，不影响退出码，下次运行时再清理。
- `record` / `accept` 开始时会处理上次中断留下的残留：`snap` 不见了而 `snap.old` 还在时把它恢复回来；`snap` 存在时删掉 `snap.old`；删掉 `.snap.tmp-*`。`snap.old` 不是 api-regress 留下的（没有 `manifest.json`）时报错（退出码 3），请手动处理。
- **`compare` 只读**：不删除、不改名快照目录里外的任何东西。`snap` 不见了而 `snap.old` 还在时直接读 `snap.old`（日志里有 WARNING），下次 `record` / `accept` 时再恢复。
- 不要同时对一个快照运行多个 `record` / `accept`。

## 日志

INFO 级别额外记录：快照的读取（请求数）、写入（本次条数和总条数）、残留的清理和恢复（只有 record / accept 会做）、accept 拒绝的每条请求，以及各命令的退出码和原因。和 `run` 一样，任何级别都不输出请求头的值。
