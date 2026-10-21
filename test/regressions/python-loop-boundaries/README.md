# Python 循环下标审查回归

这些源码仅供分析，不由测试命令执行。标签与验收条件放在本说明中，不随源码发给模型；本目录不改变原来 `test/cases` 的 20 个用例及其标签。

- `neighbors.py`：第 4 行的 `values[index + 1]` 在非空列表的最后一轮越界；同一行的 `values[index]` 本身合法。模型应报告前者，不能将后者也判为越界。
- `clean.py`：正常对照，包含缩短循环上界、显式分支保护、合法负索引，以及可截断的切片。空输入不会触发这些循环中的访问；预期没有静态诊断或模型问题。
- 项目原有 `tests/test.py`：保留第一行缺冒号等错误，检验函数索引失效时，第 8 行的循环偏移访问仍能成为独立审查任务。原文件不作修复。

在项目根目录执行，要求本地 `qwen3:8b` 已下载且服务运行于 11435；系统服务若使用 11434，请替换地址：

```bash
.venv/bin/evidence-review analyze test/regressions/python-loop-boundaries/neighbors.py \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --review-scope files --model-timeout 300 \
  --output test/results-model-loop-boundaries/neighbors

.venv/bin/evidence-review analyze test/regressions/python-loop-boundaries/clean.py \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --review-scope files --model-timeout 300 \
  --output test/results-model-loop-boundaries/clean

.venv/bin/evidence-review analyze tests/test.py \
  --model qwen3:8b --ollama-url http://127.0.0.1:11435 \
  --review-scope files --model-timeout 300 \
  --output reports/test-py-model
```

查看各目录的 `report.html`、`report.json` 和 `model-trace.jsonl`。检查 `reviews[].audit` 中的独立下标处理记录及补查状态，并人工核对问题解释和触发条件；任务完成不等于模型判断正确，也不代表动态复现。原文件存在语法错误时静态扫描仍为 `partial`，退出码 2 属于预期。
