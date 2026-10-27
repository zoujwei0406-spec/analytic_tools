# Evidence Review：证据驱动的多语言程序分析流程

这是一个可运行的课程项目原型：导入源码 → 自动识别语言并选择扫描器 → 可选 SBFL → 候选排序 → AST 提取上下文 → 可选大模型调用工具复核 → HTML / JSON 报告。

| 语言 | 文件类型 | 扫描器 | 上下文提取 |
| --- | --- | --- | --- |
| C | `.c`、`.h` | Cppcheck | C 函数、直接调用候选 |
| C++ | `.cpp`、`.cc`、`.cxx`、`.hpp`、`.hh`、`.hxx`、`.C`、`.H` | Cppcheck | C++ 函数、类方法、直接调用候选 |
| Python | `.py`、`.pyi` | Bandit + Ruff + Pyright | 函数、方法、装饰器、嵌套函数 |
| JavaScript | `.js`、`.jsx`、`.mjs`、`.cjs` | ESLint | 函数、方法、箭头函数、JSX 语法 |
| TypeScript | `.ts`、`.tsx`、`.mts`、`.cts` | ESLint + TypeScript 解析器 | 带类型函数、方法、箭头函数、TSX 语法 |

可直接传入单个文件，也支持同一目录混合多种语言。`.h` 在同时导入 C++ 文件时按 C++ 解析，否则按 C 解析。Java、Go、Rust 等尚未接入，显式传入不支持的文件会报错。

目前实现的是候选定位、上下文组织和证据复核，不是一个新的完整静态分析器。排序策略是否优于基线，需要通过实验验证。

## 设计文档与流程图

- [全流程设计文档](docs/design.md)：模块、数据、静态扫描、SBFL、模型工具、逐项补查、状态与已知限制。
- [证据缺口驱动补查与程序验证方案](docs/evidence-driven-analysis.md)：首期已实现 Python 下标知识库、定向补查和成本启发式；说明设计、目的、效果与运行命令。
- [离线流程图总览](docs/flowcharts.html)：整体流程、模型审查闭环、真实覆盖采集三张图，可用浏览器打开，无需联网。

## 知识库与证据补查（新增，可选）

默认仍使用原有 `checklist` 流程。增加 `--analysis-strategy evidence` 可开启 Python 下标检查：自动生成检查点、提取必要事实、对缺口补查，并按规则给出带前提的边界结论。无需调用模型即可运行；同时支持 `--model`，程序与模型意见分别保留，检测到的越界意见冲突会反馈复核并在报告中展示。

```bash
.venv/bin/evidence-review analyze test/cases/evidence_bounds/model_smoke.py \
  --analysis-strategy evidence --evidence-policy cost --review-scope files \
  --output reports/evidence-bounds

# 主实验：同模型、同用例，比较改进前后（真实模型调用，默认三次配对）。
.venv/bin/python test/run_before_after.py --model qwen3:8b \
  --ollama-url http://127.0.0.1:11435 --model-context 40960 --repeats 3 \
  --output test/results-before-after

# 补充实验：仅比较程序补查策略，不是完整系统的前后对照。
.venv/bin/python test/run_evidence_ablation.py \
  --budgets 80 140 200 --output test/results-evidence-ablation
```

Windows 对应使用 `.\.venv\Scripts\evidence-review.exe` 与 `.\.venv\Scripts\python.exe`；将命令写在一行，或使用 PowerShell 的反引号换行。运行目录均为项目根目录。启用本地模型时，在分析命令后增加 `--model qwen3:8b`，并按服务地址设置 `--ollama-url`（工具默认端口 11434；项目内原服务常用 11435）。

策略含义：`rules` 只做局部规则；`fixed` 固定顺序补查；`gaps` 按缺口顺序；`cost` 按可解释的收益成本启发式选择。尚未训练动作价值模型或实现联合动作搜索。新增能力使用现有依赖，无需额外安装求解器。

预算参数为 `--max-evidence-actions`（默认 100）、`--evidence-budget`（相对成本，默认 200）、`--evidence-timeout`（秒，默认 30）。它们独立于模型预算；`top-k` 仍限制选中的文件或候选，报告记录未选择的访问。大批用例可提高动作预算。

报告中的 `conditional_safe/conditional_violation` 依赖明确列出的类型、环境及路径前提，不是无条件证明或动态复现。类型不明、未知副作用、不支持控制流和语法错误保留不确定。消融脚本使用相同 25 处带标签访问，比较四种策略与不同预算；不调用模型，也不宣称模型 Token 已减少。结果详见输出目录的 `summary.md` 与 `summary.json`。

### 扩展可靠性用例

新增用例位于 `test/cases/evidence_bounds/reliability/`：4 个文件、37 个带下标访问的目标函数、40 处访问，覆盖真实越界、链式和合取保护、提前 `return/continue`、别名与辅助函数修改、未知调用、变量重绑定、不可达代码，以及同一行的名称/属性/除零问题。标签保存在独立的 `manifest.json`，不作为源码输入。

```bash
# 程序规则可靠性验证，无需模型；输出目录需为空。
.venv/bin/python test/run_evidence_reliability.py --output test/results-evidence-reliability-new

# 完整工程回归，含受控模板与原生 Python 列表的语义对照。
.venv/bin/python -m pytest -q

# 扫描四类新用例；40处访问需要高于默认值的证据预算。
.venv/bin/python -m evidence_review analyze test/cases/evidence_bounds/reliability \
  --analysis-strategy evidence --review-scope files --top-k 4 \
  --max-evidence-actions 4000 --evidence-budget 8000 \
  --output reports/evidence-reliability
```

规则增加上下界合取/链式比较和简单提前退出支持；仿射规则无法处理时，新增独立计费的 `counterexample` 动作，用白名单 AST 语义搜索可达越界路径，支持普通列表的 pop/append、别名、同文件辅助函数、嵌套/步长循环、整数乘法和条件。它不执行目标 Python，也不调用任意对象方法。搜索入口列表长度 0–8、整数 -2–8，最多 256 组输入、每组 3000 步、总计 100000 步、调用深度 4；**找到反例才判风险，搜索失败保持未知，不证明安全**。见证分别记录入口参数和访问时的长度，避免把 pop 后空列表误写成入口为空。未知回调、自定义容器、语法错误等继续交给模型。规则期望符合率包含“应当未知”，不能当作缺陷检出率；报告单列真实缺陷漏检。`test/results-evidence-reliability/summary.md` 保留本次修复前后的离线结果；它与五组真实模型对照分别统计。完整策略在这批 40 处访问中为 15 处风险、13 处条件安全、12 处未知，15 处真实危险全部检出，无安全访问误报或危险误判安全。`rules` 禁用辅助函数补查，故单独保留 1 处漏检。pytest 共 290 项通过，另含 1512 个保护模板及 98 个有界路径模板的原生语义对照；这些数量不能当作独立真实项目样本。正式模型对照须先通过这批可靠性前置检查，源码冻结后再运行。

