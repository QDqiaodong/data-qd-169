# data-qd-169 · 水准测量网平差

已实现可离线运行的业务闭环，字段、算法约定和未支持边界详见 [数据契约](docs/contract.md)。样例均为自制合成数据，来源及许可见 fixtures/SOURCES.md。

## 独立安装

Python >=3.11（本批实测3.12.14），标准库运行与unittest测试。使用已有uv；setuptools/wheel精确锁定。每个仓库建立独立环境，不共享site-packages。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
```

已有公共缓存时可为两条uv pip install添加 --offline；缓存缺失时需先准备构建工具，不伪称离线成功。修改源码后重新安装。

## 端到端演示

在仓库根目录执行。run/为忽略的运行输出，演示不访问网络、不启动服务或容器。

```sh
.venv/bin/level-network --source fixtures/input.json --output run/report.json
```

预期：B=304/3、C=308/3；weighted_squared_error=1/3，法方程残差全0。

输入JSON最多8MB，拒绝重复键和非有限数；完整验证成功后原子替换JSON报告。输入输出不得相同，拒绝符号链接输出；失败退出2且保留旧报告。同输入重跑结果一致。

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。

## 变形告警工作台

`level-monitor` 在不动 `level-network` 契约的前提下，为同一套精确平差增加有状态的测次与告警处置。字段、身份、版本与失败语义见[数据契约的变形告警章节](docs/contract.md#deformation-alarm-workbench-v1)。全部使用标准库，单操作者本地离线运行。

```sh
M=fixtures/monitor
.venv/bin/level-monitor init  --state run/monitor.json --source $M/config.json
.venv/bin/level-monitor apply --state run/monitor.json --source $M/epoch-1.json
.venv/bin/level-monitor apply --state run/monitor.json --source $M/epoch-2.json
.venv/bin/level-monitor apply --state run/monitor.json --source $M/epoch-3.json
.venv/bin/level-monitor apply --state run/monitor.json --source $M/confirm-second.json
.venv/bin/level-monitor apply --state run/monitor.json --source $M/correct-epoch-2-merged.json
.venv/bin/level-monitor report --state run/monitor.json --output run/monitor-merged.json
.venv/bin/level-monitor apply --state run/monitor.json --source $M/correct-epoch-2-restored.json
.venv/bin/level-monitor report --state run/monitor.json --output run/monitor-final.json
.venv/bin/level-monitor report --state run/monitor.json --output run/monitor-early.json --as-of 4
```

预期：sequence 1 为 init，之后每个成功批次加 1；合并批次后 B 仅剩 `(1,open)`，第二段确认显示 `orphaned`；恢复批次后 `(1,2)` 与 `(3,open)` 重新出现，`(3,open)` 的确认恢复为 `valid`，operator 仍为首次确认者；`--as-of 4` 始终显示两段及当时的未确认状态。撤回用 `observations: null`，重试同 `request_id` 且规范内容相同返回原 sequence，内容不同退出 2。

## 离线补测计划

`level-plan` 面向外业测量人员：现有观测所在分量缺少固定基准时，`level-network` 会拒绝平差，而 `level-plan` 从候补测段中给出可直接执行的最小成本补测集合。它只读取现有的 `points`/`observations` 结构来判定当前连通分量，绝不调用 `adjust`，也不把候选段伪装成观测或推算高程。字段、连通判定、精确成本与稳定平局规则见[数据契约的补测计划章节](docs/contract.md#offline-field-work-plan-v1)。

```sh
.venv/bin/level-plan --source fixtures/plan.json --output run/plan.json
```

预期：A 为固定基准，B、C 为不同浮动分量中的目标；候选 AB=4、BC=1、AC=7，另有不改变计划的 BD=0.5。最低成本计划为 AB+BC，`total_cost` 为 `5`，B 与 C 均经 BC 共享接入锚定侧，各目标报告所连基准 A；`segments` 均标记 `pending_collection`（待采集）。若某目标无法连到任何基准，命令仍成功退出并写出 `feasible: false` 与排序后的 `unreachable_targets`；输入无效时退出 2 且不覆盖旧报告。
