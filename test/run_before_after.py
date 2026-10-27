"""Paired end-to-end comparison; semantic scoring requires explicit adjudication."""
import argparse
import ast
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import urllib.request

from evidence_review import cli
from evidence_review.agent import Ollama
from run_suite import LiveLog

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'cases/evidence_bounds/comparison.py'
# Independent semantic labels, never sent to the analyzer/model.
LABELS = {
    'A': {'function': 'case_a', 'line': 3, 'bug': True, 'reason': '非空列表最后一次 i+1 等于长度；长度 1 即触发。'},
    'B': {'function': 'case_b', 'line': 7, 'bug': False, 'reason': 'range(n-1) 排除最后一个索引；n<=1 时不访问。'},
    'C': {'function': 'case_c', 'line': 11, 'bug': True, 'reason': '长度 1 时 i-2=-2，小于合法下界 -1。'},
    'D': {'function': 'case_d', 'line': 15, 'bug': False, 'reason': '只有非空列表才迭代，-1 始终合法。'},
    'E': {'function': 'case_e', 'line': 25, 'bug': True, 'reason': 'touch 无副作用；非空列表最后一次 i+1 越界。'},
    'F': {'function': 'case_f', 'line': 31, 'bug': False, 'reason': 'touch 无副作用，range(n-1) 保证 i+1<n。'},
}
CONTRACT = '仅评估普通内置 list、标准 len/range、无并发修改前提下的 IndexError；类型误用等不在本次评分范围。'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def api(base, endpoint, payload=None, timeout=180):
    request = urllib.request.Request(base.rstrip('/') + '/api/' + endpoint,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def model_identity(base, model):
    for item in api(base, 'tags')['models']:
        if model in (item.get('name'), item.get('model')):
            return {k: item.get(k) for k in ('name', 'digest', 'details')}
    raise ValueError(f'本地服务没有模型 {model}')


def artifacts(report):
    """Only final, visible alerts; withdrawn findings and SAFE are not alerts."""
    result = []
    for i, diagnostic in enumerate(report['scanner']['diagnostics']):
        result.append({'id': f'static:{i}', 'origin': 'static', 'content': diagnostic})
    for i, review in enumerate(report['reviews']):
        for j, finding in enumerate((review.get('review') or {}).get('findings', [])):
            result.append({'id': f'model:{i}:{j}', 'origin': 'program' if finding.get('evidence_origin') == 'program_verified' else 'model', 'content': finding})
    for claim in report.get('program_analysis', {}).get('claims', []):
        if claim['verdict'] == 'VIOLATION':
            result.append({'id': 'program:' + claim['access_id'], 'origin': 'program', 'content': {
                k: claim[k] for k in ('file', 'line', 'expression', 'reason', 'assumptions', 'witness')}})
    return result


def score(alerts, decisions, labels=LABELS):
    """Never turn a line match or an unreviewed finding into a semantic TP/FP."""
    if set(decisions) - {a['id'] for a in alerts}:
        raise ValueError('裁决包含不存在的报警 ID')
    detected, false_issues, false_controls = set(), set(), set()
    pending, outside = [], []
    for alert in alerts:
        decision = decisions.get(alert['id'], {})
        status = decision.get('status', 'pending')
        if status == 'pending':
            pending.append(alert['id'])
            continue
        if not decision.get('reason'):
            raise ValueError('每条语义裁决必须说明理由')
        if status == 'out_of_scope':
            outside.append(alert['id'])
        elif status == 'tp':
            matches = decision.get('labels', [])
            if not matches or any(k not in labels or not labels[k]['bug'] for k in matches):
                raise ValueError('TP 必须对应独立标注的真实缺陷')
            detected.update(matches)
        elif status == 'fp':
            if not decision.get('issue_key'):
                raise ValueError('FP 必须有去重键 issue_key')
            controls = decision.get('labels', [])
            if any(k not in labels or labels[k]['bug'] for k in controls):
                raise ValueError('误报位置必须对应安全对照，其他误报可不指定 labels')
            false_issues.add(decision['issue_key'])
            false_controls.update(controls)
        else:
            raise ValueError(f'未知裁决 {status}')
    if pending:
        return {'status': 'pending_adjudication', 'pending': pending, 'out_of_scope': outside}
    positive = {k for k, v in labels.items() if v['bug']}
    negative = {k for k, v in labels.items() if not v['bug']}
    tp, fp = len(detected), len(false_issues)
    return {'status': 'scored', 'tp': tp, 'fp': fp, 'fn': len(positive - detected),
            'recall': tp / len(positive) if positive else None,
            'precision': tp / (tp + fp) if tp + fp else None,
            'false_positive_rate': len(false_controls) / len(negative) if negative else None,
            'detected': sorted(detected), 'missed': sorted(positive - detected),
            'false_controls': sorted(false_controls), 'out_of_scope': outside}


def paired_changes(before, after):
    if before.get('status') != 'scored' or after.get('status') != 'scored':
        return None
    return {'newly_detected': sorted(set(after['detected']) - set(before['detected'])),
            'lost_detections': sorted(set(before['detected']) - set(after['detected'])),
            'removed_false_controls': sorted(set(before['false_controls']) - set(after['false_controls'])),
            'added_false_controls': sorted(set(after['false_controls']) - set(before['false_controls']))}



def first_detection_rounds(report, alerts, final_decisions, round_decisions):
    """Model rounds need semantic adjudication; program alerts occur before round 1."""
    model_first, program_first = {}, {}
    for alert in alerts:
        decision = final_decisions.get(alert['id'], {})
        if alert['origin'] == 'program' and decision.get('status') == 'tp':
            for label in decision['labels']:
                program_first[label] = 0
    events = [e for review in report['reviews'] for e in review['trace']]
    for index, decision in round_decisions.items():
        if not index.isdigit() or int(index) >= len(events):
            raise ValueError('逐轮裁决引用了不存在的事件')
        event = events[int(index)]
        if event['tool'] != 'submit_review' or not decision.get('reason'):
            raise ValueError('逐轮裁决必须对应提交事件且说明理由')
        for label in decision.get('labels', []):
            if label not in LABELS or not LABELS[label]['bug']:
                raise ValueError('逐轮正确检出只能关联真实缺陷')
            model_first[label] = min(model_first.get(label, event['round']), event['round'])
    system_first = dict(model_first)
    for label, round_number in program_first.items():
        system_first[label] = min(system_first.get(label, round_number), round_number)
    return {'model': model_first, 'program': program_first, 'system': system_first,
            'rounds_adjudicated': len(round_decisions) == sum(e['tool'] == 'submit_review' for e in events)}


def equal_target_efficiency(before, after):
    """Compare a shared correct target; total elapsed time alone is not a speedup."""
    if any(row.get('input_context_valid') is False for row in (before, after)):
        return {'status': 'invalid_context'}
    if not (before['complete'] and after['complete']):
        return {'status': 'incomplete'}
    if any(row['quality']['status'] != 'scored' for row in (before, after)):
        return {'status': 'pending_adjudication'}
    common = sorted(set(before['quality']['detected']) & set(after['quality']['detected']))
    if not all(row['first_detection']['rounds_adjudicated'] for row in (before, after)):
        return {'status': 'pending_round_adjudication', 'common_targets': common}
    def target_round(row, origin):
        first = row['first_detection'][origin]
        return max(first[label] for label in common) if common and all(label in first for label in common) else None
    def model_seconds(row):
        number = target_round(row, 'model')
        return next((r['wall_seconds_at_response'] for r in row['measurement'].get('responses', [])
                     if r['round'] == number), None)
    return {'status': 'compared' if common else 'no_common_target', 'common_targets': common,
            'before_model_target_seconds': model_seconds(before), 'after_model_target_seconds': model_seconds(after),
            'same_final_true_positive_set': before['quality']['detected'] == after['quality']['detected'],
            'before_model_round': target_round(before, 'model'), 'after_model_round': target_round(after, 'model'),
            'before_system_round': target_round(before, 'system'), 'after_system_round': target_round(after, 'system'),
            'fixed_targets': sorted(k for k, v in LABELS.items() if v['bug']),
            'before_fixed_target_reached': all(k in before['quality']['detected'] for k, v in LABELS.items() if v['bug']),
            'after_fixed_target_reached': all(k in after['quality']['detected'] for k, v in LABELS.items() if v['bug']),
            'before_final_review_rounds': before['measurement']['attempts'],
            'after_final_review_rounds': after['measurement']['attempts'],
            'note': '共同目标为最终真阳性交集，属于探索性分析；程序阶段0表示模型尚未请求，不表示没有程序动作或耗时。未达到同目标不计算加速率。'}


def command_for(strategy, output, args):
    command = ['analyze', str(SOURCE), '--analysis-strategy', strategy, '--review-scope', 'files',
        '--model', args.model, '--ollama-url', args.ollama_url, '--max-rounds', str(args.max_rounds),
        '--max-calls', '10', '--context-chars', '30000', '--model-context', str(args.model_context), '--model-timeout', str(args.model_timeout),
        '--output', str(output)]
    if strategy == 'evidence':
        command += ['--evidence-policy', 'cost', '--max-evidence-actions', '1000', '--evidence-budget', '2000']
    return command


def summarize(output):
    data = json.loads((output / 'runs.json').read_text(encoding='utf-8'))
    assessment = json.loads((output / 'adjudication.json').read_text(encoding='utf-8'))
    lines = ['# 改进前后端到端对照', '',
        'before = checklist 原审查流程；after = evidence 三方案首期组合。两组均真实调用相同本地模型。', '',
        CONTRACT, '',
        '同一当前代码版本开关对比，保留共同缺陷修复；不是历史提交的复刻。标签不发送给模型。',
        '每次运行前用相同短请求预热；多次配对时按 AB/BA 交替，仅一对时为 AB。计时包含扫描、程序补查、模型审查和报告生成。',
        '请求数统计尝试次数（含失败）；Token 仅统计成功返回的服务计数。',
        '这是小型受控回归实验，不能代表真实项目总体效果；未使用 SBFL。', '',
        '## 准确性（不按耗时评分）', '',
        '仅评分最终正式输出的 IndexError；非目标意见单列，不当作正确检出，也不据此计算全部错误类型的误报率。', '',
        '| 重复 | 组 | 完成 | 输入有效 | TP | FP | FN | 模型单独 TP | 非目标意见 |',
        '|---|---|---|---|---:|---:|---:|---:|---:|']
    if data.get('validity_notes'):
        lines[2:2] = ['**有效性说明：** ' + note for note in data['validity_notes']] + ['']
    for row in data['runs']:
        report_bytes = (output / row['report']).read_bytes()
        if hashlib.sha256(report_bytes).hexdigest() != row['report_sha256']:
            raise ValueError('报告变化，不能继续使用旧裁决')
        reviewed = assessment.get(row['id'], {})
        final_decisions = reviewed.get('final', reviewed)
        result = score(row['alerts'], final_decisions)
        row['first_detection'] = first_detection_rounds(json.loads(report_bytes), row['alerts'],
            final_decisions, reviewed.get('rounds', {}))
        row['quality'] = result
        # An incomplete run may expose findings, but is not an efficiency success.
        model_alerts = [a for a in row['alerts'] if a['origin'] == 'model']
        model_ids = {a['id'] for a in model_alerts}
        row['model_quality'] = score(model_alerts, {k: v for k, v in final_decisions.items() if k in model_ids})
        quality = [result[k] for k in ('tp', 'fp', 'fn')] if row['complete'] and result['status'] == 'scored' else ['待核/未完成'] * 3
        lines.append(f"| {row['repeat']} | {row['strategy']} | {row['complete']} | {row.get('input_context_valid', '待核')} | "
            + ' | '.join(map(str, quality)) + f" | {row['model_quality'].get('tp', '待核')} | {len(result['out_of_scope'])} |")
    lines += ['', '语义裁决见 adjudication.json；原始报告、模型轨迹和日志保留在各组目录。',
        'TP 按真实缺陷去重，FP 按错误主张去重（包含错误触发条件），FN 包括未发现的阳性；未报警不等于证明安全。',
        '同一位置的程序结论正确、模型触发条件错误时，可同时计入一个 TP 和一个错误主张；另列模型单独 TP 与安全对照误报率。',
        '质量评分取静态报警、最终模型问题、程序条件风险的并集；SAFE 不抵消仍保留的模型误报。',
        '“请求/轮次”是本文件模型请求数；工具调用不含 submit_review，程序补查动作另记。', '', '## 每对变化', '']
    pairs = []
    for repeat in sorted({r['repeat'] for r in data['runs']}):
        rows = {r['strategy']: r for r in data['runs'] if r['repeat'] == repeat}
        if set(rows) != {'checklist', 'evidence'}:
            continue
        b, a = rows['checklist'], rows['evidence']
        if b['source_hashes'] != a['source_hashes']:
            raise ValueError('两组源码不同，不能配对')
        valid = b['complete'] and a['complete'] and all(r.get('input_context_valid') is not False for r in (b, a))
        changes = paired_changes(b['quality'], a['quality']) if valid else None
        efficiency = equal_target_efficiency(b, a)
        pairs.append({'repeat': repeat, 'complete': valid, 'changes': changes, 'efficiency': efficiency})
        if changes is not None:
            lines.append(f"- 第 {repeat} 对：新增检出 {changes['newly_detected']}；反向漏报 {changes['lost_detections']}；"
                f"消除安全对照误报 {changes['removed_false_controls']}；新增安全对照误报 {changes['added_false_controls']}。")
        else:
            lines.append(f'- 第 {repeat} 对：尚未完成运行或语义裁决，不能给出质量改善结论。')
    lines += ['', '## 同目标的轮次比较（与准确性分开）', '',
        '最终真阳性集合不同时，不用总耗时比较加速。下表只比较两组都正确检出的目标，另看最终复核轮次。',
        '程序阶段 0 表示在模型调用前给出结果，仍有程序补查动作和计算耗时；不能把它当成免费检查。', '',
        '| 重复 | 共同真阳性 | 模型首次集齐轮数：前/后 | 模型集齐同目标用时秒：前/后 | 含程序结果首次集齐轮数：前/后 | 最终复核轮次：前/后 |',
        '|---|---|---|---|---|---|']
    for pair in pairs:
        e = pair['efficiency']
        if e['status'] == 'compared':
            lines.append(f"| {pair['repeat']} | {e['common_targets']} | {e['before_model_round']}/{e['after_model_round']} | "
                f"{round(e['before_model_target_seconds'], 2) if e['before_model_target_seconds'] is not None else '未记录'}/{round(e['after_model_target_seconds'], 2) if e['after_model_target_seconds'] is not None else '未记录'} | "
                f"{e['before_system_round']}/{e['after_system_round']} | {e['before_final_review_rounds']}/{e['after_final_review_rounds']} |")
        else:
            lines.append(f"| {pair['repeat']} | 待核或无共同目标 | — | — | — | — |")
    lines += ['', '共同目标取最终真阳性交集，属于探索性结果；正式效率实验还应预先固定目标集合。',
        'None 表示该来源没有正式提出全部共同目标；不能将它作为 0 轮。首次正确输出按逐轮语义核对，不代表当轮已完成全部审查。',
        '预先定义的完整目标为 A/C/E；有一组未检出全部目标时，不计算完成同一完整目标的耗时或轮次加速。']
    lines.append('')
    for pair in pairs:
        e = pair['efficiency']
        if e['status'] == 'compared':
            lines.append(f"- 第 {pair['repeat']} 对达到 A/C/E 完整目标：改进前 {e['before_fixed_target_reached']}；改进后 {e['after_fixed_target_reached']}。")
    aggregate = {}
    lines += ['', '## 资源记录（不据此单独推断加速）', '',
        '| 重复 | 组 | 总秒数 | 模型秒数 | 请求/轮次 | 模型工具调用 | 程序动作 | 输入/输出 Token |',
        '|---|---|---:|---:|---:|---:|---:|---|']
    for row in data['runs']:
        m, u = row['measurement'], row['usage']
        lines.append(f"| {row['repeat']} | {row['strategy']} | {m['wall_seconds']:.2f} | {m['model_seconds']:.2f} | "
            f"{m['attempts']} | {row['tool_calls']} | {row['program_actions']} | {u['prompt_tokens']}/{u['completion_tokens']} |")
    lines += ['', '以下汇总仅包含已完成且输入有效的配对；不是不同准确性下的速度排名。', '']
    valid_repeats = {p['repeat'] for p in pairs if p['complete']}
    for strategy in ('checklist', 'evidence'):
        rows = [r for r in data['runs'] if r['strategy'] == strategy and r['repeat'] in valid_repeats]
        if rows:
            durations = [r['measurement']['wall_seconds'] for r in rows]
            aggregate[strategy] = {'pairs': len(rows), 'median_seconds': statistics.median(durations),
                'min_seconds': min(durations), 'max_seconds': max(durations),
                'mean_requests': statistics.mean(r['measurement']['attempts'] for r in rows)}
            lines.append(f"- {strategy}：{len(rows)} 次，耗时中位数 {statistics.median(durations):.2f} 秒，"
                f"范围 {min(durations):.2f}–{max(durations):.2f} 秒，平均请求 {aggregate[strategy]['mean_requests']:.1f} 次。")
    data.update(pairs=pairs, aggregate=aggregate)
    save(output / 'summary.json', data)
    (output / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')



# A path-specific regression: this function is safe for [], but buggy for [1].
# It is not labelled globally safe, and the old IndexError-only scores stay intact.
FALSE_POSITIVE_LABELS = {
    'EMPTY_INPUT': {'function': 'case_e', 'line': 23, 'bug': False,
        'condition': 'values == []; ordinary list and standard len/range',
        'reason': '空输入允许无操作；循环执行零次，正常返回 None，没有要求必须拒绝空输入。'},
    'NONEMPTY_BOUNDS': {'function': 'case_e', 'line': 25, 'bug': True,
        'condition': 'len(values) >= 1; touch is the unchanged local no-op',
        'reason': '非空输入末次访问 values[len(values)] 越界，不能为了减少报警漏掉此问题。'},
}


def empty_input_proof(source):
    """Check the exact AST needed for this path proof; never execute target code."""
    expected = ast.parse('def case_e(values: list[int]):\n'
        '    for i in range(len(values)):\n'
        '        touch(values)\n'
        '        item = values[i + 1]\n')
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'case_e']
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'touch']
    helper = ast.parse('def touch(values):\n    pass\n').body[0]
    if (any(not isinstance(node, ast.FunctionDef) for node in tree.body)
            or any(node.name in {'len', 'range'} for node in tree.body if isinstance(node, ast.FunctionDef))
            or len(functions) != 1 or len(helpers) != 1
            or ast.dump(functions[0]) != ast.dump(expected.body[0])
            or ast.dump(helpers[0]) != ast.dump(helper)):
        raise ValueError('回归源码已改变，旧的空输入/非空输入判定依据不再适用')
    return {'method': 'AST shape check plus ordinary Python list/range semantics; no target execution',
        'empty_length': len([]), 'empty_iterations': list(range(len([]))),
        'empty_path': '循环体执行零次，无其余语句，正常返回 None；空输入无操作是允许行为。',
        'positive_witness': {'input': [1], 'iteration': 0, 'index': 1, 'valid_indices': [0]},
        'assumptions': '普通 list、标准 len/range；没有额外业务需求要求拒绝空列表。'}