前后对照使用当前代码的 `checklist`（未启用新增证据模块）与 `evidence`（三方案首期组合），共同修复保持一致，不是回退到历史提交。两组均按文件审查，使用相同模型、输入源码、上下文上限和模型轮次上限；顺序交替，每次先预热模型。记录总耗时、模型耗时、实际请求轮次、工具调用和 Token；标签独立保存于测试脚本，不会发送给分析器。新增证据模块的计算和模型上下文开销计入新方案耗时。对照脚本默认显式设置两组 `--model-context 40960`（Ollama 的 token 上限），并记录实际加载的上下文和逐轮 token 用量；它与 `--context-chars 30000`（返回的源码证据字符预算）不是同一参数。直接使用分析命令时可同样传入 `--model-context`，未指定则沿用服务默认值。增大上下文会增加内存需求，需在机器可承受范围内设置，并给后续模型输出和历史消息留空间；单纯使用相同服务默认值不保证两组完整输入都能容纳。

`summary.md` 展示前后对比，`runs.json` 保存命令、源码/代码哈希和模型摘要，各组目录保留原始报告和轨迹。缺陷指标必须先逐条阅读报告，填写 `adjudication.json` 中每次运行的 `final` 语义裁决，再运行 `.venv/bin/python test/run_before_after.py --summarize-only --output test/results-before-after`。状态为 `tp` 时填写真实缺陷 `labels` 和 `reason`；`fp` 填写去重键 `issue_key`、可对应的安全标签及理由；本次 IndexError 范围外用 `out_of_scope` 并说明理由。不能仅凭行号匹配自动判定正确。汇总统计去重后的 TP/FP/FN、精确率、召回率、安全对照误报率，并列出新增检出与反向漏报；失败或未完成的运行不视为效率提升。已有结果目录禁止覆盖，再跑时换一个输出目录。六处教学访问只能提供初步证据，不能代表真实项目总体性能。

准确性与效率分开评价：准确性使用完整检查后的最终问题，不按时间给结论加分。效率先核对 `adjudication.json` 的 `rounds`（键是报告各 review.trace 顺序展开后的事件序号，从 0 开始，值包含该次正式提交正确提出的 `labels` 与 `reason`），再比较检出同一组真实问题所需的模型轮数。未提出正确问题的提交也填写空 `labels` 和理由。报告分别列出模型首次正确检出、包含程序结果的首次检出、最终复核轮次。程序阶段 0 表示尚未请求模型，仍有程序动作和耗时。预设完整目标 A/C/E 未被两组都达到时，不计算完整目标加速；共同检出交集的轮次比较仅作为探索性结果。不同最终检出集合的总耗时只作资源记录，不能据此声称效率提高。

此前单对六处访问的有效对照见 `test/results-before-after-context40k/summary.md`（一对六处访问）：最终正式正确检出由 1/3 增至 3/3，模型单独为 1/3 至 2/3；安全对照均无最终越界误报。共同问题的模型首次检出均为第 1 轮，完整复核由 2 轮增至 3 轮，尚无轮次减少证据。默认 16384-token 批次的新输入超限，已标记无效，不合并计分。完整语义说明见设计文档；运行报告可重建，不要求提交到 Git。

### 已知误报的专项前后对照

现有 `comparison.py` 中的 `case_e` 可直接复用：空列表时循环执行零次，是允许的正常路径；非空列表最后一次访问会越界，作为真实缺陷对照。原流程曾将正常空路径报为“空输入未处理”，新流程没有这条报警，同时保留非空越界。该已知误报专项为 **1 → 0，真实缺陷两组均检出**，不能外推为总体误报率下降。 **这只是输出差异，尚未证明新方案识别并纠正了该误报，也未证明整个新方案导致这一差异。** 新运行没有生成这条空输入报警，因而没有针对它的冲突反馈和撤回记录。

```bash
# 复用已有真实报告，不重新调用模型，也不修改原始报告。
.venv/bin/python test/run_before_after.py --false-positive-regression \
  --output test/results-before-after-context40k
```

输出 `false-positive-review.md` / `.json`，逐条判定依据保存在该目录 `adjudication.json` 各运行的 `false_positive_regression` 字段。新模式与原 `final`（IndexError 评分）分开，不能把先前范围外意见的重新分类冒充重新运行模型。它核对报告哈希、源码哈希、完成状态、已确认有效的上下文和模型摘要；缺失裁决显示待核，输入无效或报告变化则拒绝比较。换机器时先用前述命令生成真实前后报告，核对输入容量及逐条语义裁决后再使用本模式；不把旧报告当成新机器的运行结果。正常路径依据是 AST 与原生列表/range 语义，没有执行缺陷文件。


### 固定同一误报，验证是否由新方案纠正

另一个纠错对照把已有真实模型提交固定为共同起点：第 7 行安全访问误报 F2，与第 3 行真实越界 F1 同时交给两组。仅实验组增加程序证据与冲突反馈；两组沿用同一模型、源码、初始提交、复核指令和预算。后续复核调用真实模型，不将固定提交冒充本次模型生成。

```bash
.venv/bin/python test/run_before_after.py --correction-experiment \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --model-context 40960 --repeats 2 --max-rounds 2 \
  --correction-reference reports/evidence-model-smoke/report.json \
  --output test/results-fp-correction
```

需要保留含首轮原始提交的参考报告；普通源码包不包含被忽略的运行报告，换机器须一并复制该参考报告。已有结果目录拒绝覆盖。仅重新汇总时使用 `--summarize-correction --output test/results-fp-correction`。

本次两对 AB/BA 结果一致：**两组都撤回 F2，所以没有证明新方案额外消除了这条误报。** 新流程有 SAFE 证据→冲突反馈→明确引用程序校验的撤回记录；旧流程自行撤回 F2，同时错误撤回 F1；新流程保留 F1。观察到的优势是保留真实问题，不能换成“误报消除是新增方案造成”的结论。见 `test/results-fp-correction/correction-experiment.md`，完整请求与回复在同名 JSON。首轮误报原本来自 evidence 集成运行，所以此项是固定起点的机制对照，不是旧方案自然生成误报的端到端复现，也不证明三个组件各自的贡献。

### 多组完整前后对照与效果图

