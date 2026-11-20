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

## 单次观测失效韧性审查

`level-resilience` 面向交付负责人：正式成果仍是完整观测集上的 `level-network` 平差，随后逐条把每个观测视为暂不可用、仅移除该观测并独立重解，给出目标点是否仍可求高程、相对正式成果的精确位移及是否超过该点容差。移除不累计，基准、权重和其他输入不变；`level-network` 的输入、输出与失败行为完全不变。字段、容差与结论优先级见[数据契约的韧性审查章节](docs/contract.md#single-observation-resilience-review-v1)。

```sh
.venv/bin/level-resilience --source fixtures/resilience.json --output run/resilience.json
.venv/bin/level-resilience --source fixtures/resilience-bridge.json --output run/resilience-bridge.json
```

预期：正式高程 B=2、C=2；任一单条观测失效后网络仍可解，目标位移恰为 1 而容差同为 1，等号可接受，两目标均为 `resilient`。桥接样例中全部目标点只经 AB 连回基准 A，移除 AB 后该分量失去基准，此情景对所有目标 `unavailable`，相关结论为 `delivery_at_risk`（不可用优先于超差）。逐情景仅移除一条观测，观测与目标按 ID 稳定排序；失败退出 2 且保留旧报告。

## 水准尺零差校准交接

`level-staff` 在两组互不见面的保管方之间交接同一套水准尺的联合零差校准：校准组只导出尺号及其轮廓二次代价（保留常数项与尺间耦合），不含任何校准点、观测 ID 或行级读数；交付组只用该包与自己的测网联合平差，对全部最优解中取值唯一的查询发布精确值，其余标记 `unresolved`，绝不把未定零差或悬空基准填成零。点与观测 ID 在两组数据中各自独立，尺按实物编号对应；全部使用标准库与精确 `Fraction`。字段、包校验与发布判据见[数据契约的校准交接章节](docs/contract.md#leveling-staff-calibration-handover-v1)。

```sh
M=fixtures/staff
.venv/bin/level-staff calibrate --source $M/example1-calibration.json --output run/staff-package1.json
.venv/bin/level-staff deliver  --source $M/example1-delivery.json   --calibration run/staff-package1.json --output run/staff-report1.json
.venv/bin/level-staff calibrate --source $M/example2-calibration.json --output run/staff-package2.json
.venv/bin/level-staff deliver  --source $M/example2-delivery.json   --calibration run/staff-package2.json --output run/staff-report2.json
```

预期：例 1 的 B 高程为 `49/3`、联合最小代价 `4/3`（交付进程不读取校准原始数据）；例 2 的 B 高程为 `10`，C、D 绝对高程为 `unresolved`，C→D 高差为 `3`、D→C 为 `-3`，联合最小代价 `0`。`unanchored-*` 样例含无固定基准的校准分量，生成全零曲率的合法平坦包，依赖未识别尺方向的交付查询仍为 `unresolved`。读取包时校验版本、尺号、维度与精确数值，并拒绝非对称、负曲率、无有限最小值、最小值为负或对全体尺加同一偏移会改变代价的包；平坦方向本身合法。输入或包无效时退出 2 且保留旧输出；输入列表重排不改变产物。

## 竣工移交验收

`level-accept` 面向竣工移交：测量组先证明整批水准读数在同一预留比例下能够同时成立，验收组再判定每个工作包是否可签发。它不调用 `adjust`：最佳拟合值可能掩盖无法同时满足的读数，也可能在某条验收线上合格，却存在另一组同样相容的高程使整包不合格。字段、精确比例与矛盾链格式见[数据契约的验收章节](docs/contract.md#level-acceptance-review-v1)。

```sh
.venv/bin/level-accept --source fixtures/accept/triangle.json --output run/accept-triangle.json
.venv/bin/level-accept --source fixtures/accept/triangle-rejected.json --output run/accept-rejected.json
.venv/bin/level-accept --source fixtures/accept/floating.json --output run/accept-floating.json
```

预期：三角样例的最小预留比例为 `1/3`，所有相容高程满足 B=`4/3`、C=`8/3`、X=Y；只要求 B∈[0,1.4] 的包为 `pass`，只要求 X∈[0,4] 的包为 `conditional`，同时要求 X∈[0,4] 与 Y∈[6,10] 的包为 `fail`。预留上限改为 0 的样例批次为 `rejected`，各包标为 `not_evaluated`，测量冲突链不含任何条款。`exact-full-reserve.json` 演示比例恰为 1 时仍可接纳；`floating.json` 演示悬浮分量的绝对条款为 `conditional` 而相对条款直接 `pass`/`fail`；`benchmark-conflict.json` 演示固定基准间冲突不被吸收；`zero-reserve.json` 演示比例为 0 时证明为 `null`。全部计算为精确分数；输入或运行失败退出 2 并保留旧报告，合法的业务拒绝照常写报告。

## 离线手簿归卷

`level-fieldbook` 面向档案员：工地电子水准手簿导出后各条路线混在一起，测站记录仍保留唯一流水号、实测方向、实测时段与后视/前视读数，但路线号已丢失。命令按各路线既有的走站计划与时窗判定整批能否归卷，给出公共见证；只在这条路线的流水号序列于全部完整归卷中一致时才确认其原始行，否则给出反例说明其归属会变。它不改动 `level-network` 的输入、输出与失败语义，全部使用标准库与精确 `Fraction`。字段、候选、闭合与确认判据见[数据契约的手簿归卷章节](docs/contract.md#level-field-book-filing-v1)。

```sh
.venv/bin/level-fieldbook --source fixtures/fieldbook/example.json --output run/fieldbook.json
```

预期：A、B 均为 0；R1 时窗 `[10,18]` 只能取 `a,c`，R2 时窗 `[12,18]` 只能取 `b,d`，两条路线均 `confirmed`，闭合差为 0。`fixtures/fieldbook/contested.json` 把 R2 时窗放宽到 `[10,18]`，两条路线都应为 `ambiguous` 并各自带一份交换归属的反例，尽管闭合差始终为 0。`fixtures/fieldbook/missing-page.json` 中的 R3 没有任何候选行，整批 `infeasible`，`locally_impossible_runs` 为 `["R3"]`，其余路线本身可独立成卷。某一批次的每条路线单独看都能排出合格手簿、却争用同一批流水号时，报告同样为 `infeasible`，但 `locally_impossible_runs` 为空，提示档案员这是路线间的争用而非缺页或闭合差问题。合法但不可归卷的批次仍写报告并退出 0；输入错误退出 2 且保留旧报告。