def false_positive_change(before, after):
    if before['status'] != 'scored' or after['status'] != 'scored':
        return {'status': 'pending_adjudication'}
    reproduced = 'EMPTY_INPUT' in before['false_controls']
    removed = reproduced and 'EMPTY_INPUT' not in after['false_controls']
    retained = 'NONEMPTY_BOUNDS' in before['detected'] and 'NONEMPTY_BOUNDS' in after['detected']
    return {'status': 'compared', 'baseline_false_positive_reproduced': reproduced,
        'target_false_positive_removed': removed, 'true_positive_preserved': retained,
        'target_regression_passed': reproduced and removed and retained,
        'causal_attribution': 'not_established',
        'scope': '仅观察到报告差异；未证明新方案识别并纠正该误报，也未证明差异由新方案造成。'}


def check_saved_regression_run(output, data, row):
    if not row['complete'] or row.get('input_context_valid') is not True:
        raise ValueError('误报回归只使用已完成且输入有效的真实运行')
    raw = (output / row['report']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != row['report_sha256']:
        raise ValueError('原报告变化，不能复用旧语义裁决')
    report = json.loads(raw)
    if (report['scanner']['status'] != 'completed' or not report['reviews']
            or any(r['status'] != 'completed' for r in report['reviews'])):
        raise ValueError('原报告没有完成检查，不能把少报当成误报改善')
    expected_hashes = {SOURCE.name: hashlib.sha256(SOURCE.read_bytes()).hexdigest()}
    if report['source_hashes'] != expected_hashes or row['source_hashes'] != expected_hashes:
        raise ValueError('报告源码与当前回归用例不一致')
    if artifacts(report) != row['alerts']:
        raise ValueError('缓存报警与原报告不一致')
    models = row.get('runtime_model', {}).get('models', [])
    if not any(m.get('digest') == data['model']['digest']
               and m.get('context_length') == data['model_context'] for m in models):
        raise ValueError('模型摘要或实际上下文不匹配')
    return report


def summarize_false_positive_regression(output):
    data = json.loads((output / 'runs.json').read_text(encoding='utf-8'))
    assessments = json.loads((output / 'adjudication.json').read_text(encoding='utf-8'))
    proof = empty_input_proof(SOURCE.read_text(encoding='utf-8'))
    rows, pairs = [], []
    lines = ['# 已知误报的前后回归对照', '',
        '复用同目录已经完成的真实模型运行；本命令不重新调用模型，不修改原报告或原 IndexError 评分。',
        '这是从原流程实际误报中选出的专项回归，不是随机测试集，也不是总体误报率评估。', '',
        '## 固定判定依据', '',
        '同一个 case_e 函数区分两条输入路径，不能把整个函数标成安全：', '',
        '| 路径 | 独立判定 | 原因 |', '|---|---|---|',
        '| values=[] | 正常路径 | len([])=0，range(0) 不迭代，循环体不执行，正常返回 None；没有必须拒绝空输入的需求。 |',
        '| values=[1] | 真实越界 | i=0 时访问 values[1]，超出只有一个元素的列表；touch 是本文件空函数。 |', '',
        '依据为 AST 结构核对和 Python 原生 len/range 语义；没有执行缺陷文件，也没有把 AST 匹配当作模型结论正确性的自动证明。', '',
        '## 前后结果', '',
        '| 重复 | 方案 | 本条空输入误报 | 同函数真实越界检出 | 核对状态 |', '|---|---|---:|---|---|']
    for row in data['runs']:
        check_saved_regression_run(output, data, row)
        decisions = assessments.get(row['id'], {}).get('false_positive_regression', {})
        quality = score(row['alerts'], decisions, FALSE_POSITIVE_LABELS)
        rows.append({'id': row['id'], 'repeat': row['repeat'], 'strategy': row['strategy'],
            'quality': quality, 'report': row['report'], 'report_sha256': row['report_sha256'],
            'scoped_decisions': decisions})
        fp = int('EMPTY_INPUT' in quality['false_controls']) if quality['status'] == 'scored' else '待核'
        tp = '是' if 'NONEMPTY_BOUNDS' in quality.get('detected', []) else '否/待核'
        lines.append(f"| {row['repeat']} | {row['strategy']} | {fp} | {tp} | {quality['status']} |")
    lines += ['', '## 原始报警与裁决', '']
    for row in data['runs']:
        decisions = assessments.get(row['id'], {}).get('false_positive_regression', {})
        for alert in row['alerts']:
            decision = decisions.get(alert['id'], {})
            if decision.get('status') == 'fp' and 'EMPTY_INPUT' in decision.get('labels', []):
                finding = alert['content']
                lines += [f"- {row['id']} / {alert['id']}：{finding['file']}:{finding['line']}「{finding['title']}」。",
                    f"  原解释：{finding['explanation']}", f"  触发条件：{finding['condition']}",
                    f"  判为误报的依据：{decision['reason']}"]
    lines += ['', '## 报告差异（不等于机制归因）', '']
    for repeat in sorted({r['repeat'] for r in rows}):
        pair = {r['strategy']: r for r in rows if r['repeat'] == repeat}
        if set(pair) != {'checklist', 'evidence'}:
            continue
        change = false_positive_change(pair['checklist']['quality'], pair['evidence']['quality'])
        pairs.append({'repeat': repeat, **change})
        if change['status'] == 'compared':
            lines.append(f"- 第 {repeat} 对：原报告存在该误报={change['baseline_false_positive_reproduced']}；"
                f"误报消失={change['target_false_positive_removed']}；真实越界仍检出={change['true_positive_preserved']}；"
                f"报告差异核对通过={change['target_regression_passed']}；新方案纠错归因=未证实。")
        else:
            lines.append(f'- 第 {repeat} 对：裁决不完整，不给出改善结论。')
    lines += ['', '先前的“越界误报 0”只统计 IndexError；这条“空输入未处理”曾列在范围外意见。本专项明确增加该正常路径的功能误报判定，没有改写先前结果。',
        '其他函数问题仅在此专项中不计分，仍保留在原报告及全套准确性汇总里。',
        '这里只能说明新报告没有这条误报，并且相邻真实缺陷没有漏掉。新流程从未提交空输入误报，没有针对它的程序冲突或撤回记录；不能声称新方案识别并纠正了该误报，也不能声称已经证明整个新方案导致这一差异。',
        '语义裁决由助手逐条阅读源码和报告完成，未进行独立盲评；没有进行运行时间或轮次的效率比较。', '']
    result = {'schema_version': 1, 'kind': 'known_false_positive_regression_reusing_real_runs',
        'selected_after_observation': True, 'new_model_calls': 0, 'model': data['model'],
        'model_context': data['model_context'], 'source_sha256': data['source_sha256'],
        'labels': FALSE_POSITIVE_LABELS, 'proof': proof, 'runs': rows, 'pairs': pairs}
    save(output / 'false-positive-review.json', result)
    (output / 'false-positive-review.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'runs.json').exists():
        raise ValueError('输出目录已有对照记录，请选择新目录，避免覆盖证据')
    identity = model_identity(args.ollama_url, args.model)
    source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    code_hashes = {str(p.relative_to(ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted((ROOT.parent / 'src/evidence_review').rglob('*.py'))}
    data = {'schema_version': 1, 'kind': 'paired_end_to_end', 'model': identity,
        'model_context': args.model_context, 'platform': platform.platform(), 'python': sys.version, 'source_sha256': source_hash,
        'code_hashes': code_hashes, 'labels': LABELS, 'contract': CONTRACT, 'runs': []}
    assessments = {}
    for repeat in range(1, args.repeats + 1):
        order = ('checklist', 'evidence') if repeat % 2 else ('evidence', 'checklist')
        for strategy in order:
            if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != source_hash:
                raise ValueError('实验中源码发生变化')
            if model_identity(args.ollama_url, args.model) != identity:
                raise ValueError('实验中模型发生变化')
            warmup = api(args.ollama_url, 'chat', {'model': args.model, 'stream': False, 'think': False,
                'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                'options': {'temperature': 0, 'num_predict': 1, 'num_ctx': args.model_context}}, args.model_timeout)
            run_id = f'{repeat:02d}-{strategy}'
            destination = output / run_id
            destination.mkdir()
            measurement = {'attempts': 0, 'failed_requests': 0, 'model_seconds': 0.0, 'responses': []}

            class MeteredOllama(Ollama):
                def chat(self, *values, **kwargs):
                    measurement['attempts'] += 1
                    tick = time.monotonic()
                    prior = dict(self.usage)
                    try:
                        return super().chat(*values, **kwargs)
                    except Exception:
                        measurement['failed_requests'] += 1
                        raise
                    finally:
                        measurement['model_seconds'] += time.monotonic() - tick
                        measurement['responses'].append({'round': measurement['attempts'],
                            'seconds': time.monotonic() - tick, 'wall_seconds_at_response': time.monotonic() - run_started,
                            'prompt_tokens': self.usage['prompt_tokens'] - prior['prompt_tokens'],
                            'completion_tokens': self.usage['completion_tokens'] - prior['completion_tokens']})

            command = command_for(strategy, destination, args)
            old = cli.Ollama
            with (destination / 'run.log').open('w', encoding='utf-8') as log:
                with redirect_stdout(LiveLog(sys.stdout, log)), redirect_stderr(LiveLog(sys.stderr, log)):
                    print(f'开始配对实验 {run_id}', flush=True)
                    cli.Ollama = MeteredOllama
                    tick = run_started = time.monotonic()
                    try:
                        exit_code = cli.main(command)
                    finally:
                        measurement['wall_seconds'] = time.monotonic() - tick
                        cli.Ollama = old
            path = destination / 'report.json'
            if not path.exists():
                raise ValueError(f'{run_id} 未生成报告；查看 run.log')
            report = json.loads(path.read_text(encoding='utf-8'))
            alerts = artifacts(report)
            assessments[run_id] = {'final': {a['id']: {'status': 'pending', 'reason': ''} for a in alerts}, 'rounds': {}}
            row = {'id': run_id, 'repeat': repeat, 'strategy': strategy, 'command': command,
                'complete': exit_code == 0 and bool(report['reviews']) and all(r['status'] == 'completed' for r in report['reviews']),
                'exit_code': exit_code, 'measurement': measurement, 'usage': report['model_usage'],
                'source_hashes': report['source_hashes'], 'alerts': alerts,
                'tool_calls': sum(e['tool'] != 'submit_review' for r in report['reviews'] for e in r['trace']),
                'program_actions': report.get('program_analysis', {}).get('summary', {}).get('actions', 0),
                'runtime_model': api(args.ollama_url, 'ps'),
                'warmup': {k: warmup.get(k) for k in ('load_duration', 'total_duration', 'eval_count')},
                'report': str(path.relative_to(output)), 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            data['runs'].append(row)
            save(output / 'runs.json', data)
            save(output / 'adjudication.json', assessments)
            summarize(output)
    return 0 if all(r['complete'] for r in data['runs']) else 2



# A frozen real model submission makes correction, rather than fresh generation,
# the outcome. This is a mechanism experiment, not an end-to-end accuracy score.
class FrozenFirstClient:
    def __init__(self, delegate, submission):
        self.delegate = delegate
        self.submission = deepcopy(submission)
        self.calls = 0
        self.requests = []

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'name': 'submit_review', 'arguments': deepcopy(self.submission)}}]}
        self.requests.append(deepcopy({'messages': messages, 'tools': tools}))
        return self.delegate.chat(messages, tools)


def correction_request_without_intervention(request):
    """Only the existing program-analysis fields may differ at the fork."""
    request = deepcopy(request)
    for message in request['messages']:
        if message['role'] == 'user':
            payload = json.loads(message['content'])
            payload.pop('program_analysis', None)
            payload.pop('program_analysis_rules', None)
            payload.get('audit_plan', {}).pop('program_analysis', None)
            message['content'] = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        elif message['role'] == 'tool':
            payload = json.loads(message['content'])
            payload.pop('program_conflicts', None)
            message['content'] = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return request


def correction_outcome(result):
    if result['status'] != 'completed':
        return {'status': 'incomplete'}
    review_result = result['review']
    final = review_result['findings']
    withdrawn = review_result.get('withdrawn_findings', [])
    # New claims need semantic adjudication; do not score them by location alone.
    if any(f['finding_id'] not in {'F1', 'F2'} for f in final):
        return {'status': 'pending_adjudication'}
    retained = any(f['finding_id'] == 'F1' for f in final)
    removed = (any(f['finding_id'] == 'F2' for f in withdrawn)
               and not any(f['finding_id'] == 'F2' for f in final))
    conflicts = [c for event in result['trace'] for c in event['response'].get('program_conflicts', [])
                 if c['finding_id'] == 'F2']
    return {'status': 'scored', 'false_positive_withdrawn': removed,
            'true_positive_retained': retained, 'conflict_feedback_observed': bool(conflicts),
            'withdrawal_reasons': [f['withdraw_reason'] for f in withdrawn if f['finding_id'] == 'F2']}


def correction_pair(before, after, same_start):
    if not same_start or any(r['status'] != 'scored' for r in (before, after)):
        return 'invalid_or_unadjudicated'
    if not after['true_positive_retained']:
        return 'true_positive_regression'
    if before['false_positive_withdrawn'] and after['false_positive_withdrawn']:
        return ('both_corrected_no_incremental_benefit' if before['true_positive_retained']
                else 'both_corrected_baseline_lost_true_positive')
    if not before['false_positive_withdrawn'] and after['false_positive_withdrawn']:
        return ('incremental_correction_observed' if after['conflict_feedback_observed']
                else 'missing_mechanism_trace')
    return ('treatment_failed' if before['false_positive_withdrawn']
            else 'neither_corrected')


def run_correction_experiment(args):
    from evidence_review.agent import review
    from evidence_review.project import Project
    from evidence_review.evidence import analyze_evidence

    source = ROOT / 'cases/evidence_bounds/model_smoke.py'
    reference = args.correction_reference.resolve()
    raw = reference.read_bytes()
    old = json.loads(raw)
    project = Project(source)
    if old['source_hashes'] != project.hashes():
        raise ValueError('固定误报的原报告与当前源码不匹配')
    submission = old['reviews'][0]['trace'][0]['arguments']
    expected = [(3, '索引越界风险'), (7, '索引越界风险')]
    if [(f['line'], f['title']) for f in submission['findings']] != expected:
        raise ValueError('原提交不是人工核对的 F1 真实越界 / F2 安全误报，需重新裁决')
    if project.hashes() != {'model_smoke.py': '3494be870d43b6e8690eb1a60445047d99b2c4996e1ea1b9f55d882dc86ea168'}:
        raise ValueError('源码改变，独立标签需要重新核对')
    candidate = old['candidates'][0]
    analysis = analyze_evidence(project, policy='cost', max_actions=1000, max_cost=2000)
    if [(c['line'], c['verdict']) for c in analysis['claims']] != [(3, 'VIOLATION'), (7, 'SAFE')]:
        raise ValueError('程序证据不符合当前已核对的用例，停止实验')
    output = args.output.resolve()
    if (output / 'correction-experiment.json').exists():
        raise ValueError('已有纠错实验记录，拒绝覆盖')
    output.mkdir(parents=True, exist_ok=True)
    identity = model_identity(args.ollama_url, args.model)
    data = {'kind': 'frozen_real_submission_correction_experiment',
        'source_hashes': project.hashes(), 'reference_report': str(reference),
        'reference_sha256': hashlib.sha256(raw).hexdigest(), 'model': identity,
        'model_context': args.model_context, 'frozen_submission': submission,
        'program_analysis': analysis, 'max_live_rounds': args.max_rounds,
        'frozen_submission_origin_strategy': old.get('analysis_strategy'),
        'code_hashes': {str(p.relative_to(ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in [Path(__file__), *sorted((ROOT.parent/'src/evidence_review').rglob('*.py'))]},
        'scope': '固定真实误报后比较纠错；首轮为回放，不是本次新生成。仅后续复核调用真实模型；不代表原流程自然产生该误报的频率。',
        'runs': [], 'pairs': []}
    for repeat in range(1, args.repeats + 1):
        pair = {}
        order = ('checklist', 'evidence') if repeat % 2 else ('evidence', 'checklist')
        for strategy in order:
            print(f'纠错对照 {repeat}/{args.repeats} {strategy}：回放同一提交，开始真实模型复核', flush=True)
            delegate = Ollama(args.model, args.ollama_url, args.model_timeout, args.model_context)
            client = FrozenFirstClient(delegate, submission)
            result = review(project, candidate, client, budget=60000,
                max_rounds=args.max_rounds + 1, max_calls=20,
                program_analysis=analysis if strategy == 'evidence' else None,
                progress=lambda message: print(message, flush=True))
            runtime = api(args.ollama_url, 'ps')
            context_valid = any(m.get('digest') == identity['digest']
                and m.get('context_length') == args.model_context for m in runtime.get('models', []))
            outcome = correction_outcome(result) if context_valid else {'status': 'invalid_context'}
            row = {'repeat': repeat, 'strategy': strategy, 'result': result,
                   'outcome': outcome, 'live_requests': client.requests, 'usage': delegate.usage,
                   'runtime_model': runtime, 'input_context_valid': context_valid}
            pair[strategy] = row
            data['runs'].append(row)
            save(output / 'correction-experiment.json', data)
        before, after = pair['checklist'], pair['evidence']
        same = (bool(before['live_requests'] and after['live_requests'])
            and correction_request_without_intervention(before['live_requests'][0])
                == correction_request_without_intervention(after['live_requests'][0]))
        data['pairs'].append({'repeat': repeat, 'same_start_except_intervention': same,
            'outcome': correction_pair(before['outcome'], after['outcome'], same)})
        save(output / 'correction-experiment.json', data)
    summarize_correction_experiment(output)
    return 0 if all(r['outcome']['status'] == 'scored' for r in data['runs']) else 2


def summarize_correction_experiment(output):
    data = json.loads((output / 'correction-experiment.json').read_text(encoding='utf-8'))
    for row in data['runs']:
        row['outcome'] = (correction_outcome(row['result']) if row['input_context_valid']
                          else {'status': 'invalid_context'})
    data['pairs'] = []
    for repeat in sorted({r['repeat'] for r in data['runs']}):
        pair = {r['strategy']: r for r in data['runs'] if r['repeat'] == repeat}
        if set(pair) != {'checklist', 'evidence'}:
            raise ValueError('对照未完成，不能生成归因结论')
        before, after = pair['checklist'], pair['evidence']
        same = (bool(before['live_requests'] and after['live_requests'])
            and correction_request_without_intervention(before['live_requests'][0])
                == correction_request_without_intervention(after['live_requests'][0]))
        data['pairs'].append({'repeat': repeat, 'same_start_except_intervention': same,
            'outcome': correction_pair(before['outcome'], after['outcome'], same)})
    data['summary_code_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    save(output / 'correction-experiment.json', data)
    lines = ['# 固定同一误报的纠错对照', '', data['scope'], '',
        '共同输入来自已有真实模型提交：F1 是第 3 行真实越界，F2 是第 7 行安全访问误报。',
        '独立依据：普通 list 与内置 len/range 前提下，第 3 行 n=1 时访问下标 1；第 7 行 n<=1 不执行，n>=2 时最大下标为 n-1。',
        '每组保留原流程的复核指令和撤回能力。实验组只增加程序分析、审查计划中的程序证据以及冲突反馈；首个真实请求逐字段校验其他内容一致。',
        '这隔离的是当前组合方案的证据干预，不能分别归因到三个组件；本小用例也不检验成本策略的优势。', '',
        '| 重复 | 方案 | F2 撤回 | F1 保留 | F2 程序冲突反馈 | 状态 |', '|---|---|---|---|---|---|']
    for row in data['runs']:
        o = row['outcome']
        lines.append(f"| {row['repeat']} | {row['strategy']} | {o.get('false_positive_withdrawn')} | {o.get('true_positive_retained')} | {o.get('conflict_feedback_observed')} | {o['status']} |")
    meanings = {
        'both_corrected_baseline_lost_true_positive': '两组都撤回误报，无法把误报消除归因于新增方案。旧流程还错误撤回真实越界，新流程保留真实越界；本例观察到的是保留真实问题的优势。',
        'true_positive_regression': '实验组丢失真实问题，不能算纠错成功。',
        'both_corrected_no_incremental_benefit': '两组都纠正误报，未观察到新方案额外消除误报的收益；不能把撤回归功于新增方案。',
        'incremental_correction_observed': '本对照中仅加入方案后撤回，真实问题保留且有冲突反馈；支持此固定起点下的增量纠错，不代表普遍因果效果。'}
    for pair_result in data['pairs']:
        lines += ['', f"第 {pair_result['repeat']} 对：共同起点校验={pair_result['same_start_except_intervention']}。"
                  + meanings.get(pair_result['outcome'], pair_result['outcome'])]
    lines += ['', '## 实际撤回依据', '']
    for row in data['runs']:
        for reason in row['outcome'].get('withdrawal_reasons', []):
            lines.append(f"- {row['repeat']} / {row['strategy']}：{reason}")
    lines += ['', '完整提交、程序证据、实际模型请求、回复与最终结果均保存在同目录 JSON。未根据试验结果改写初始误报或挑选有利重复。',
              '不把回放首轮计为真实模型生成；本实验不报告速度提升。原始误报来自 evidence 集成运行，因此不能称为旧方案自然误报的端到端复现。', '']
    (output / 'correction-experiment.md').write_text('\n'.join(lines), encoding='utf-8')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='qwen3:8b')
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11435')
    parser.add_argument('--repeats', type=cli.positive, default=3)
    parser.add_argument('--max-rounds', type=cli.positive, default=6)
    parser.add_argument('--model-timeout', type=cli.positive, default=300)
    parser.add_argument('--model-context', type=cli.positive, default=40960)
    parser.add_argument('--output', type=Path, default=ROOT / 'results-before-after')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--summarize-only', action='store_true')
    mode.add_argument('--false-positive-regression', action='store_true',
                      help='复用已有真实运行，核对空输入误报与同函数非空越界；不调用模型')
    mode.add_argument('--correction-experiment', action='store_true', help='固定同一真实误报，调用模型比较前后纠错')
    parser.add_argument('--correction-reference', type=Path, default=ROOT.parent / 'reports/evidence-model-smoke/report.json')
    mode.add_argument('--summarize-correction', action='store_true', help='只重新汇总已有纠错轨迹，不调用模型')
    args = parser.parse_args()
    if args.summarize_correction:
        summarize_correction_experiment(args.output.resolve())
        return 0
    if args.correction_experiment:
        return run_correction_experiment(args)
    if args.false_positive_regression:
        summarize_false_positive_regression(args.output.resolve())
        return 0
    if args.summarize_only:
        summarize(args.output.resolve())
        return 0
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
