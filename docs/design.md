# Evidence Review 程序分析工具设计文档

版本：对应 `evidence-review 0.2.0` 当前实现；整理日期：2026-10-20。

本文按仓库代码说明已经实现的行为。后续改进单独列出，不把模型推断、教学覆盖数据或处理记录完整性视为已验证的缺陷结论。

## 1. 系统定位与阅读入口

系统将现有静态扫描器、Tree-sitter 语法索引、可选的基于频谱的故障定位（SBFL）以及本地大模型审查组合成一条可追溯流程。输入是源码及可选覆盖数据，输出是原始静态诊断、候选优先级、源码证据、模型意见和审查记录。

它解决三个工程问题：把多语言扫描结果统一起来；在预算内组织模型所需的源码上下文；记录哪些位置被提供、哪些对象有处理意见、哪些意见仍不确定。当前没有实现完整的跨过程数据流分析、形式化验证或自动修复。

图形入口：[离线流程图总览](flowcharts.html)。同一页面内嵌三张图，无需联网或额外图源文件：

- [整体分析流程](flowcharts.html#overview)
- [模型审查与证据校验闭环](flowcharts.html#model-review)
- [真实覆盖采集与 SBFL](flowcharts.html#coverage)

图描述逻辑关系，具体边界条件以本文和代码为准。

## 2. 三种常用运行方式

| 方式 | 实际执行 | 不会自动发生 |
| --- | --- | --- |
| `analyze`，不传 `--model` | 导入、索引、静态扫描、可选 SBFL、排序、初始源码提取、报告 | 调用模型、执行待分析代码 |
| `analyze --model ...` | 上述过程，再按候选顺序进行有预算的模型审查 | 自动下载模型、训练模型、自动修复和执行目标程序 |
| `collect` 后再 `analyze --spectra ...` | 独立编译并执行简单 C 测试，采集真实逐测试覆盖；之后分析命令消费该数据 | 自动适配任意项目构建系统或采集其他语言覆盖 |

`demo` 默认运行真实 Cppcheck 和 AST，但 SBFL 使用 `synthetic-demo` 人工教学覆盖，且不调用模型。`demo --suite multilang` 运行多语言真实扫描与索引，不提供 SBFL、不调用模型。

## 3. 架构与模块分工

当前为 Python CLI 协调的顺序流水线，没有独立调度服务。扫描器按语言顺序运行，模型按选中的候选逐个运行。

| 模块 | 责任与主要产物 |
| --- | --- |
| [cli.py](../src/evidence_review/cli.py) | 解析参数；协调 `analyze/collect/demo/doctor`；汇总 schema v2 报告与退出码 |
| [project.py](../src/evidence_review/project.py) | 限定输入路径、源码字节快照、语言映射、SHA-256、函数索引和调用候选 |
| [languages.py](../src/evidence_review/languages.py) | Tree-sitter 语言解析器、函数识别、名称及直接调用提取 |
| [scanner.py](../src/evidence_review/scanner.py) | 启动各静态扫描器、归一化诊断、汇总工具状态 |
| [eslint_runner.mjs](../src/evidence_review/eslint_runner.mjs) | 使用工具内置配置对 JS/TS 快照运行 ESLint |
| [sbfl.py](../src/evidence_review/sbfl.py) | 验证覆盖 JSON、Ochiai 评分、RRF 排序、整文件候选聚合 |
| [agent.py](../src/evidence_review/agent.py) | 源码证据工具、Ollama 请求、工具白名单、结论引用校验、轮次与调用预算 |
| [audit.py](../src/evidence_review/audit.py) | 整文件逐项任务、循环下标任务、补查、问题合并与撤回 |
| [coverage.py](../src/evidence_review/coverage.py) | 编译简单 C 程序、逐测试运行、导出 LLVM 覆盖和测试结果 |
| [report.py](../src/evidence_review/report.py) | 生成 JSON 和 HTML，展示原始诊断、模型意见、证据和不完整状态 |
| [progress.py](../src/evidence_review/progress.py) | 模型请求阻塞期间每 15 秒输出等待提示 |
| [test/run_suite.py](../test/run_suite.py) | 批量运行带标签用例，保存 `run.log` 与汇总；不属于分析核心 |

## 4. 主流程：从源码到报告

[查看整体分析流程图](flowcharts.html#overview)

### 4.1 导入与建立索引

1. 输入可以是单个文件，也可以是目录；目录支持用 `--inputs` 缩小导入范围。直接传文件时，项目根为该文件的父目录。
2. 解析后路径必须位于项目根内；目录遍历不跟随符号链接目录，文件解析后也必须仍在根内。跳过 `.git`、`.venv`、`node_modules`、`build`、`dist`、`reports`、`models`、`runtime` 和常见缓存目录等。
3. 每个文件不超过 2,000,000 字节，保存源码字节、语言与 SHA-256。未导入文件不能通过模型工具读取。
4. Tree-sitter 提取函数名、范围、语言、直接调用名称、间接调用计数等。函数 ID 通常为 `file:start:name`，必要时增加列号区分。
5. 语法错误成为 `index_warnings`。恢复语法树可能保留部分结构，也可能使函数索引为空；不会先修正源码再分析。

调用关系按语言族、简单名称及 C 的文件局部约束产生候选；它不是精确调用图。宏、别名、函数指针、动态分派、同名函数和类型解析缺失均会带来歧义或遗漏。C++ 引用返回函数等结构也有已知索引局限。

### 4.2 静态扫描与诊断归一化

| 语言 | 扫描器 | 本实现的检查范围与限制 |
| --- | --- | --- |
| C / C++ | Cppcheck | XML 输出；warning/style/performance/portability，启用 inconclusive；可传编译数据库 |
| Python | Bandit | 安全规则，忽略源码中的 `nosec` 抑制；不能替代类型和边界分析 |
| Python | Ruff | 固定 `E9,F63,F7,F82`；隔离配置、不缓存、忽略 `noqa` |
| Python | Pyright | basic 类型检查；仅导入快照，分析未注解函数；不加载目标配置、不推断第三方库实现、关闭缺失导入诊断 |
| JavaScript | ESLint | 内置 recommended 及 `no-eval/no-implied-eval/no-new-func`；关闭 `no-unused-vars` |
| TypeScript | ESLint + TS parser | 同上，并关闭核心 `no-undef`；尚未接入 `tsc` 类型检查 |

扩展名与细节见 [README](../README.md)。`.h` 在同时导入 C++ 文件时按 C++，否则按 C。Java、Go、Rust 等尚不支持。

所有诊断规范为 `D1、D2…`，包含工具、规则、严重性、消息、实际文件及行号、位置和语言等信息；按扫描器保留额外详情。ID 只在当前运行中唯一。Bandit LOW 会以 warning 展示，同时保留原生严重性。

静态扫描由协调程序在模型前执行。模型工具中没有“重新运行扫描器”的接口。扫描器失败时保留其他工具的结果；无诊断不等于无缺陷。

源码一致性需要区分：Pyright、ESLint 使用导入快照，Cppcheck、Bandit、Ruff 使用磁盘路径。末尾会检查源码是否仍与快照相同，但这不是所有扫描器均在完全隔离快照上运行的保证，也不能捕获分析中改动后又还原的全部情况。

### 4.3 可选 SBFL

未提供 `--spectra` 时，状态是 `not_provided`，继续使用静态结果；这不是报错。提供时先校验：schema v1、唯一测试名、合法 outcome、文件和行号，以及 `source_hashes` 与本次导入文件集合及内容完全匹配。导入范围不同也会导致哈希集合不匹配。

仅 `pass/fail` 测试参与计算，`error` 排除。令 F 为失败测试总数，ef 为覆盖某行的失败测试数，ep 为覆盖该行的通过测试数：

```text
Ochiai(line) = ef / sqrt(F × (ef + ep))
```

只输出 ef > 0 的行；相同测试内重复行号只计一次。没有失败测试时返回 `no_failing_tests`，不据此宣布程序安全。函数级可疑度取函数内部行的最大分数。Ochiai 表示与失败的关联程度，不是经过校准的缺陷概率。

### 4.4 候选聚合与排序

将静态诊断位置与正 SBFL 行合并。位置属于函数时聚到最小包围函数；否则建立文件行级候选。

两个排序通道为：静态通道取候选内最高严重性，SBFL 通道取最高 Ochiai 分数。严重性顺序为 error > warning > performance > portability/style > 其他。相同值共享名次，采用竞赛排名。RRF 使用常数 60：

```text
priority(c) = Σ [1 / (60 + rank_channel(c))]
```

候选没有进入某通道时，该通道贡献为 0。最终按优先级降序，ID 用于稳定打破最终并列。RRF 融合的是名次，不把两个不同来源的数值当作相同概率。

接着选择审查范围：

| 参数 | 候选来源 | `--top-k` 的含义 | 容易遗漏的情况 |
| --- | --- | --- | --- |
| `--review-scope candidates`，CLI 默认 | 静态报警或正 SBFL 产生的候选 | 前 K 个函数或文件位置 | 两个通道都没有线索的位置不会进入模型 |
| `--review-scope files` | 所有导入文件，包括没有报警的文件 | 前 K 个文件 | 超过 K 的文件仍不进入模型；大文件仍受源码预算限制 |

files 模式聚合文件内诊断与 SBFL 行，文件优先级取内部候选优先级最大值；不重新按文件计算 RRF。没有线索的文件优先级为 0。报告保留未选候选。

### 4.5 建立初始源码证据

每个选中候选独立建立 `ContextTools`，先提供候选诊断等线索，再提供源码：函数候选读取函数前 80 行；无函数时读取诊断位置前后约 8 行；整文件模式先读文件前 80 行。

| `--context-mode` | 初始上下文及后续读取 |
| --- | --- |
| `function` | 初始源码后仅允许提交，不再取源码 |
| `fixed` | 函数候选额外读取一层调用者/被调者函数源码，之后仅允许提交；整文件候选没有此函数扩展 |
| `adaptive`，默认 | 允许模型按需获取函数、调用候选与源码片段，然后提交 |

证据统一编号 `E1、E2…`，编号作用域为当前候选。`candidate` 是线索；`source` 才是源码证据；`call_candidates` 是调用摘要。证据序列化字符数计入预算，包含元数据，不仅是源码文本。重复的相同 `read_lines` 范围会复用证据。

### 4.6 可选模型复核

没有 `--model` 时，到此记录 `not_run` 并生成报告。启用时，通过 Ollama `/api/chat` 顺序请求所选模型。模型权重和服务需要提前准备，分析命令不负责下载。

模型根据当前证据提出工具名和参数，程序只执行白名单里的 Python 函数，校验后返回结果；这些“工具”不是参考文档或任意 shell。

| 工具 | 功能 | 约束 |
| --- | --- | --- |
| `get_function` | 获取指定函数带行号源码 | 仅已知函数 ID，最多先返回 80 行 |
| `find_callers` | 查询可能调用该函数的函数摘要 | 最多 20 个；语法候选，需再读取源码 |
| `find_callees` | 查询可能被该函数调用的函数摘要 | 同上 |
| `read_lines` | 获取已导入文件的源码片段 | 1 起始；每次最多 80 行；末尾可截断；起点越界、倒序等拒绝 |
| `submit_review` | 提交结构化意见、发现及审查记录 | 校验字段、ID、源码位置、已提供证据与任务记录 |

adaptive 整文件审查中，若**所有导入文件**均已完整提供，才只暴露 submit。判断依据是全局导入清单，不能简化为“当前文件读完即只提交”。function/fixed 本身也只提供提交工具。

模型请求采用非流式响应、temperature=0。有整文件审查清单且本轮只剩提交工具时，改用 JSON Schema 约束输出，再由程序转换为 submit 调用；否则使用工具调用协议。对 Qwen3，该结构化 Schema 包含循环下标任务时请求 `think=true`、输出上限 8192；普通结构化审查请求 `think=false`。这只是请求配置，不能证明模型推理质量提高。

每轮携带历史消息，工具参数/结果进入 trace。字段错误、未知工具或引用无效时，把具体错误反馈模型，在剩余预算内重试。无工具调用时也提示继续。请求失败记为 error；耗尽轮次或调用次数则为 budget_exhausted；不转换成“没有问题”。

### 4.7 汇总与写报告

所有选中候选处理后，检查磁盘源码与导入快照一致。然后生成 schema v2 JSON 与 HTML；模型启用时，每个工具事件在执行中追加到 `model-trace.jsonl`。

扫描部分失败或模型某项失败，通常仍可生成含已取得结果的报告；但非法输入、覆盖校验失败、源码变化或写盘失败等早期/顶层错误可能直接退出，不能保证每次失败都产生完整报告。覆盖同一输出目录会改写报告，启用模型还会清空并重建 trace，实验应使用独立目录。

## 5. 整文件审查清单与补查机制

这一机制仅在 files 模式且启用模型时建立，目的是暴露未处理项，避免模型只认可第一条报警便结束。

### 5.1 审查对象

- 每条原始静态诊断都有 `diagnostic_reviews` 项。
- 每个已索引函数都有 `function_reviews` 项。
- 函数之外的非空源码形成模块单元；Python 能成功用 ast 解析时，纯 import 行不另作模块任务，其安全诊断仍单独处理。
- Python 显式 for/while 内的每个 subscript 单独形成 `kind=loop_index` 操作单元，ID 包含文件、行、列及 `loop-index`。同时保存访问表达式、循环控制、访问范围及外层循环上下文范围。

下标单元利用 Tree-sitter 恢复语法树，缺冒号使函数索引失效时仍可能保留这些任务。它是要求模型逐处检查的任务生成器，不是静态越界求解器；列表推导式等不在这套独立循环单元的完整覆盖范围内。

### 5.2 结论与校验

候选/诊断结论使用 `likely_issue / likely_false_positive / inconclusive`；函数、模块和操作单元使用 `issue_found / no_issue_found / inconclusive`。每条处理记录需要真实 ID、合法枚举和非空理由。

整文件 findings 必须有 file、line、title、explanation、condition、suggestion。程序自动关联已读到且覆盖该行的源码 E 编号。候选模式需要模型引用实际返回的 E 编号，有倾向性的意见至少引用一份源码证据。

对单元或诊断给出非 inconclusive 意见前，要求相关源码范围已完整提供。循环下标还要求其循环上下文已提供。程序校验引用存在和位置覆盖，不验证“该表达式确实会越界”等语义命题。

一致性检查会将“issue_found 却没有范围内发现或已认可静态诊断”、以及“no_issue_found 却有同范围发现”等矛盾降为 inconclusive。当前问题定位主要是行级，同一行多个下标可能被混淆；列级单元 ID 尚未等价于列级问题证据。

### 5.3 增量保存、修订、撤回与补查

源码位置校验通过的发现会先保存，即使同次提交的任务清单无效也尽量保留。任务清单按该次提交整体验证，错误反馈附带已保留发现和待处理 ID。

发现以 `(file,line,title)` 合并并分配稳定 F 编号；同键最新有效提交更新解释、条件和建议，原始提交保留在 trace。模型可用 `withdraw_findings` 按 F 编号及理由撤回，报告保留被撤回条目，原始静态诊断不删除。

清单补齐之外，还会请求一次定向复查：重新检查没有发现问题/不确定的单元、所有循环下标单元，并核对已有发现。下一轮要求真正重新提交相应处理记录，旧记录不能代替。Schema 优先约束尚缺的 ID 和数量。

只有缺项清空、规定的补查完成，程序才把该审查标为 completed。允许部分意见仍是 inconclusive；completed 表示流程与记录满足要求，不表示每个判断正确、源码已执行或所有问题已找出。

## 6. 第 8 行索引越界案例如何经过流程

针对现有 [tests/test.py](../tests/test.py)：第一行缺冒号，函数索引可能为 0；原来整文件只剩大模块单元。模型即使看过全部 22 行，也可能仅报告其他错误便遗漏 `data_list[i + 1]`。

当前流程在 files 模式中，恢复语法树将 `data_list[i]` 与 `data_list[i+1]` 分为两个下标任务。每项需要自己的边界处理意见，并进入补查。前者在列表长度不变的 `range(len(data_list))` 中合法；后者对非空列表在最后一轮有 `i+1=len(data_list)`，超过最大正索引。

该结论应注明前提：先修复语法阻断错误，并确实能执行到该访问；原始混合类型输入可能更早在累加处失败。空列表不会进入循环。本机制提高此类任务的显式性，但不提供普遍不漏报保证。

## 7. 真实覆盖采集是独立流程

`collect` 接受项目、可选导入范围以及 manifest。manifest.sources 必须是本次导入的 `.c` 文件；tests 含唯一名称、字符串参数列表、expected_stdout 和可选 expected_exit。

1. 查找 clang、llvm-profdata、llvm-cov，记录版本；应使用相互兼容的 LLVM 工具链，代码不自动安装或保证版本匹配。
2. 在临时目录编译：C11、O0、调试信息、profile instrumentation、coverage mapping。
3. 每个测试独立执行已编译程序，独立 profraw；运行目录为临时目录，单测试默认超时 10 秒。
4. 合并该测试 profile，用 llvm-cov 导出 LCOV，只提取导入文件中执行次数大于 0 的行。
5. 退出码与完整 stdout 都匹配预期则 pass，否则在成功采集前提下为 fail。超时、未生成 profile、无覆盖或工具失败为 error。
6. 检查源码未变，输出 schema v1 spectra。之后需要另行调用 analyze 并显式传入 `--spectra`。

collect 确实在本机执行用户指定 C 程序，当前没有容器或沙箱执行层。崩溃且没有 profile 的测试归 error，不会冒充有效失败频谱。C++/Python/JS/TS 当前需在外部采集并转换相同格式的数据。

覆盖数据模板如下，SHA 必须替换为本次精确导入快照值，不能原样运行：

```json
{
  "schema_version": 1,
  "origin": "external-per-test-execution",
  "source_hashes": {"example.c": "<该文件的 SHA-256>"},
  "tests": [
    {"name": "normal", "outcome": "pass", "covered": {"example.c": [1, 2]}},
    {"name": "boundary", "outcome": "fail", "covered": {"example.c": [1, 2, 3]}}
  ]
}
```

## 8. 状态、预算与退出码

| 位置 | 状态 | 含义 |
| --- | --- | --- |
| 单个扫描器 | completed / error / unavailable | 扫描完成 / 运行或解析失败 / 未安装等不可用 |
| 扫描汇总 | completed / partial / unavailable / error | 全部完成 / 有工具完成但其他未完成 / 全部不可用 / 其他失败组合 |
| SBFL | not_provided / no_failing_tests / completed | 未传数据 / 无有效失败测试 / 已按失败测试计算 |
| 模型审查 | not_run / completed / error / budget_exhausted | 未启用 / 有效完成 / 请求或数据处理失败 / 预算内未完成 |

| CLI 参数 | 默认 | 作用域与解释 |
| --- | --- | --- |
| `--review-scope` | candidates | 候选报警复核或 files 整文件审查 |
| `--context-mode` | adaptive | 模型取证策略 |
| `--top-k` | 5 | 全次运行选择的候选/文件数 |
| `--context-chars` | 14000 | 每候选累计发放的证据序列化字符预算 |
| `--max-rounds` | 6 | 每候选最多模型请求轮次 |
| `--max-calls` | 10 | 每候选模型请求执行的工具次数，含 submit 和无效调用 |
| `--model-timeout` | 180 秒 | 单轮请求等待超时，不是整个分析总时限 |
| `--ollama-url` | `http://localhost:11434` | 模型服务地址 |
| `--output` | reports/latest | 报告目录 |

扫描调用默认超时 120 秒；并非 analyze 暴露的独立 CLI 参数。初始源码与 fixed 扩展消耗证据预算，但不计模型请求执行的工具次数。字符预算不等于 Token 预算；每轮重发历史仍会增加 Token 和耗时。基础输出上限由任务结构决定（2048/4096/6144），特定 Qwen3 下标结构化任务为 8192。

`analyze` 正常完成返回 0；扫描未完整完成或任何模型审查 error/budget_exhausted 返回 2。发现疑似问题本身不会让退出码非零。`collect` 有测试 error 返回 2，有有效 fail 仍可返回 0。`doctor` 用于探测工具但当前返回 0，且不单独探测 Pyright，不能只凭其退出码判定环境完整。

## 9. 数据与报告契约

| 数据对象 | 核心字段与关联 |
| --- | --- |
| Project | sources 字节、languages、functions、warnings；hashes 对应本次所有导入文件 |
| Diagnostic | id=Dn、tool、rule、severity、file、line、message、locations 等 |
| Candidate | id、function_id、file、line、name、diagnostics、sbfl_lines、sbfl_score、priority；文件候选含 scope=file |
| Evidence | evidence_id=En、kind；source 含 file/start/end/code，调用候选含 functions 摘要 |
| Review | candidate_id、status、mode、review、trace、evidence、context_chars；files 另有 source_coverage、模型审查另有 audit |
| Finding | finding_id=Fn（整文件累计发现）、file、line、title、explanation、condition、suggestion、关联源码编号 |
| Audit | units、诊断及单元处理记录、pending、loop_index_total、followup_performed、complete、撤回记录 |

成功汇总的 report.json 使用 `schema_version=2`，顶层包含 project、created_at、model、context_mode、review_scope、source_hashes、languages、summary、scanner、sbfl、candidates、reviews、model_usage、index_warnings、limitations。

summary 汇总文件数、函数数、静态诊断数、候选及选中数、模型 findings 数、证据字符数、源码字符数和耗时。候选复核模式主要给 verdict，不提供整文件 findings 数组；因此该模式下 `model_findings=0` 不能解释成模型未认可任何风险。static diagnostics 与 files 模式 findings 可能重合，也不能简单相加当作唯一缺陷数。

输出文件的责任边界：

- `report.html`：供人工查看，原始诊断与模型推断分开，展示证据及不完整提示。
- `report.json`：供实验统计与集成，包含候选、审查和调用轨迹。
- `model-trace.jsonl`：启用模型时，逐工具事件写入原始 arguments、程序 response 及可用 transport 信息。不是完整 HTTP 请求和模型所有内部输出的归档，也不保证请求超时前已有可记录事件。
- `run.log`、`summary.md/json`：由批量用例运行器或外层日志捕获产生，核心 analyze 不自动生成 run.log。

部分专项报告有 `postprocessing`，记录用保存的真实模型响应重新应用校验/合并修复的来源。这是开发验收时的处理记录，不是当前 CLI 自动提供的通用缓存、续跑或重放功能。

## 10. 部署与最小复现

安装步骤以 [README](../README.md) 为准。基础环境为 Python 3.11+、满足 package.json engines 的 Node.js 与 npm；即使只分析 Python，Pyright 也依赖 Node。使用 requirements-lock.txt 和 package-lock.json 安装，不跨机器复制 `.venv` 或 node_modules。模型与 LLVM 都是额外可选依赖。

在项目根目录分析自己的单个文件：

```bash
# 静态分析，不执行目标源码
.venv/bin/evidence-review analyze tests/test.py --output reports/test-py-static

# 已下载模型，且本地服务已在 11435 启动后：
.venv/bin/evidence-review analyze tests/test.py \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --review-scope files --context-mode adaptive --model-timeout 300 \
  --output reports/test-py-model
```

不要用 `python tests/test.py` 启动分析器，这会执行待测文件。存在语法错误时，部分扫描器受阻导致 partial/退出码 2 与保留其他诊断是可以同时发生的。

入口默认不同，运行前应对应：

| 入口 | 默认模型地址 | 默认审查范围 |
| --- | --- | --- |
| `evidence-review analyze` | 11434 | candidates |
| `scripts/local-model.sh analyze` | 127.0.0.1:11435 | files |
| `test/run_suite.py` | 127.0.0.1:11435 | files |

项目内脚本使用 runtime/ollama/ollama 与 models/ollama，要求运行器事先存在，服务配置上下文 16384、单请求并行度及单模型加载；不会自动安装运行器。源码证据发送至用户配置的模型地址，显式使用远端地址时也会发送至远端。模型工具仅能读取导入快照，不具备任意 shell、联网检索、读取任意文件或修改目标源码的权限。

## 11. 验证体系与已知实际结果

`tests/` 是 pytest 契约和回归测试；`test/cases/` 是供分析的 5 种语言、4 组共 20 个用例；`test/regressions/` 存专项补充。用例带有故意错误，不应作为普通脚本执行。

```bash
.venv/bin/python -m pytest
.venv/bin/python test/run_suite.py --output test/results
```

批量运行器按规则 ID 与行号统计静态标签命中，标签不会发给模型；该统计不是自动语义评判，不能直接作为模型召回率。正常对照出现静态或模型发现返回 1，意外工具/模型失败返回 2，正常工作流可为 0；语法错误组允许特定预期扫描失败。

最近一次循环下标改动的记录显示：106 项 pytest 通过。以下是**已有真实模型验收记录**，本次编写文档未重跑模型：

| 用例 | 静态诊断 | 模型发现 | 下标任务 | 记录完成 | 语义评估 |
| --- | ---: | ---: | ---: | --- | --- |
| 原 tests/test.py | 5，扫描 partial | 4 | 2 | 是 | 检出第 8 行；模型又撤回了真实的潜在未定义名称风险 |
| neighbors.py | 0 | 2 | 2 | 是 | 检出第 4 行越界；另有“空输入未处理”误报 |
| clean.py | 0 | 0 | 5 | 是 | 4 种安全边界写法没有报警 |

详见 [review.md](../test/results-model-loop-boundaries/review.md) 与 [summary.json](../test/results-model-loop-boundaries/summary.json)。三例都未执行目标代码、未提供 SBFL。目标回归与正常对照通过，但 `all_semantic_acceptance_checks_passed=false`。同一行多访问会引起行级一致性歧义，原文件第 8 行模型触发条件也只举了长度为 1 的例子。这些限制不能被“completed”或单元测试通过掩盖。

## 12. 设计取舍、创新边界与后续工作

当前可描述为工程组合与机制改进：多语言统一诊断；静态与可选 SBFL 双通道候选；有预算的自适应取证；可追溯引用；整文件逐项记录、独立循环下标任务和补查。AST、SBFL、工具调用这些组成部分本身不能据此宣称首创；也不能未经验证认定与某篇论文完全相同。若需论文对照，应提供论文全文并逐项映射任务定义、算法、工具、数据集和指标。

| 当前限制 | 后续改进方向（尚未实现） | 建议验收方式 |
| --- | --- | --- |
| 仅按函数与语法调用取上下文 | 增加类型/数据流/切片证据 | 构造跨函数、别名、动态分派对照 |
| 越界任务依赖模型判定 | 有条件接入符号约束或专用边界规则 | 首末轮、guard、容器变化、负索引、切片及推导式测试 |
| 行级发现关联多个操作 | 列范围或 AST 节点级 finding 定位 | 同行合法与非法访问混合对照 |
| TS 没有类型检查 | 受控 tsc/type service 适配 | 编译期类型错误与合法泛型对照 |
| 覆盖采集只支持简单 C | 各语言逐测试覆盖适配器 | 哈希绑定、覆盖转换与失败判定测试 |
| 预算限制与单机速度 | 更好的分块调度、缓存/续跑、预算估算 | 吞吐、超时率、Token、端到端耗时 |
| 模型误报、撤回真实潜在问题 | 更明确的可达性/契约证据和人工复核 | 独立语义标注与误报/漏报分析 |

要验证组合策略的价值，可在相同数据、模型和预算下比较：静态基线、仅函数上下文、固定扩展、自适应取证，以及有/无独立下标任务。报告 precision/recall、误报、覆盖到的源码比例、Token 与耗时，区分工具失败和有效无发现。仅有“模型少读了一些代码”或个别用例检出，不能证明总体效果或研究创新。