`test/cases/evidence_bounds/matrix/` 固定了正向偏移、负索引、分支保护、辅助函数、空循环/固定长度五组用例，每组一个真实缺陷和一个安全对照。独立标签与源码摘要保存在 `manifest.json`，不会发送给模型。每组两次重复、AB/BA 交换顺序，共 20 次真实完整分析；不执行用例源码。此处比较同一代码版本的 `checklist` / `evidence` 开关，保留两组共同缺陷修复。

```bash
.venv/bin/python test/run_comparison_matrix.py \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --model-context 40960 --repeats 2 --max-rounds 4 \
  --output test/results-comparison-matrix
```

脚本保存每次原始报告、轨迹和日志，并生成待填写的 `adjudication.json`。需逐条核对结论及触发条件，填写 `tp` / `fp` / `out_of_scope` 与理由，不能仅按报警行号自动判为正确。`tp` 关联真实缺陷标签；`fp` 使用稳定的 `issue_key` 去重，安全对照上的误报可关联安全标签；错误触发条件出现在真实缺陷行时仍记错误主张。原始报告摘要必须匹配，未完成裁决或缺失运行时不会生成完整效果图。

图表依赖只在绘图时需要，不影响分析工具运行：

```bash
.venv/bin/python -m pip install matplotlib==3.11.2
.venv/bin/python test/plot_comparison_matrix.py --output test/results-comparison-matrix
```

对已有旧流程最终误报，可再冻结其真实首轮提交，做同一起点的纠错对照（首轮回放不计为新模型调用）。此模式读取已有逐条裁决，只接受有效的旧流程误报报告；当前支持首轮 F1 为真实问题、F2 为安全对照误报的用例：

```bash
.venv/bin/python test/run_comparison_matrix.py \
  --replay-reference test/results-comparison-matrix/guard/01-checklist/report.json \
  --repeats 1 --max-rounds 2 --output test/results-comparison-matrix
```

完整请求和撤回轨迹另存 `mechanism-replay.json`，不会混入 20 次完整分析的统计；重新运行绘图脚本会把定点验证结果补入同一报告。

输出 `summary.md`（逐组代码与结果）、`summary.json`、`comparison.png`、`case-matrix.png`。中文字体默认寻找 macOS Arial Unicode、Windows 微软雅黑或 Linux Noto Sans CJK，也可用 `--font` 指定。重复运行选择新输出目录，避免覆盖证据。按真实缺陷去重统计模型＋程序结果，另列模型单独检出与所有错误主张；程序正确结果不会抵消模型误报。最终输出差异与程序反证→撤回的机制记录分开，重复运行不能冒充独立项目样本。

上一批未做本轮退步修复的 5 组 × 2 次重复结果见 `test/results-comparison-matrix/summary.md`：最终系统（程序＋模型去重）正确检出 **6/10 → 10/10**，漏检 **4 → 0**，错误问题项 **2 → 4**，无漏检且无误报的完整运行 **4/10 → 6/10**。模型最终正确检出 **6/10 → 4/10** 单独用于解释来源。`comparison.png` 以最终系统结果为主，`summary.md` 增加各组最终正确检出、漏检、错误项及整次运行正确性对照；这是标签核对后的实验综合计分，不代表已有完善的自动裁决。分支组旧误报被移除且通过额外同起点对照支持归因，但辅助函数组新增安全说明误报，正向偏移组新增错误触发条件，不能声称整体误报下降。图表、全部代码示例和真实轨迹均保留；本次补充 8 项实验校验，完整回归 196 项通过。

### 退步修复及耗时、漏检、误报复测

组合方案现在按源码哈希、访问 ID 和问题性质校正已支持的列表边界结论：SAFE 边界说明不再作为问题，VIOLATION 的触发条件由程序符号见证规范化；原始模型意见保留在 `audit.program_reconciliations`。同一行的其他问题和未知边界意见继续保留审查。程序校正条目的 `evidence_origin=program_verified`，属于程序参与的结果，不计为模型独立正确检出。矛盾撤回在 `audit.blocked_withdrawals` 留痕，不能撤回程序验证支持的风险。

模型只接收精简结论与必要前提，完整事实账本仍在报告中。没有可用程序结论或仍有未知访问时保留原有复核，不据此提前结束。程序已完成的循环边界任务由程序记录，函数其他性质仍交给模型；缺项、未决意见和未验证问题才触发补查。`stage_timings` 和 `model_request_metrics` 分别记录扫描/证据/审查耗时，以及模型请求大小、加载、输入处理和生成耗时。

```bash
.venv/bin/python test/run_comparison_matrix.py \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --model-context 40960 --repeats 2 --max-rounds 4 \
  --output test/results-optimized-comparison
# 按原始结论及触发条件完成逐条裁决后：
.venv/bin/python test/plot_comparison_matrix.py \
  --output test/results-optimized-comparison \
  --reference test/results-comparison-matrix --require-no-regression
```

此前优化批次实测报告见 `test/results-optimized-comparison/summary.md`：5 组、双重复、20 次真实分析全部完成且上下文有效；最终正确检出 **6/10 → 10/10**，漏检 **4 → 0**，误报问题项 **2 → 0**，无漏检且无误报的完整运行 **4/10 → 10/10**。平均全流程耗时 **125.95 → 41.73 秒/次**；检出相同真实问题的 6 对子集为 **176.15 → 42.07 秒/次**。模型请求总数 **22 → 16**，无新增错误主张，验收门槛通过。完整测试 **207 项通过**。以上限于本 Python 列表边界回归集，重复不是独立项目样本。

实验期间分析器代码冻结。末尾补充“无证据或未知证据保留原复核”的保守回退，用已有真实提交逐轮验证本批结果及反馈完全一致；这是协议回归，不增加真实模型样本。可重新执行：

```bash
.venv/bin/python test/run_comparison_matrix.py \
  --validate-recorded --output test/results-optimized-comparison
```

复测使用同批用例、模型和预算，在当前版本内比较 checklist/evidence；两组共享基础撤回校验。旧运行目录作为历史参考保留，不能把跨批次的耗时差直接归因为优化。输出仍为 `summary.md`、`summary.json` 和两张图，不另生成重复报告。主图展示全流程平均耗时、最终系统漏检和错误问题项；另列两组最终检出相同真实问题时的全流程耗时。完整运行包含扫描、补查、全部模型轮次和报告生成，排除两组相同的预热；不同检出集合不能直接声称同质量提速。

## 压缩包内容与使用方式

`analytic_tools.zip` 包含源码、依赖版本清单、测试用例、设计文档，以及现有的 `qwen3:8b` 模型权重与清单（`models/ollama/`）。保留设计文档引用的最近专项验收记录，其余开发报告不随包分发，可按下文命令重新生成。

