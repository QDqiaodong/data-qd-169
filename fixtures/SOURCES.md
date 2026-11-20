原创虚构合成样例，MIT许可。无真实客户、出行记录、测量成果或他人书籍正文。

`fixtures/monitor/` 为水准变形告警工作台的自制合成示例：A 为固定基准 0，B 为未知点，参考测次 0 的 AB=0，阈值 on=5、off=2。后续批次 1/2/3 的 AB 为 6/1/7，演示告警合并、确认失效与恢复。

`fixtures/plan.json` 为水准补测计划的自制合成示例：A 为固定基准 100，B、C 为不同浮动分量中的待交付目标；候选测段 AB=4、BC=1、AC=7，另加不改变计划的低成本 BD=0.5 枝边。最低成本计划为 AB+BC，总成本 5。

`fixtures/resilience.json` 与 `fixtures/resilience-bridge.json` 为单次观测失效韧性审查的自制合成示例：前者以 A 为固定基准 0，B、C 各有两条等权观测，任一单条观测失效后位移恰等于容差 1；后者观测 A-B-C 链与 B-D、D-E 支线，移除 AB 后 B/C/D/E 分量失去基准，演示 `unavailable` 优先于超差。

`fixtures/staff/` 为水准尺零差校准交接的自制合成示例：`example1-*` 是校准场 A=0、P 未知的三条 S→S/S→T/S→U 读数与交付场 A/F 固定、B 未知的联合平差（B=49/3、代价 4/3）；`example2-*` 是固定点之间单条 S→T 读数的校准与含未观测尺 U 的交付网（B=10、C/D 绝对高程不可发布、C-D=3、代价 0）；`unanchored-*` 演示含无固定基准校准分量的合法平坦包。

`fixtures/accept/` 为竣工移交验收的自制合成示例：`triangle.json` 是契约验收样例（A 固定为 0，AB/BC/AC 读数 1/1/3、常规误差 0、预留 1，AX 读数 5、常规误差 5，XY 精确；最小预留比例 1/3，B=4/3、C=8/3、X=Y，三个工作包分别为 pass/conditional/fail）；`triangle-rejected.json` 把前三条观测的预留改为 0，批次在比例 1 下仍冲突而被拒绝；`exact-full-reserve.json` 的最小比例恰为 1；`floating.json` 含悬浮分量，绝对条款与相对条款结论不同；`benchmark-conflict.json` 演示两个固定基准之间的冲突不被吸收；`zero-reserve.json` 在比例 0 下接纳、证明为 null。

`fixtures/fieldbook/` 为水准手簿归卷的自制合成示例：`example.json` 以 A、B 为 0 高程基准，两条路线各走 A→B→A，时窗分别为 [10,18] 与 [12,18]，四行记录的后视减前视高差为 +4、+5、−4、−5，归卷唯一，两条路线均可确认；`contested.json` 把 R2 时窗放宽到 [10,18]，两种交换归属都合格，两条路线均应标为 ambiguous；`missing-page.json` 中 R3 的站位于 A 与 C 之间，但手簿没有 A→C/C→A 的行，故 R3 出现在 locally_impossible_runs，而 R1、R2 各自可独立成卷。
