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
- [离线流程图总览](docs/flowcharts.html)：整体流程、模型审查闭环、真实覆盖采集三张图，可用浏览器打开，无需联网。

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

函数的“发现问题”记录可引用函数内模型认可的静态诊断，无需将同一诊断再复制成新的模型发现。模型的函数记录与问题清单矛盾时，程序保留原始提交，将该条处理意见标为 `inconclusive` 并注明程序一致性检查原因；不会反复重试到模型给出肯定回答。报告单独显示待确认数量。`completed` 表示提交和处理记录完整，不表示所有结论已经确认。

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

最终结论只能是 `likely_issue`（疑似问题）、`likely_false_positive`（疑似误报）、`inconclusive`（无法判断）。候选复核模式由模型填写证据编号；整文件审查模式由程序根据模型提交的 `file`、`line` 关联已提供且覆盖该行的源码，并从各问题汇总总引用，模型无需重复填写 `evidence_ids`。报告会标注程序关联的引用，原始工具参数仍保留在调用记录中。兼容旧模型返回的引用，但虚构编号、错误位置或引用未读取的源码仍被拒绝。这个检查仅保证引用存在和位置对应，不证明推理正确。证据 ID 在单个候选内部有效。

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