压缩包不包含 Python、Node.js、Ollama 可执行文件，也不包含 `.venv`、`node_modules`、系统原生库、下载缓存或安装包。解压后，请在目标机器安装适合其操作系统和 CPU 架构的运行环境，再按下面步骤安装依赖。安装运行环境及 pip / npm 依赖通常需要联网；模型权重已经随包提供，无需再次下载。这是项目分发包，不是免安装离线运行环境。普通 Git 源码获取不会带上被 `.gitignore` 排除的模型文件。

模型文件超过 4 GB，压缩包采用 ZIP64；解压工具和目标磁盘必须支持超过 4 GB 的单个文件（不要使用 FAT32）。如果解压工具报错，可先安装 Python，使用其内置解压命令：

```powershell
# Windows PowerShell；路径按实际位置修改
py -3.13 -m zipfile -e .\analytic_tools.zip D:\Work
```

```bash
# macOS / Linux；路径按实际位置修改
python3 -m zipfile -e analytic_tools.zip /path/to/work
```

解压后应看到 `analytic_tools/README.md`、`src/`、`test/` 和 `models/ollama/` 等内容。

## 快速运行

### 先安装目标系统的运行环境

安装 [Python](https://www.python.org/downloads/) 3.11+ 和 [Node.js / npm](https://nodejs.org/)。当前项目使用 Python 3.13 验证；Node.js 版本须满足 `package.json` 的 `^20.19.0 || ^22.13.0 || >=24`，建议使用符合要求的 Node.js 24 版本。安装后重新打开终端，确认 Python、`node --version` 和 npm 可用。Linux 若提示没有 `venv` / pip 模块，请通过系统包管理器安装对应 Python 的模块。

**即使只分析 Python，也需要安装 npm 依赖，因为 Pyright 通过 Node.js 运行。** Python 依赖提供语法解析器、Cppcheck、Bandit 和 Ruff；npm 依赖提供 Pyright、ESLint 和 TypeScript 解析器。Ollama 仅在启用模型时安装，LLVM 仅在真实 C 覆盖采集时安装。

### Windows：PowerShell 安装与运行

在解压后的项目根目录执行。下列命令直接调用虚拟环境的 Python，无需修改 PowerShell 执行策略；`npm.cmd` 可避免 `npm.ps1` 被执行策略拦截：

```powershell
Set-Location "D:\Work\analytic_tools"
$env:PYTHONUTF8 = "1"
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-lock.txt
npm.cmd ci --ignore-scripts
$env:Path = "$((Resolve-Path .\.venv\Scripts).Path);$env:Path"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m evidence_review doctor

# 安装检查：仅静态分析，不执行有缺陷的用例
.\.venv\Scripts\python.exe -m evidence_review analyze .\test\cases\python\defects.py --output reports\install-check

# 分析自己的单个文件；替换源码路径即可
.\.venv\Scripts\python.exe -m evidence_review analyze .\tests\test.py --output reports\my-check

# 程序自身测试与五语言静态用例
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe test\run_suite.py --output test\results-new-machine
```

每次打开新 PowerShell 窗口后，先进入项目目录，再设置 `$env:PYTHONUTF8 = "1"` 和上面的 `$env:Path`。前者统一中文文件的默认编码，后者使扫描器能找到虚拟环境中的 `bandit.exe`、`ruff.exe`、`cppcheck.exe`。无需每次重新安装依赖。若 Python 不是 3.13，将 `py -3.13` 换成已安装且满足要求的版本。

下文其他章节的 `.venv/bin/evidence-review` 在 Windows 对应 `.\.venv\Scripts\python.exe -m evidence_review`，`.venv/bin/python` 对应 `.\.venv\Scripts\python.exe`。下文 Bash 的多行续行符 `\` 不适用于 PowerShell，请合并为一行执行。`.sh` 脚本属于 Bash 辅助入口，Windows 按本节和模型章节直接运行命令即可。

### macOS / Linux：Bash 或 Zsh 安装与运行

```bash
cd "/path/to/analytic_tools"  # 替换成解压后的项目目录
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]' -c requirements-lock.txt
npm ci --ignore-scripts
.venv/bin/python -m pip check
.venv/bin/python -m evidence_review doctor
.venv/bin/python -m evidence_review analyze test/cases/python/defects.py --output reports/install-check
```

### 安装检查与平台差异

打开 `reports/install-check/report.html`，确认 Bandit、Ruff、Pyright 均为 `completed`。这一步不需要 Ollama，也不会执行用例源码；扫描到缺陷属于预期，工具不可用或扫描不完整需要排查。`doctor` 检查基础扫描工具和可选 LLVM，目前不单独检查 Pyright。

`requirements-lock.txt` 固定 Python 依赖版本，`npm ci` 按 `package-lock.json` 安装。请保留项目目录并使用上述可编辑安装，Node 扫描器依赖项目内的 `node_modules`。不要跨机器复制 `.venv` 或 `node_modules`。

当前实际验收环境是 macOS；Windows、Linux 和 WSL2 的步骤未经过整套实机验收。锁定依赖也不意味着所有操作系统 / CPU / Python 组合都有可用的二进制包。若 pip 报 `No matching distribution` 或编译错误，应查看具体依赖，核对其平台支持和 Python 版本；Cppcheck 分发包不能安装时可按官方方式安装目标平台的 Cppcheck 并加入 PATH，再处理 pip 的对应安装约束，不能把安装失败当作成功。`cppcheck==1.5.3` 是 PyPI 分发版本，扫描器实际版本以 `doctor` / 报告为准。

仅分析 Python 时如无需 C/C++ 支持，也应先解决依赖安装问题；本项目当前未将各语言扫描器拆成独立可选安装组。Windows 如选择 WSL2，请在 WSL2 内重新安装 Python、Node、依赖和模型服务，按 Linux 步骤运行。

### 已安装后的演示

在项目根目录运行：

```bash
.venv/bin/evidence-review demo

# 多语言示例：真实扫描和 AST，不使用人工 SBFL 数据
.venv/bin/evidence-review demo --suite multilang --output reports/multilang
```

打开 `reports/demo/report.html`，结构化数据位于 `reports/demo/report.json`。

**演示中的 Cppcheck 和 AST 实际执行；SBFL 使用明确标注为 `synthetic-demo` 的人工教学数据；未调用大模型。** 示例包含一个静态越界问题，以及一个可通过测试定位的运费边界条件问题。报告不会将这些示例覆盖数据称为动态复现。

## 分析自己的文件

### 单个自定义用例

以当前的 `tests/test.py` 为例，在项目根目录运行下面的命令。将 `tests/test.py` 换成你自己的源码路径即可；路径含空格时用引号包住。

```bash
# 仅静态分析：无需启动模型，不执行待测文件
.venv/bin/evidence-review analyze tests/test.py \
  --output reports/test-py-static
