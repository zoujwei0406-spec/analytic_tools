"""Offline rule reliability regressions; no LLM calls and no fixture execution."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from evidence_review.evidence import analyze_evidence
from evidence_review.project import Project

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'cases/evidence_bounds/reliability/manifest.json'
POLICIES = ('rules', 'fixed', 'gaps', 'cost')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(policies=POLICIES):
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    results = []
    for policy in policies:
        predictions = []
        total_cost = total_actions = 0
        for group in manifest['groups']:
            source = ROOT / group['source']
            if digest(source) != group['sha256']:
                raise ValueError(f'用例摘要不匹配：{source}')
            project = Project(source)
            analysis = analyze_evidence(project, policy=policy, max_actions=4000, max_cost=8000)
            expected = {(row['line'], row['column']): (name, row, label)
                        for name, label in group['functions'].items() for row in label['accesses']}
            actual = {(claim['line'], claim['column']): claim for claim in analysis['claims']}
            if set(expected) != set(actual):
                raise ValueError(f"{group['id']}: 检查点有遗漏、冗余或标签错位")
            total_cost += analysis['summary']['estimated_cost']
            total_actions += analysis['summary']['actions']
            for location, (name, label, function) in expected.items():
                claim = actual[location]
                predictions.append({'group': group['id'], 'function': name, **label,
                    'expected': label.get('expected_by_policy', {}).get(policy, label['expected']), 'actual': claim['verdict'], 'reason': claim['reason'], 'stop_reason': claim['stop_reason'],
                    'access_id': claim['access_id'], 'expression': claim['expression'],
                    'label_reason': function['reason'], 'other_issue': function['other_issue'],
                    'witness': claim.get('witness'), 'assumptions': claim['assumptions'],
                    'tools': [event['tool'] for event in analysis['trace'] if event['access_id'] == claim['access_id']]})
        counts = Counter()
        for row in predictions:
            want, got, truth = row['expected'], row['actual'], row['bounds_truth']
            counts['expectation_matches'] += want == got
            counts['unknown'] += got == 'UNDECIDED'
            counts['resolved'] += got != 'UNDECIDED'
            counts['unsafe_as_safe'] += truth == 'unsafe' and got == 'SAFE'
            counts['safe_as_violation'] += truth == 'safe' and got == 'VIOLATION'
            counts['boundary_decisions'] += want == 'UNDECIDED' and got != 'UNDECIDED'
            counts['program_tp'] += truth == 'unsafe' and got == 'VIOLATION'
            counts['program_fn'] += truth == 'unsafe' and got != 'VIOLATION'
        results.append({'policy': policy, 'metrics': dict(counts), 'actions': total_actions,
                        'relative_cost': total_cost, 'predictions': predictions})
    totals = Counter(r['bounds_truth'] for r in results[0]['predictions'])
    return {'schema_version': 1, 'kind': 'deterministic_reliability_regression', 'model': None,
            'fixture_execution': False, 'contract': manifest['contract'], 'manifest_sha256': digest(MANIFEST),
            'input_hashes': {g['source']: g['sha256'] for g in manifest['groups']},
            'analyzer_hashes': {str(p.relative_to(ROOT.parent)): digest(p)
                                for p in sorted((ROOT.parent / 'src/evidence_review').rglob('*.py'))},
            'cases': {'functions': sum(len(g['functions']) for g in manifest['groups']),
                      'accesses': len(results[0]['predictions']), 'truths': dict(totals)},
            'runs': results}


def compare(result, baseline):
    for key in ('manifest_sha256', 'input_hashes', 'cases'):
        if result[key] != baseline[key]:
            raise ValueError('前后输入或标签不一致，拒绝比较')
    if [r['policy'] for r in result['runs']] != [r['policy'] for r in baseline['runs']]:
        raise ValueError('前后策略集合不一致')
    return baseline


def markdown(result):
    lines = ['# 扩展用例与规则可靠性验证', '',
             f"新增 {result['cases']['functions']} 个函数、{result['cases']['accesses']} 处下标访问，分为真实缺陷、安全保护、能力边界和其他问题四类。", '',
             '以下为程序规则检查，不调用模型、不执行用例；人工语义标签与规则能力期望分别保存。未知不算安全，测试通过不代表全部真实缺陷已检出。', '',
             '| 版本 | 策略 | 符合期望 | 未知 | 真实危险误判安全 | 安全访问误报 | 能力边界越权判定 | 程序检出 / 漏检 |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |']
    variants = ([('修改前', result['baseline'])] if 'baseline' in result else []) + [('当前', result)]
    for title, data in variants:
        for row in data['runs']:
            m = row['metrics']
            lines.append(f"| {title} | {row['policy']} | {m['expectation_matches']}/{data['cases']['accesses']} | {m['unknown']} | {m['unsafe_as_safe']} | {m['safe_as_violation']} | {m['boundary_decisions']} | {m['program_tp']} / {m['program_fn']} |")
    lines += ['', '## 当前结果逐项说明', '', '| 文件与函数 | 访问 | 语义标签 | 规则期望 | 当前结果 | 原因 |',
              '| --- | --- | --- | --- | --- | --- |']
    for p in result['runs'][-1]['predictions']:
        lines.append(f"| {p['group']}/{p['function']}:{p['line']} | `{p['expression']}` | {p['bounds_truth']} | {p['expected']} | {p['actual']} | {p['reason']} |")
    if 'baseline' in result:
        lines += ['', '## 本次修改解决的回归', '']
        for before, after in zip(result['baseline']['runs'][-1]['predictions'], result['runs'][-1]['predictions']):
            if before['actual'] != after['actual']:
                lines.append(f"- `{after['function']}`：{before['actual']} → {after['actual']}；{after['label_reason']}")
    lines += ['', '## 验证范围', '',
              '- 修改前结果用动手前保存的冻结规则模块实际运行相同最终用例得到；不是恢复历史论文实验，也不是旧的 checklist/evidence 大模型对照。',
              '- 四个策略使用相同源码与足够的动作/相对成本预算，检验调度顺序是否影响正确性；本表不是三个组件的完整消融。',
              '- 安全保护的规则扩展包括链式/合取上下界和简单提前退出；副作用、嵌套循环和复杂下标新增有限白名单 AST 反例搜索；找不到反例不证明安全。',
              '- 能力边界含真实缺陷，因此程序漏检仍单独列出；UNKNOWN 不等于已证明安全或已检出；撤销不可靠的程序报警不代表模型最终误报也消失。',
              '- pytest 另用受控模板与原生 Python 列表进行有限输入对照，并检查属性级校正、未知回退与预算约束；不导入这些缺陷文件。',
              '- 用例为本次人工设计回归，不是独立留出集或真实开源缺陷，不能据此声称真实项目准确率提高。',
              '- 本表仅测程序规则；重新运行的真实模型前后对照另见 results-reliability-comparison，不能混合两种实验口径。', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policies', nargs='+', choices=POLICIES, default=list(POLICIES))
    parser.add_argument('--baseline', type=Path, help='同一新增用例在本次修复前的真实结果')
    parser.add_argument('--output', type=Path, default=ROOT / 'results-evidence-reliability')
    args = parser.parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('输出目录非空，请选择新目录，保留已有记录')
    result = run(args.policies)
    if args.baseline:
        result['baseline'] = compare(result, json.loads(args.baseline.read_text(encoding='utf-8')))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output / 'summary.md').write_text(markdown(result), encoding='utf-8')
    for row in result['runs']:
        print(row['policy'], row['metrics'])
    print(f"报告：{args.output / 'summary.md'}")
    return 0 if all(r['metrics']['expectation_matches'] == result['cases']['accesses'] for r in result['runs']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
