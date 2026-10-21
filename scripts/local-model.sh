#!/bin/bash
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
export OLLAMA_MODELS="$repo_root/models/ollama"
export OLLAMA_HOST="127.0.0.1:11435"
export OLLAMA_NO_CLOUD=1
export OLLAMA_CONTEXT_LENGTH=16384
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
ollama_bin="$repo_root/runtime/ollama/ollama"
model_name="qwen3:8b"

if [[ ! -x "$ollama_bin" ]]; then
  printf 'Ollama 运行器不存在：%s\n' "$ollama_bin" >&2
  exit 2
fi

action="${1:-help}"
if [[ $# -gt 0 ]]; then shift; fi
case "$action" in
  serve)
    printf '本地模型目录：%s\n服务地址：http://%s\n' "$OLLAMA_MODELS" "$OLLAMA_HOST"
    exec "$ollama_bin" serve
    ;;
  pull)
    exec "$ollama_bin" pull "$model_name"
    ;;
  list)
    exec "$ollama_bin" list
    ;;
  chat)
    exec "$ollama_bin" run "$model_name" "$@"
    ;;
  analyze)
    if [[ $# -eq 0 ]]; then
      set -- "$repo_root/src/evidence_review/demo_project"
    fi
    exec "$repo_root/.venv/bin/evidence-review" analyze \
      --model "$model_name" --ollama-url "http://$OLLAMA_HOST" \
      --review-scope files \
      --output "$repo_root/reports/local-model" "$@"
    ;;
  *)
    printf '用法：%s {serve|pull|list|chat|analyze [项目目录及分析选项]}\n' "$0"
    printf '终端 1：%s serve\n终端 2：%s analyze\n' "$0" "$0"
    ;;
esac