```

如需本地模型一起审查，先按下文“启用真实大模型工具调用”配置系统 Ollama，指向随包的模型目录，在终端一启动 11435 服务。再在另一个终端进入项目根目录运行（下面是 Bash / Zsh 示例；PowerShell 完整命令见模型章节）：

```bash
.venv/bin/evidence-review analyze tests/test.py \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --review-scope files --model-timeout 300 \
  --output reports/test-py-model
```

如果使用已有的 Ollama 默认服务，将地址改为 `http://127.0.0.1:11434`，并确认该服务能看到 `qwen3:8b`；配置与模型目录说明见下文“启用真实大模型工具调用”。`--review-scope files` 让模型检查文件源码，包括没有静态报警的位置；300 秒是每轮请求超时，整个审查可能需要多轮。

静态结果保存在 `reports/test-py-static/report.html` 和 `report.json`；模型结果保存在 `reports/test-py-model/` 下的同名文件，另有 `model-trace.jsonl` 调用记录。待测文件存在语法错误时，错误会写入报告；若部分扫描器因此无法继续，整体状态会显示 `partial`，命令返回 2，其他已获得的诊断仍保留。

**不要用 `.venv/bin/python tests/test.py` 来启动分析。** 这条命令会直接执行待测文件，语法错误会由 Python 当场抛出，分析工具不会启动。`test/run_suite.py` 是运行预设 `test/cases/` 用例的批处理入口，分析自己的单个文件应使用上面的 `evidence-review analyze`。多行命令中，每行末尾的续行符 `\` 后面不要添加空格。

### 项目目录与其他输入方式

```bash
# 整个项目：自动按文件类型选择扫描器
.venv/bin/evidence-review analyze /path/to/project --output reports/my-project

# 单个文件（Python / C++ / JS / TS 均可）
.venv/bin/evidence-review analyze /path/to/script.py --output reports/python

# 只导入某几个文件；路径相对项目根目录
.venv/bin/evidence-review analyze /path/to/project \
  --inputs src/a.c src/b.c include/api.h --output reports/selected

# 可选编译数据库；其路径相对项目根目录
.venv/bin/evidence-review analyze /path/to/project \
  --compile-commands build/compile_commands.json
```

单独导入文件时，其他文件不会进入上下文查询索引。需要跨文件上下文时应导入整个相关目录。目录遍历跳过 `.git`、`.venv`、`node_modules`、`models`、`runtime`、`build`、`reports` 等目录。每个源码文件限制为 2 MB。

报告列出每个扫描器的状态、版本与诊断数。扫描器失败会明确记为 `error` / `unavailable`；部分工具成功、部分不可用时整体为 `partial`，不会转换为“无缺陷”。原始诊断和未进入 top-k 的候选均保留，诊断 ID 在一次运行中全局唯一。CLI 遇到工具错误或部分不可用时返回 2；发现候选问题本身不会导致非零退出。

Python 的 Bandit 负责安全规则，Ruff 运行 `E9,F63,F7,F82` 错误规则（例如未定义名称），不执行待分析源码。ESLint 使用本工具内置的推荐规则及 `no-eval`、`no-implied-eval`、`no-new-func`，不加载目标项目的可执行配置或插件。扫描器不会自动套用目标项目的忽略配置。为避免噪声，ESLint 关闭 `no-unused-vars`，TypeScript 另外关闭不理解类型的核心 `no-undef`。**当前 TypeScript 是语法和规则检查，未接入 tsc 类型检查。**

## 启用真实大模型工具调用

### 新机器：使用系统安装的 Ollama

按 [Ollama 官方安装说明](https://docs.ollama.com/quickstart) 安装与目标操作系统和架构匹配的 Ollama；Windows 另见 [Windows 说明](https://docs.ollama.com/windows)。确认 `ollama --version` 可运行。压缩包只携带模型数据，不携带 Ollama 原生运行器；模型需要足够的可用内存和磁盘，速度取决于硬件，首次加载可能较慢。

完整保留 `models/ollama/blobs/` 和 `models/ollama/manifests/`。让 Ollama 服务的 `OLLAMA_MODELS` 指向该目录，即可尝试加载随包的 `qwen3:8b`，无需把某一个 GGUF 文件单独复制到系统目录。下面在独立的本机端口 11435 启动服务，环境变量仅作用于该终端启动的进程。

#### Windows PowerShell

终端一进入解压后的项目目录，执行并保持服务运行：

```powershell
Set-Location "D:\Work\analytic_tools"
$env:OLLAMA_MODELS = (Resolve-Path .\models\ollama).Path
$env:OLLAMA_HOST = "127.0.0.1:11435"
$env:OLLAMA_NO_CLOUD = "1"
$env:OLLAMA_CONTEXT_LENGTH = "16384"
$env:OLLAMA_NUM_PARALLEL = "1"
$env:OLLAMA_MAX_LOADED_MODELS = "1"
ollama serve
```

终端二进入同一目录，检查模型并分析单个文件：

```powershell
Set-Location "D:\Work\analytic_tools"
$env:PYTHONUTF8 = "1"
$env:Path = "$((Resolve-Path .\.venv\Scripts).Path);$env:Path"
$env:OLLAMA_HOST = "127.0.0.1:11435"
ollama list
.\.venv\Scripts\python.exe -m evidence_review analyze .\tests\test.py --review-scope files --model qwen3:8b --ollama-url http://127.0.0.1:11435 --model-timeout 300 --output reports\my-model-check
```

模型批处理示例（同一终端）：

```powershell
.\.venv\Scripts\python.exe test\run_suite.py --language python --group defects --group clean --model qwen3:8b --ollama-url http://127.0.0.1:11435 --model-timeout 300 --output test\results-model-new-machine
```

#### macOS / Linux Bash 或 Zsh

终端一进入解压后的项目根目录，执行并保持服务运行：

```bash
OLLAMA_MODELS="$PWD/models/ollama" OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NO_CLOUD=1 \
  OLLAMA_CONTEXT_LENGTH=16384 OLLAMA_NUM_PARALLEL=1 \
  OLLAMA_MAX_LOADED_MODELS=1 ollama serve
```

终端二进入同一目录：

```bash
export OLLAMA_HOST=127.0.0.1:11435
ollama list
.venv/bin/python -m evidence_review analyze tests/test.py \
  --review-scope files --model qwen3:8b \
  --ollama-url http://127.0.0.1:11435 --model-timeout 300 \
  --output reports/my-model-check
