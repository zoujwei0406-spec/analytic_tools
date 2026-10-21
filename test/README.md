# 多语言程序分析用例

本目录包含 **5 种语言、20 个源码文件、82 个预期问题或条件性风险标签**，用于观察扫描器和本地模型的能力与遗漏。用例刻意包含错误；批处理工具只读取、扫描源码，不导入或执行用例。

`test/` 是分析素材目录，`tests/` 是原有 pytest 测试目录，两者用途不同。

| 目录 | 语言 | 预期标签数 | 主要问题 |
|---|---|---:|---|
| `cases/c/` | C | 17 | 数组越界、空指针、释放后读取、重复释放、内存泄漏、未初始化变量、整数除零、二分查找死循环 |
| `cases/cpp/` | C++ | 17 | new/delete 不匹配、悬空引用、迭代器失效、内存错误、去重不完整、整数截断 |
| `cases/python/` | Python | 16 | eval、反序列化、命令注入风险、未定义名称、错误方法、类型混用、可变默认参数、列表别名 |
| `cases/javascript/` | JavaScript | 16 | 动态执行、常量赋值、重复键、不可达代码、finally 吞异常、异步 Promise executor、排序和回调错误 |
| `cases/typescript/` | TypeScript | 16 | 赋值/返回类型不匹配、缺失属性、可空对象、不可调用的数值、错误方法，以及 JavaScript 语义问题 |

每种语言各有四个文件：

- `defects.*`：语法正确，集中放置多种安全、类型或运行时问题，避免一开始就被语法错误阻断。
- `logic.*`：边界、状态、数值计算等问题，通常不会被当前规则全部检出。需要业务要求的地方写有普通需求注释，例如“满 100 免邮”。
- `syntax_error.*`：故意包含语法错误，观察 AST 和扫描器失败时是否保留报警、明确标记不完整。
- `clean.*`：正常对照，用于观察扫描器是否给出额外报警；不作为形式化正确性证明。

源码中没有写出答案或 `BUG` 注释。预期位置、原因和触发条件独立存储在 `expected.json`，不会随单个源码文件送入模型。`build_manifest.py` 用源码锚点生成行号，修改源码后可重新运行，避免标签错位。

## 已生成的静态测试记录

查看 [results/summary.md](results/summary.md)，以及各用例的 `report.html`、`report.json` 和 `run.log`。这是实际运行扫描器和 AST 的结果，**未调用模型、未执行用例、未采集覆盖数据**。

在项目根目录运行全部静态测试：

```bash
.venv/bin/python test/build_manifest.py
.venv/bin/python test/run_suite.py
```

只运行 Python 的多缺陷文件：

```bash
.venv/bin/python test/run_suite.py --language python --group defects
```

为了保留全量静态基线，子集测试建议单独指定输出目录：

```bash
.venv/bin/python test/run_suite.py --language python --group defects \
  --output test/results-python
```

## 使用本地模型

新机器先按主目录 [README 的安装与模型配置步骤](../README.md) 准备依赖和模型。下面的脚本示例要求已准备项目内运行器，批处理默认连接 `127.0.0.1:11435`。如果使用系统安装的 Ollama 默认服务，请直接运行批处理，并为所有带 `--model` 的命令加上 `--ollama-url http://127.0.0.1:11434`，无需使用项目内脚本。

先在一个终端启动已经下载的模型服务：

```bash
./scripts/local-model.sh serve
```

在另一个终端运行一个用例，结果全部保存在 `test/` 下：

```bash
.venv/bin/python test/run_suite.py --language python --group defects \
  --model qwen3:8b --output test/results-model-python
```

扫描五种语言的 `defects` 文件，或扫描全部 20 个文件：

```bash
.venv/bin/python test/run_suite.py --group defects \
  --model qwen3:8b --output test/results-model-defects

.venv/bin/python test/run_suite.py \
  --model qwen3:8b --output test/results-model-all
```

这两条命令需要逐文件运行本地模型，耗时明显高于静态扫描。批处理每次只传入一个文件，不会被默认 `--top-k 5` 限制成整个项目只检查五个文件。`defects` 和 `logic` 文件都在 80 行以内，整文件模式会提供全部源码。

终端和各用例的 `run.log` 会实时显示进度。模型请求尚未返回时，每 15 秒显示一次等待状态；这说明客户端仍在等待，不证明服务端已生成多少内容。响应返回后会显示工具查询、提交被拒绝的原因或结论被接受。日志在运行中也可以直接打开。

整文件模式的问题引用由程序根据 `file`、`line` 关联已提供源码，模型无需填写 E1/E2 等编号。一个用例的全部源码已提供时，只允许提交结果；较长文件仍可分段读取，超出末尾的结束行会被截断并明确标记。虚构行号或未读取的源码仍不会通过校验。

