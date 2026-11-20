原创虚构合成样例，MIT许可。无真实客户、出行记录、测量成果或他人书籍正文。

`fixtures/monitor/` 为水准变形告警工作台的自制合成示例：A 为固定基准 0，B 为未知点，参考测次 0 的 AB=0，阈值 on=5、off=2。后续批次 1/2/3 的 AB 为 6/1/7，演示告警合并、确认失效与恢复。

`fixtures/plan.json` 为水准补测计划的自制合成示例：A 为固定基准 100，B、C 为不同浮动分量中的待交付目标；候选测段 AB=4、BC=1、AC=7，另加不改变计划的低成本 BD=0.5 枝边。最低成本计划为 AB+BC，总成本 5。

`fixtures/resilience.json` 与 `fixtures/resilience-bridge.json` 为单次观测失效韧性审查的自制合成示例：前者以 A 为固定基准 0，B、C 各有两条等权观测，任一单条观测失效后位移恰等于容差 1；后者观测 A-B-C 链与 B-D、D-E 支线，移除 AB 后 B/C/D/E 分量失去基准，演示 `unavailable` 优先于超差。