```

先确认 `ollama list` 能看到 `qwen3:8b`。仅获取源码、没有模型权重时，在终端一先创建并指定模型目录，然后在连接相同服务的终端二执行 `ollama pull qwen3:8b`；本 ZIP 已包含权重时不需要这一步。分析命令不会自动下载模型。报告位于指定目录的 `report.html`、`report.json`，原始模型调用记录为 `model-trace.jsonl`。

**端口需要一致：** `evidence-review analyze` 默认连接 11434，`test/run_suite.py` 默认连接 11435；上述命令都显式使用 11435。若使用已有 11434 服务，须确认其模型目录配置，再将客户端地址一致改为 11434。端口被占用时先确认已有服务，不要重复启动占同一端口。桌面应用或系统后台服务的环境配置见 [Ollama 配置说明](https://docs.ollama.com/faq#how-do-i-configure-ollama-server)，在另一个终端设置变量不会改变已经运行的服务。

### 已准备项目内运行器时：使用本地脚本

这是保留的 Bash 辅助入口，适用于已自行准备项目内运行器的 macOS / Linux 环境；新 ZIP 不包含其所需的 `runtime/`，Windows 不使用此脚本。新机器优先按上一节安装系统 Ollama 并直接运行分析 CLI。`scripts/local-model.sh` 要求项目内已有适合本机系统和架构、具有执行权限的 `runtime/ollama/ollama`，不会自动安装运行器，也不会回退到系统 `ollama`。其配置为：

- 模型：`qwen3:8b`。
- 运行器：`runtime/ollama/ollama`。
- 权重与模型清单：`models/ollama/`，不需要移到用户默认模型目录。
- 服务地址：`http://127.0.0.1:11435`，只监听本机，关闭云端模型功能。
- 上下文配置：16,384 tokens，一次加载一个模型、处理一个请求。

在一个终端启动服务（保持这个终端运行，`Ctrl+C` 停止）：

```bash
./scripts/local-model.sh serve
```

手动准备与本机系统匹配的项目内运行器后，可用 `./scripts/local-model.sh list` 检查已有权重；仅在没有权重时才需执行 `./scripts/local-model.sh pull`。随后运行真实模型分析：

```bash
# 分析随项目提供的 C 示例；真实扫描 + AST + 本地模型，不使用人工 SBFL
./scripts/local-model.sh analyze

# 分析自己的项目
./scripts/local-model.sh analyze /path/to/project --top-k 3

# 整文件多问题审查（本地脚本默认），包括没有静态报警的文件
./scripts/local-model.sh analyze test/cases/python/defects.py

# 只复核扫描器 / SBFL 候选，复现原来的流程
./scripts/local-model.sh analyze test/cases/python/defects.py --review-scope candidates

# 用同一个本地模型分析 Python 示例；输出目录可以覆盖
./scripts/local-model.sh analyze src/evidence_review/demo_multilang/unsafe.py \
  --output reports/local-python

# 查看本地模型、交互对话
./scripts/local-model.sh list
./scripts/local-model.sh chat
```

报告默认输出到 `reports/local-model/`。`demo` 命令仍然保留为明确标注的离线教学演示；真实大模型运行请使用上面的 `analyze`。

本地脚本默认使用 `--review-scope files`：按文件组织静态诊断，同时让模型独立检查源码，并逐项列出不同问题。没有静态报警的文件也进入审查队列；`--top-k` 此时限制文件数量（默认 5），不是保证审查整个项目。直接使用 `evidence-review analyze` 仍默认 `--review-scope candidates`，只复核已有候选。

整文件审查最初提供前 80 行，较长文件在 `adaptive` 模式下可继续调用 `read_lines`。全部导入文件的源码都已提供时，只开放 `submit_review`，避免模型重复读取或猜测行号范围。报告展示实际提供的行数；超过预算、没有继续读取或没有进入 top-k 的内容不能视为已检查。语法错误不会让源码被自动修复，但模型可以继续检查文本，并区分当前阻断错误和修复前置错误后的潜在问题。每个模型问题包含位置、解释、触发条件、建议和覆盖该行的源码引用；这些仍是未经执行验证的模型意见，可能误报或漏报。

原始静态诊断数与模型问题数分别统计，两者可能重合。语法错误导致 Bandit / AST 失败时报告仍显示扫描不完整，即使模型完成了审查也不会将扫描状态改为成功。

Python 另接入 Pyright basic 类型检查（通过 `npm ci --ignore-scripts` 安装）。扫描器只分析本次导入的源码快照，使用工具生成的固定配置，不读取目标项目的 Pyright 配置，也不执行目标代码。未知参数类型和未导入的依赖可能限制检查能力；仍未加入动态测试执行器。Bandit 的 LOW 安全诊断显示为 warning，并保留原生严重程度。

整文件模型审查同时提交 `diagnostic_reviews`（每条静态诊断的复核意见）和 `function_reviews`（每个 AST 函数及模块级执行语句的处理意见；Python 纯导入由诊断任务处理）。程序校验 ID、源码是否已提供和处理记录是否齐全。缺项时保留位置校验通过的发现，要求补齐；首次“未发现问题”的单元与已有问题还会再复核一次。新增问题与已有发现合并，误判可以通过 `withdraw_findings` 指定程序生成的 F 编号和理由撤回；报告保留撤回记录。增量提交无需重复已有问题。没有实际诊断时，多余诊断处理记录不作为证据，原始参数仍保留。达到轮次或调用上限仍未完成时，报告保留部分结果并显示未完成，不会当作成功。记录完整不证明判断正确，也不代表动态验证。每轮原始工具调用实时保存为 `model-trace.jsonl`。

Python 显式 `for` / `while` 循环中的每处下标访问另建 `kind=loop_index` 审查单元，仍通过 `function_reviews` 提交，避免同函数已有其他报警就跳过边界问题。任务要求独立核对首末轮索引、偏移、空输入、保护条件及容器变化，并区分负索引、切片与字典键。即使语法错误导致函数未能索引，也会尝试从 Tree-sitter 恢复语法树提取这些任务；不会修改源码或假装语法已修复。循环上下文没有完整提供时不能提交肯定结论。下标任务在补查时再次核对，必须实际补交处理记录；HTML 展示对应表达式。此机制增加审查粒度，仍依赖模型判断，不是静态越界证明，推导式暂未建立这种独立任务。