整文件审查还要求逐条诊断和逐函数/模块的处理记录。提交缺项时，程序保留已有问题并要求补查；首次“未发现问题”的单元会再核对一次，因此可能需要更多时间。报告包含这些处理意见。源码提供完整、记录齐全都不保证没有漏报。Python 静态扫描新增 Pyright basic 类型检查，使用导入源码快照和固定配置，需先在工具目录运行 `npm ci --ignore-scripts`。

回归结果建议输出到新目录，保留旧报告用于比较：

```bash
.venv/bin/python test/run_suite.py --output test/results-audited
.venv/bin/python test/run_suite.py --language python --group defects --group clean --model qwen3:8b \
  --model-timeout 300 --output test/results-model-python-verified
```

“静态规则未匹配的预期标签”只统计扫描器匹配，不包含模型语义匹配；模型可能已经报告其中的问题，需查看对应模型报告。

补查也复核已有发现，误判允许带理由撤回；撤回条目保留在报告中。原始工具调用实时写入各用例的 `model-trace.jsonl`，不必等全部完成才能排查格式或推理问题。`results-model-python-audit-development` / `results-model-python-audited` 为开发中间试运行，完整验收以 `results-model-python-verified` 为准。

仅剩提交操作时使用约束 JSON 输出，Qwen3 通常关闭显式思考输出（`think=false`）；包含循环下标独立任务时改用 `think=true` 并提高生成上限，可能需要更多时间。日志记录实际输出模式、思考模式、结束原因和 Token 数；本次与旧报告的比较反映整套流程和提交配置的变化，不是单独证明某条提示词的贡献。循环边界专项用例见 [regressions/python-loop-boundaries/README.md](regressions/python-loop-boundaries/README.md)。

默认最多 6 轮，每轮请求等待超时 180 秒，整个用例可能需要多分钟，180 秒不是总时限。可缩小轮次、设置单轮等待时限：

```bash
.venv/bin/python test/run_suite.py --language python --group defects \
  --model qwen3:8b --max-rounds 3 --model-timeout 120 \
  --output test/results-model-python
```

较少的轮次或较短的超时可能导致模型来不及完成，不保证检出能力相同。按 `Ctrl+C` 可中断批处理，已产生的日志会保留。旧版已经启动的进程需要结束后重新运行，才能使用实时进度。

也可以直接分析一个文件：

```bash
./scripts/local-model.sh analyze test/cases/typescript/defects.ts \
  --output test/results-single-typescript
```

## 如何阅读结果

- **预期标签数**：人工设计的问题和风险数，不等于扫描器诊断数。有的诊断只是风格建议，有的问题会产生多个诊断。
- **静态规则/位置匹配**：只有规则 ID 和报告中的实际源码位置都匹配标签时才计数。规则别名涵盖当前版本可见的同类规则；不是关键词猜测或按附近行号自动认领问题。
- **没有自动匹配的标签**：需要检查原始诊断和预期条件；可能是漏报、规则未接入，或诊断位置/规则名不同，不能直接宣称都是漏报。
- **模型问题数**：保留模型输出，不会按数量自动匹配答案。需要核对内容、位置、触发条件与建议，才能计算准确率和召回率。
- **语法错误组**：Python 的 Bandit 可能无法完成扫描，报告保留 `partial`。用例程序不会因为扫描完成就被认为可以运行。
- **条件性安全风险**：例如 eval/pickle/Shell 接收不可信输入、普通随机数用于安全令牌；条件不成立时不能直接认定存在可利用漏洞。
- **JavaScript/TypeScript 的边界语义**：数组越界读取通常得到 `undefined`，空数组均值为 `NaN`，不能照搬 Python 的异常结论。
- **TypeScript 类型检查**：当前工具没有接入 `tsc`，特意保留了六个仅靠当前 ESLint 很可能遗漏的类型问题。可空检查还依赖 `strictNullChecks`，数组索引类型检查依赖 `noUncheckedIndexedAccess`。
- **C++ 引用返回值索引**：当前 `int &local_reference()` 用例的 Tree-sitter 语法树可解析，但函数名提取器不能识别这个声明。原始索引提示保留在汇总中；整文件模型模式仍能读到该函数文本。这个用例用于持续暴露该限制。

批处理退出码：`0` 表示用例分析完成（包括预期的语法阻断），不表示问题全部检出；`1` 表示正常对照产生了诊断，需检查是风格建议还是误报；`2` 表示非预期的工具不可用或审查未完成。原始分析命令的退出码另存于 `summary.json`。
