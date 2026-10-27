"""Reproducible program-only ablations; this does not measure LLM recall/token savings."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from evidence_review.evidence import analyze_evidence
from evidence_review.project import Project

ROOT = Path(__file__).resolve().parent
POLICIES = ('rules', 'fixed', 'gaps', 'cost')


def evaluate(project, analysis, expected):
    grouped = defaultdict(list)
    for claim in analysis['claims']:
        function = project.at(claim['file'], claim['line'])
        if function is None:
            raise ValueError('用例没有函数标签')
        grouped[function.name].append(claim)
    if set(grouped) != set(expected):
        raise ValueError('检查点函数集合与标签不匹配，禁止漏掉未标注位置')
    predictions, counts = [], Counter()
    for name, labels in expected.items():
        claims = grouped[name]
        if len(claims) != len(labels):
            raise ValueError(f'{name}: 检查点数量与标签不一致')
        for index, (label, claim) in enumerate(zip(labels, claims)):
            prediction = claim['verdict']
            counts['unknown'] += prediction == 'UNDECIDED'
            counts['correct_label'] += prediction == label
            if label == 'VIOLATION':
                counts['tp' if prediction == 'VIOLATION' else 'fn'] += 1
                counts['unsafe_as_safe'] += prediction == 'SAFE'
            elif label == 'SAFE':
                counts['fp'] += prediction == 'VIOLATION'
                counts['tn'] += prediction == 'SAFE'
            else:
                counts['unsupported_decisions'] += prediction != 'UNDECIDED'
            predictions.append({'function':name,'ordinal':index,'access_id':claim['access_id'],
                                'line':claim['line'],'column':claim['column'],'expression':claim['expression'],
                                'expected':label,'actual':prediction,'reason':claim['reason'],
                                'missing':claim['missing'],'stop_reason':claim['stop_reason']})
    tp, fp, fn = (counts[k] for k in ('tp','fp','fn'))
    metrics = {k:counts[k] for k in ('tp','fp','fn','tn','unknown','correct_label','unsafe_as_safe','unsupported_decisions')}
    metrics.update(precision=tp/(tp+fp) if tp+fp else None, recall=tp/(tp+fn) if tp+fn else None,
                   unknown_rate=counts['unknown']/len(predictions) if predictions else None,
                   resolved_known=sum(p['expected'] != 'UNDECIDED' and p['actual'] != 'UNDECIDED' for p in predictions))
    return metrics, predictions


def run(budgets=(80,140,200), max_actions=1000):
    project = Project(ROOT/'cases/evidence_bounds/cases.py')
    labels = json.loads((ROOT/'cases/evidence_bounds/expected.json').read_text())
    rows = []
    for budget in budgets:
        for policy in POLICIES:
            analysis = analyze_evidence(project,policy=policy,max_actions=max_actions,max_cost=budget)
            metrics,predictions = evaluate(project,analysis,labels['functions'])
            rows.append({'budget':budget,'policy':policy,'metrics':metrics,'costs':analysis['summary'],
                         'predictions':predictions})
    return {'schema_version':1,'kind':'deterministic_program_ablation','model':None,
            'source_hashes':project.hashes(),'contract':labels['contract'],
            'labels':{'accesses':sum(map(len,labels['functions'].values())),
                      'positive':sum(v=='VIOLATION' for rows in labels['functions'].values() for v in rows),
                      'safe':sum(v=='SAFE' for rows in labels['functions'].values() for v in rows),
                      'capability_boundary':sum(v=='UNDECIDED' for rows in labels['functions'].values() for v in rows)},
            'notes':['所有策略共用检查点、标签、动作上限和相对成本预算。',
                     'rules 仅做局部规则，不查询辅助函数；fixed 按固定顺序补齐必要事实；gaps 按缺口顺序；cost 按启发式收益成本排序。',
                     'SAFE/VIOLATION 为显式前提下的规则结果，不是无条件证明或动态复现。',
                     '预算耗尽的阳性算 FN；unknown 不算 TN；能力边界标签不参与缺陷召回率。',
                     '这是新增教学/回归用例的确定性消融，不证明真实模型性能、真实项目召回率或成本策略普遍优越。'],
            'runs':rows}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budgets',nargs='+',type=int,default=[80,140,200])
    parser.add_argument('--max-actions',type=int,default=1000)
    parser.add_argument('--output',type=Path,default=ROOT/'results-evidence-ablation')
    args=parser.parse_args(argv)
    if any(b<0 for b in args.budgets) or args.max_actions<0:
        parser.error('预算不能为负数')
    result=run(args.budgets,args.max_actions)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    lines=['# 知识库与证据补查消融结果','',
           '同一批 25 处访问：12 个条件风险、7 个条件安全、6 个能力边界。没有调用大模型；四处旧未知已由有界反例提升为真实风险标签。','',
           '| 预算 | 策略 | TP | FP | FN | 已判定的已知样本 | 未确定总数 | 实际相对成本 | 动作数 |',
           '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in result['runs']:
        m,c=row['metrics'],row['costs']
        lines.append(f"| {row['budget']} | {row['policy']} | {m['tp']} | {m['fp']} | {m['fn']} | {m['resolved_known']} | {m['unknown']} | {c['estimated_cost']} | {c['actions']} |")
    lines.extend(['','## 解释','',*['- '+note for note in result['notes']],
                  '', '需要同时看 FN 与 unknown，不能将少报问题当作误报更少的改进。',
                  '成本策略可能低于简单缺口顺序；应据实报告，不为当前用例调整权重来制造优势。',
                  '真实模型调用与报告集成另行验证，本表不包含模型准确率或 Token 节省结论。',''])
    (args.output/'summary.md').write_text('\n'.join(lines))
    print('\n'.join(lines))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