当整文件审查只剩 `submit_review` 工具时，Ollama 使用 `format` JSON Schema 约束输出，再由程序转换为提交操作，减少字段、枚举和格式错误；仍需检索源码时使用原生工具调用。Qwen3 的普通约束提交阶段使用 `think=false`；包含循环下标任务时使用 `think=true`，并将单轮生成上限调为 8,192 tokens，以支持边界推理。实际请求配置记录在调用日志的 `transport.thinking_requested` 中。启用推理可能增加等待时间，且不保证消除误报或漏报；应结合正常对照评估。

函数的“发现问题”记录可引用函数内模型认可的静态诊断，无需将同一诊断再复制成新的模型发现。每次增量提交后，程序重新核对全部已有函数记录与当前诊断、问题清单及撤回状态。旧记录失去依据或出现矛盾时，进入待补查队列；仍有其他有效诊断或未撤回的问题时保留结论，并刷新依据。当前提交自身矛盾时，程序保留原始提交，将该条处理意见标为 `inconclusive` 并注明程序一致性检查原因；不会强迫模型给出肯定回答。预算耗尽但待补查项仍存在时，状态为 `budget_exhausted`。报告单独显示待确认数量。`completed` 表示提交和处理记录完整，不表示所有结论已经确认。

若模型下载被中断，启动服务后运行 `./scripts/local-model.sh pull` 续传。模型首次加载需要时间，后续分析可复用已加载模型。模型权重、运行器及下载缓存均已加入 `.gitignore`。

### 接入其他 Ollama 服务

接入 Ollama 原生 `/api/chat` 接口。启动 Ollama 并准备一个支持 tool calling 的模型后，替换下面的 `YOUR_TOOL_CAPABLE_MODEL`：

```bash
.venv/bin/evidence-review analyze /path/to/project \
  --model YOUR_TOOL_CAPABLE_MODEL \
  --ollama-url http://localhost:11434 \
  --top-k 3 --max-rounds 6 --max-calls 10 \
  --context-chars 14000 --output reports/with-model
```

模型运行会实时显示轮次、工具结果和提交校验错误；等待响应期间每 15 秒显示等待状态。默认 `--model-timeout 180` 是每轮请求的等待超时，配合 `--max-rounds 6` 运行时整个用例可能需要多分钟。较短的超时或较少轮次可能造成未完成提交，报告会保留这个状态。

模型名必须是服务端已经可用的模型；本项目不会自动下载模型。默认不开启模型，也不发送源码。显式指定 `--model` 后，选中的源码证据会发送至 `--ollama-url` 指定的服务。

模型可调用：

| 工具 | 实际行为 |
| --- | --- |
| `get_function(function_id)` | 从本次源码快照读取函数，单次最多 80 行 |
| `find_callers(function_id)` | 查询可能的直接调用方摘要 |
| `find_callees(function_id)` | 查询可能的被调用函数摘要 |
| `read_lines(file, start, end)` | 在导入范围内读取指定源码，单次最多 80 行；超出文件末尾或单次行数时明确截断，并返回实际范围与下一段起点 |
| `submit_review(...)` | 提交带证据引用的结构化结论 |

扫描器预先执行，其输出作为初始证据；Agent 通过只读查询补充上下文。程序调度函数，模型只提供工具名称与参数。没有任意 shell 工具，也不会自动修改源码或执行生成的补丁。

最终结论只能是 `likely_issue`（疑似问题）、`likely_false_positive`（疑似误报）、`inconclusive`（无法判断）。候选复核模式由模型填写证据编号；`likely_issue` / `likely_false_positive` 必须引用覆盖该候选主位置、全部聚合静态诊断位置及 SBFL 位置的源码。不能只引用函数开头或其他文件。长函数的目标位置超出前 80 行时，优先提供目标附近片段，再尝试提供函数开头；预算不足时仍需补读和引用，或提交 `inconclusive`。整文件审查模式由程序根据模型提交的 `file`、`line` 关联已提供且覆盖该行的源码，并从各问题汇总总引用，模型无需重复填写 `evidence_ids`。报告会标注程序关联的引用，原始工具参数仍保留在调用记录中。兼容旧模型返回的引用，但虚构编号、错误位置或引用未读取的源码仍被拒绝。这个检查仅保证引用存在和位置对应，不证明推理正确。证据 ID 在单个候选内部有效。

读取范围从第 1 行开始。`read_lines` 的 `end` 超出文件末尾时返回实际可读取的部分，`range_adjusted` 标记截断；起点超出文件、反向范围、非法参数或未导入路径仍报错，并给出可用范围。重复读取同一片段会复用已有证据，避免重复消耗上下文字符预算。

## 真正采集 SBFL 数据

需要可用且版本匹配的 `clang`、`llvm-profdata` 和 `llvm-cov`，并加入 PATH。macOS 可使用 Xcode Command Line Tools 或 LLVM，Linux 可安装发行版 LLVM 工具链；原生 Windows 的真实覆盖采集尚未完成验证，可优先在 WSL2 的 Linux 环境准备整套 LLVM 后使用。压缩包不包含 LLVM。使用 `doctor` 检查实际工具状态，普通 `analyze` 不要求 LLVM。

`collect` 会将源程序编译为覆盖插桩版本，针对每个测试单独运行、合并 profile 并导出 LCOV。测试结果由真实退出码和标准输出与预期值比较确定。

```bash
.venv/bin/evidence-review collect src/evidence_review/demo_project \
  --manifest src/evidence_review/demo_project/tests.json \
  --output reports/real-spectra.json

.venv/bin/evidence-review analyze src/evidence_review/demo_project \
  --spectra reports/real-spectra.json --output reports/real-sbfl
```

**`collect` 在本机执行输入的 C 程序，当前没有容器隔离。仅对你信任的源码显式运行该命令。** `analyze` 不会自动运行目标程序。采集器适用于不需要外部依赖的简单 C 命令行程序，暂不支持项目任意构建脚本或后台子进程工作负载。

测试清单格式：

```json
{
  "sources": ["main.c", "fees.c"],
  "tests": [
    {"name": "boundary", "args": ["100"], "expected_stdout": "0\n", "expected_exit": 0}
  ]
}
```

也可导入其他采集器生成的 JSON，格式如下。SBFL 计算与语言无关，Python/JS/TS 等语言可以导入同一格式；内置 `collect` 仍只负责简单 C 程序，不会假装已经执行其他语言的测试。源码 SHA-256 必须与本次导入文件集合完全一致，防止将旧覆盖映射到新源码：

```json
{
  "schema_version": 1,
  "origin": "your-collector",
  "source_hashes": {"main.c": "实际 SHA-256"},
  "tests": [
    {"name": "test1", "outcome": "pass", "covered": {"main.c": [4, 5, 9]}},
    {"name": "test2", "outcome": "fail", "covered": {"main.c": [4, 5, 6]}}
  ]
}
```

超时、覆盖采集失败等标记为 `error`，排除出 SBFL。只有通过测试时状态为 `no_failing_tests`，不产生有意义的失败可疑度。

## 排序与上下文策略

SBFL 使用 Ochiai：`ef / sqrt(F * (ef + ep))`。`ef` / `ep` 是覆盖某行的失败 / 通过测试数，`F` 是失败测试总数。重复执行同一行不会重复计数。函数 SBFL 分数取其内部行分数最大值。

候选取静态报警与正 SBFL 分数位置的并集，因此无静态报警的失败热点也能进入分析。使用 RRF 合并静态严重程度排序与 SBFL 排序：各渠道贡献 `1 / (60 + rank)`。并列得分共享 rank。这里的分数是优先级，不是出错概率。

支持三种可比较的上下文模式，使用相同 `--top-k`、模型与预算运行：

```bash
.venv/bin/evidence-review analyze /path/to/project --context-mode function --output reports/function
.venv/bin/evidence-review analyze /path/to/project --context-mode fixed --output reports/fixed
.venv/bin/evidence-review analyze /path/to/project --context-mode adaptive --output reports/adaptive
```

- `function`：只提供候选函数；模型仅提交结论。
- `fixed`：额外提供一层可能调用方与被调用函数；模型仅提交结论。
- `adaptive`：以候选函数起步，由模型请求更多源码。

上面未指定 `--model` 的命令只比较初始上下文提取。加入相同的 `--model` 才会实际运行模型策略，`adaptive` 才会继续扩展。

比较这三种候选函数上下文策略时，统一使用 `--review-scope candidates`；尤其通过本地脚本运行实验时要显式指定，避免与默认整文件审查混淆。

报告统计证据字符数、真实模型接口返回的 Token 用量（启用模型时）、耗时、工具调用和候选数量。字符预算并不等于总 Token 预算；多轮消息会重复发送历史。不要仅据字符减少就声称准确率提高，需使用人工标签计算误报、漏报和定位效果。

## 代码结构

```text
src/evidence_review/
  languages.py     语言识别、各语言 AST 适配、调用名称提取
  project.py       文件快照、多语言 AST、直接调用候选索引
  scanner.py       Cppcheck / Bandit / Ruff / Pyright / ESLint 适配与统一诊断
  audit.py         逐条诊断与函数审查记录、缺项和无问题结论补查
  eslint_runner.mjs 固定 ESLint 配置和 TypeScript 解析器
  coverage.py      LLVM 逐测试覆盖采集与 LCOV 解析
  sbfl.py          Ochiai 与 RRF 候选排序
  agent.py         Ollama、工具调度、预算与证据引用检查
  report.py        HTML / JSON 报告
  cli.py           命令行与流程编排
  demo_project/    小型 C 示例与测试清单
  demo_multilang/  C / C++ / Python / JavaScript / TypeScript 示例
tests/             算法、边界和真实扫描集成测试
```

## 在新机器验证安装与改动

先运行程序自身的自动化测试，再运行五种语言的 20 个静态分析用例。以下命令都在项目根目录执行，静态测试不需要模型：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python test/run_suite.py --output test/results-new-machine
```

静态用例汇总位于 `test/results-new-machine/summary.md` 和 `summary.json`；各语言 / 分组目录下保存 `report.html`、`report.json`、`run.log`。预期语法错误用例可能显示扫描 `partial`；重点检查汇总中的非预期工具错误是否为 0、五个 `clean` 对照是否没有诊断。退出码 0 表示流程按预期完成，不表示所有人工标签都已检出。

模型验收先只运行 Python 的多缺陷用例与正常对照。使用系统 Ollama 的 11434 服务时：

```bash
.venv/bin/python test/run_suite.py \
  --language python --group defects --group clean \
  --model qwen3:8b --ollama-url http://127.0.0.1:11434 \
  --model-timeout 300 --output test/results-model-new-machine
```

使用项目内脚本启动的服务时，将上面的地址改为 `http://127.0.0.1:11435`。检查汇总中非预期工具错误为 0、正常对照无模型问题，并在报告中核对处理记录、待确认项和发现内容；模型结果不保证在其他机器或模型版本上逐字相同。每轮请求的 300 秒超时不是整个测试的总时限，等待时每 15 秒输出状态。出现模型未完成时查看该用例的 `run.log` 和 `model-trace.jsonl`。

此前本机验收记录：程序测试 95 项通过；`test/results-audited/summary.md` 为五语言静态结果；`test/results-model-python-verified/` 为 Python 两个模型用例的验收结果，人工核对说明在其 `review.md`。这些是已有运行记录，新机器应输出到上面的新目录，保留旧结果供比较。完整用例、标签与退出码说明见 [test/README.md](test/README.md)。

常见安装问题：

- **Python 的 Pyright 不可用、JS / TS 扫描失败**：确认在项目根目录运行过 `npm ci --ignore-scripts`，且 Node.js 版本满足要求；仅安装 pip 依赖不够。
- **模型连接被拒绝或找不到模型**：核对服务已启动、模型已下载，且下载与分析使用同一服务地址；特别检查 11434 / 11435 是否混用。
- **本地脚本提示运行器不存在**：本 ZIP 和普通源码都不包含 Ollama 二进制文件。按“新机器：使用系统安装的 Ollama”安装并直接调用分析 CLI，或自行准备项目内运行器。
- **`doctor` 提示 LLVM 不可用**：普通静态分析可继续；需要真实 SBFL 时再安装版本匹配的 LLVM 工具链。

AST 调用关系只是语法候选，尚未处理完整类型解析、函数指针、宏展开、导入别名及动态分派。C/C++ 作为同一语言组，JS/TS 作为同一语言组，Python 单独分组；查询不会把不同组中的同名函数直接连起来，但同组内的方法名仍可能存在歧义。嵌套函数内部的调用不会记到外层函数上。静态报警复核和 SBFL 排序的总体范式已有相关工作，这个原型的贡献定位是流程实现与可比较策略，不宣称首创。


### 2026-10-25 修复后复测

完整测试290项通过，扩展可靠性集的15处真实危险全部检出，程序安全误报3→0、危险误判安全1→0。原五组20次真实模型复测全部完成：最终正确检出 6/10 → 10/10，漏检 4 → 0，误报 2 → 0，完整平均耗时 169.61 → 62.27 秒。共同修复两组共享，方案专属改动通过开关隔离；不是历史提交还原。

详见 `test/results-reliability-comparison/summary.md` 和 `docs/evidence-driven-analysis.md` 第九、十节；扩展集与真实模型对照口径不同，不混合计分。结果只限本Python边界回归集。
