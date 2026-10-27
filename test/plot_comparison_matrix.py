"""Validate adjudication and produce the matrix report and static figures."""
import argparse
from collections import Counter
import hashlib
import json
import os
import statistics
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize_timing(rows):
    measured = [row for row in rows if isinstance(row.get('measurement', {}).get('wall_seconds'), (int, float))]
    if not measured:
        return None
    wall = [row['measurement']['wall_seconds'] for row in measured]
    runtime = [response['runtime'] for row in measured for response in row['measurement'].get('responses', []) if response.get('runtime')]
    return {'runs': len(measured), 'mean_seconds': statistics.mean(wall),
        'median_seconds': statistics.median(wall), 'total_seconds': sum(wall),
        'min_seconds': min(wall), 'max_seconds': max(wall),
        'runtime_records': len(runtime),
        'request_bytes': sum(item.get('request_bytes', 0) for item in runtime),
        'thinking_requests': sum(item.get('thinking_requested') is True for item in runtime),
        'server_seconds': {key: sum(item.get(key + '_seconds', 0) for item in runtime)
            for key in ('load_duration', 'prompt_eval_duration', 'eval_duration', 'total_duration')},
        'requests': sum(row['measurement']['attempts'] for row in measured),
        'model_seconds': sum(row['measurement']['model_seconds'] for row in measured),
        'prompt_tokens': sum(row['usage']['prompt_tokens'] for row in measured),
        'completion_tokens': sum(row['usage']['completion_tokens'] for row in measured),
        'non_model_seconds': sum(row['measurement']['wall_seconds'] - row['measurement']['model_seconds'] for row in measured),
        'stage_seconds': {key: sum(row.get('stage_timings', {}).get(key, 0) for row in measured)
                          for key in ('index_seconds', 'scanner_seconds', 'candidate_seconds', 'evidence_seconds', 'review_seconds')}}


def summarize_results(rows, groups):
    """Keep system errors and error-free runs visible alongside model-only hits."""
    counts = Counter({key: 0 for key in
        ('tp', 'fp', 'fn', 'model_tp', 'safe_case_false_alerts', 'fully_correct_runs')})
    for row in rows:
        for key in ('tp', 'fp', 'fn'):
            counts[key] += row['quality'][key]
        counts['model_tp'] += row['model_quality']['tp']
        counts['safe_case_false_alerts'] += len(row['quality']['false_controls'])
        counts['fully_correct_runs'] += int(row['quality']['fn'] == 0 and row['quality']['fp'] == 0)
        counts.update(row['false_claim_categories'])
    return {**dict(counts), 'runs': len(rows), 'timing': summarize_timing(rows),
        'positive_opportunities': sum(sum(label['bug'] for label in groups[row['group']]['labels'].values()) for row in rows),
        'safe_opportunities': sum(sum(not label['bug'] for label in groups[row['group']]['labels'].values()) for row in rows)}


def load_scored(output):
    from run_before_after import artifacts, score, paired_changes
    data = json.loads((output/'runs.json').read_text())
    decisions = json.loads((output/'adjudication.json').read_text())
    groups = {g['id']: g for g in data['manifest']['groups']}
    expected = {(g, r, s) for g in groups for r in range(1, data['repeats']+1)
                for s in ('checklist', 'evidence')}
    actual = [(r['group'], r['repeat'], r['strategy']) for r in data['runs']]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError('运行矩阵不完整或存在重复，拒绝生成完整效果图')
    manifest_path = ROOT/'cases/evidence_bounds/matrix/manifest.json'
    if digest(manifest_path) != data['manifest_sha256'] or json.loads(manifest_path.read_text()) != data['manifest']:
        raise ValueError('预先登记的标签或分组已变化，拒绝使用旧运行计分')
    rows = []
    for row in data['runs']:
        path = output/row['report']
        if digest(path) != row['report_sha256']:
            raise ValueError('报告变化，裁决失效')
        group = groups[row['group']]
        source = ROOT/group['source']
        if digest(source) != group['sha256'] or row['source_hashes'] != {source.name: group['sha256']}:
            raise ValueError('源码与预先登记的用例不一致')
        annotation = decisions[row['id']]
        if annotation['report_sha256'] != row['report_sha256']:
            raise ValueError('裁决对应的报告不匹配')
        report = json.loads(path.read_text())
        if artifacts(report) != row['alerts']:
            raise ValueError('缓存报警与原报告不一致')
        valid = row['complete'] and row['input_context_valid']
        quality = score(row['alerts'], annotation['final'], group['labels'])
        model_alerts = [a for a in row['alerts'] if a['origin'] == 'model']
        model_ids = {a['id'] for a in model_alerts}
        model_quality = score(model_alerts,
            {k: v for k, v in annotation['final'].items() if k in model_ids}, group['labels'])
        if quality['status'] != 'scored':
            raise ValueError(f"{row['id']} 尚未逐条裁决")
        fp_keys = sorted({d['issue_key'] for d in annotation['final'].values() if d['status'] == 'fp'})
        fp_categories = dict(Counter(
            {d['issue_key']: d.get('category', 'other_false_claim')
             for d in annotation['final'].values() if d['status'] == 'fp'}.values()))
        fp_lines = sorted({a['content']['line'] for a in row['alerts']
            if annotation['final'][a['id']]['status'] == 'fp' and isinstance(a['content'].get('line'), int)})
        events = []
        for review in report['reviews']:
            withdrawn = {f['finding_id']: f for f in (review.get('review') or {}).get('withdrawn_findings', [])}
            for event in review['trace']:
                for conflict in event['response'].get('program_conflicts', []):
                    finding = withdrawn.get(conflict['finding_id'])
                    if finding:
                        events.append({'round': event['round'], 'file': finding['file'], 'line': finding['line'],
                            'finding_id': finding['finding_id'], 'title': finding['title'],
                            'withdraw_reason': finding['withdraw_reason'], 'conflict': conflict})
        reconciliations = [item for review in report['reviews'] for item in review.get('audit', {}).get('program_reconciliations', [])]
        rows.append({**row, 'valid': valid, 'quality': quality, 'model_quality': model_quality, 'program_reconciliations': reconciliations,
                     'false_claim_keys': fp_keys, 'false_claim_lines': fp_lines, 'false_claim_categories': fp_categories, 'program_corrections': events})
    pairs = []
    for group in groups:
        for repeat in range(1, data['repeats']+1):
            pair = {r['strategy']: r for r in rows if r['group'] == group and r['repeat'] == repeat}
            before, after = pair['checklist'], pair['evidence']
            pairs.append({'group': group, 'repeat': repeat, 'valid': before['valid'] and after['valid'],
                'changes': paired_changes(before['quality'], after['quality']),
                'removed_false_claims': sorted(set(before['false_claim_keys'])-set(after['false_claim_keys'])),
                'added_false_claims': sorted(set(after['false_claim_keys'])-set(before['false_claim_keys']))})
    totals = {}
    valid_pair_keys = {(p['group'], p['repeat']) for p in pairs if p['valid']}
    if not valid_pair_keys:
        raise ValueError('没有有效完成的配对，不能将空图当作效果结果')
    for strategy in ('checklist', 'evidence'):
        selected = [r for r in rows if r['strategy'] == strategy and (r['group'], r['repeat']) in valid_pair_keys]
        totals[strategy] = summarize_results(selected, groups)
    transitions = {
        'new_detections': sum(len(p['changes']['newly_detected']) for p in pairs if p['valid']),
        'lost_detections': sum(len(p['changes']['lost_detections']) for p in pairs if p['valid']),
        'removed_false_claims': sum(len(p['removed_false_claims']) for p in pairs if p['valid']),
        'added_false_claims': sum(len(p['added_false_claims']) for p in pairs if p['valid'])}
    same_target_pairs = []
    for pair in pairs:
        if not pair['valid']:
            continue
        selected = {row['strategy']: row for row in rows if row['group'] == pair['group'] and row['repeat'] == pair['repeat']}
        b, a = selected['checklist'], selected['evidence']
        if b['quality']['detected'] and b['quality']['detected'] == a['quality']['detected']:
            same_target_pairs.append({'group': pair['group'], 'repeat': pair['repeat'],
                'targets': b['quality']['detected'], 'before': b, 'after': a})
    equal_target_timing = {'pairs': len(same_target_pairs),
        'targets': [{'group': p['group'], 'repeat': p['repeat'], 'labels': p['targets']} for p in same_target_pairs],
        'before': summarize_timing([p['before'] for p in same_target_pairs]),
        'after': summarize_timing([p['after'] for p in same_target_pairs]),
        'note': '仅有效配对且两组最终正确检出集合相同、非空；比较完成全流程的耗时，不是首次检出时刻。不得外推整个缺陷集的同质量加速。'}
    return {**data, 'runs': rows, 'pairs': pairs, 'totals': totals, 'transitions': transitions,
            'equal_target_timing': equal_target_timing,
            'quality_gate': {'no_additional_misses': totals['evidence']['fn'] <= totals['checklist']['fn'],
                             'no_additional_false_claims': totals['evidence']['fp'] <= totals['checklist']['fp'],
                             'no_new_false_claims': transitions['added_false_claims'] == 0},
            'adjudication_sha256': digest(output/'adjudication.json'),
            'interpretation_notes': decisions.get('_interpretation', []),
            'scoring': '助手逐条核对原始结论，未盲评；程序与模型重复检出按独立缺陷去重；SAFE 不抵消仍保留的模型错误主张，程序校正须有明确记录。'}


def plot(data, output, font_path=None):
    with tempfile.TemporaryDirectory(prefix='evidence-plot-') as tmp:
        os.environ['MPLCONFIGDIR'] = tmp
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.font_manager import FontProperties
        from matplotlib.colors import ListedColormap
        candidates = [font_path, '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
            'C:/Windows/Fonts/msyh.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
        font = next((Path(p) for p in candidates if p and Path(p).exists()), None)
        if not font:
            raise ValueError('需要中文字体；通过 --font 指定字体文件')
        from matplotlib import font_manager
        font_manager.fontManager.addfont(str(font))
        plt.rcParams.update({'font.family': FontProperties(fname=str(font)).get_name(),
            'axes.unicode_minus': False, 'font.size': 11, 'axes.spines.top': False,
            'axes.spines.right': False, 'figure.facecolor': '#f5f7fb', 'axes.facecolor': '#f5f7fb'})
        colors = ['#425e89', '#168b83']
        before, after = data['totals']['checklist'], data['totals']['evidence']
        fig, axes = plt.subplots(1, 3, figsize=(15, 6.5))
        for ax, key, title, unit in [
            (axes[0], 'time', '全流程检查时长 · 越低越好', '平均秒/次'),
            (axes[1], 'fn', '最终系统漏检 · 越低越好', '漏检机会数'),
            (axes[2], 'fp', '最终错误问题项 · 越低越好', '错误主张数')]:
            values = [(totals['timing']['mean_seconds'] if key == 'time' else totals[key]) for totals in (before, after)]
            bars = ax.bar([0, 1], values, width=.58, color=colors)
            ax.bar_label(bars, labels=[f'{v:.1f}' if key == 'time' else str(v) for v in values], padding=7, fontsize=17)
            ax.set_xticks([0, 1], ['加入前 checklist', '加入后 evidence'])
            ax.set_ylabel(unit)
            maximum = max(*values, 1)
            ax.set_ylim(0, maximum * 1.3 + (0 if key == 'time' else 1))
            if key != 'time':
                ax.yaxis.get_major_locator().set_params(integer=True)
            ax.set_title(title, loc='left', fontsize=14, pad=20)
            ax.set_axisbelow(True)
            ax.grid(axis='y', alpha=.2)
        fig.suptitle('组合方案加入前后 · 时长、漏检与误报', x=.06, ha='left', fontsize=21, fontweight='bold')
        fig.text(.06,.90, f"5 组 / 10 个代码案例 × {data['repeats']} 次重复；同源码、同模型、同预算，交换运行顺序", color='#526174')
        fig.text(.06,.13, f"系统正确检出 {before['tp']}/{before['positive_opportunities']} → {after['tp']}/{after['positive_opportunities']}；无漏检且无误报的运行 {before['fully_correct_runs']}/{before['runs']} → {after['fully_correct_runs']}/{after['runs']}。模型与程序结论去重计分。", fontsize=11, color='#203245')
        fig.text(.06,.04, f"有效配对 {before['runs']} / {len(data['pairs'])}；每个方案 {before['positive_opportunities']} 次真实缺陷检出机会、{before['safe_opportunities']} 次安全案例检查。\n总耗时包含扫描、证据计算、模型审查和报告生成；不含两组相同预热。不同检出集合不直接推断同质量加速，见报告同目标子集。", fontsize=10, color='#526174')
        fig.subplots_adjust(top=.78,bottom=.23,left=.06,right=.98,wspace=.32)
        fig.savefig(output/'comparison.png', dpi=180)
        plt.close(fig)
        groups = data['manifest']['groups']
        cells, annotations, labels = [], [], []
        for group in groups:
            for key, label in group['labels'].items():
                values, texts = [], []
                for repeat in range(1,data['repeats']+1):
                    for strategy in ('checklist','evidence'):
                        row = next(r for r in data['runs'] if r['group']==group['id'] and r['repeat']==repeat and r['strategy']==strategy)
                        if not row['valid']:
                            value,text = 4,'未完成/无效'
                        elif label['bug']:
                            hit = key in row['quality']['detected']
                            value,text = (0,'正确检出') if hit else (2,'漏报')
                            if hit and key not in row['model_quality']['detected']:
                                text += '\n程序参与'
                            if label['line'] in row['false_claim_lines']:
                                text += '\n另有错误问题项'
                        else:
                            fp = key in row['quality']['false_controls']
                            value,text = (3,'误报') if fp else (1,'无误报')
                        values.append(value);texts.append(text)
                cells.append(values);annotations.append(texts)
                labels.append(f"{group['title']} · {key} {'真实缺陷' if label['bug'] else '安全对照'}")
        fig, ax = plt.subplots(figsize=(13,8))
        cmap = ListedColormap(['#c6e7e2','#e3eaf3','#f0b8bd','#f4d49b','#d8d8d8'])
        ax.imshow(cells,cmap=cmap,vmin=-.5,vmax=4.5,aspect='auto')
        ax.set_xticks(range(data['repeats']*2),[f'第 {r} 次 · {s}' for r in range(1,data['repeats']+1) for s in ['加入前','加入后']])
        ax.xaxis.tick_top()
        ax.set_yticks(range(len(labels)),labels)
        for i,row in enumerate(annotations):
            for j,value in enumerate(row):
                ax.text(j,i,value,ha='center',va='center',fontsize=10 if value.count('\n') > 1 else 11,color='#203245')
        ax.set_xticks([j-.5 for j in range(data['repeats']*2+1)],minor=True)
        ax.set_yticks([j-.5 for j in range(len(labels)+1)],minor=True)
        ax.grid(which='minor',color='white',linewidth=3)
        ax.tick_params(which='both',length=0)
        fig.suptitle('逐案例对照 · 所有运行均保留',x=.04,ha='left',fontsize=21,fontweight='bold')
        fig.text(.04,.04,'正确检出按模型＋程序的最终结果计分；“程序参与”表示程序验证或校正参与最终结论，不表示模型原意见一定错误。\n无误报仅表示未保留错误报警，不表示模型证明了安全。真实缺陷上的其他错误主张另计入总误报。',fontsize=10,color='#526174')
        fig.subplots_adjust(top=.86,bottom=.14,left=.30,right=.97)
        fig.savefig(output/'case-matrix.png',dpi=180)
        plt.close(fig)


def timing_report(data):
    before, after = data['totals']['checklist']['timing'], data['totals']['evidence']['timing']
    lines = ['', '## 检查时长与慢因定位', '',
        '全流程计时包含索引、静态扫描、证据补查、全部模型轮次和报告生成；每次相同的单 Token 预热排除在计时外。成功与失败的模型尝试均计入，输入超限或未完成运行不进入有效配对。', '',
        '| 指标 | 加入前 | 加入后 |', '|---|---:|---:|']
    for label, key, digits in [('总耗时（秒）', 'total_seconds', 2), ('平均耗时（秒/次）', 'mean_seconds', 2),
            ('中位耗时（秒/次）', 'median_seconds', 2), ('模型请求/轮次总数', 'requests', 0),
            ('模型请求耗时（秒）', 'model_seconds', 2), ('其余耗时（秒）', 'non_model_seconds', 2),
            ('输入 Token', 'prompt_tokens', 0), ('生成 Token（含服务计入的思考）', 'completion_tokens', 0)]:
        lines.append(f"| {label} | {before[key]:.{digits}f} | {after[key]:.{digits}f} |")
    if before['runtime_records'] and after['runtime_records']:
        lines += ['', '### 服务分段耗时与输入负担（成功响应累计）', '',
            '| 指标 | 加入前 | 加入后 |', '|---|---:|---:|',
            f"| 有分段记录的请求数 | {before['runtime_records']} | {after['runtime_records']} |",
            f"| 请求体累计字节 | {before['request_bytes']} | {after['request_bytes']} |",
            f"| 开启循环边界思考的请求数 | {before['thinking_requests']} | {after['thinking_requests']} |"]
        for label, key in [('加载耗时秒', 'load_duration'), ('输入处理耗时秒', 'prompt_eval_duration'), ('生成耗时秒', 'eval_duration')]:
            lines.append(f"| {label} | {before['server_seconds'][key]:.2f} | {after['server_seconds'][key]:.2f} |")
        lines += ['', f"模型请求占全流程耗时：{before['model_seconds']/before['total_seconds']:.1%} → {after['model_seconds']/after['total_seconds']:.1%}。",
            f"索引/扫描/证据补查累计秒：前 {before['stage_seconds']['index_seconds']:.3f}/{before['stage_seconds']['scanner_seconds']:.3f}/{before['stage_seconds']['evidence_seconds']:.4f}；后 {after['stage_seconds']['index_seconds']:.3f}/{after['stage_seconds']['scanner_seconds']:.3f}/{after['stage_seconds']['evidence_seconds']:.4f}。",
            '服务分段值来自 Ollama 成功响应的纳秒字段换算；与客户端墙钟不完全相等，不能重复相加到总耗时。未决边界任务仍保留思考模式，不能为了提速跳过未知问题。']
    lines += ['', '### 每组全流程平均耗时', '', '| 组 | 加入前秒/次 | 加入后秒/次 | 前/后请求数 |', '|---|---:|---:|---:|']
    for group in data['manifest']['groups']:
        rows = [row for row in data['runs'] if row['group'] == group['id'] and row['valid']]
        b = summarize_timing([row for row in rows if row['strategy'] == 'checklist'])
        a = summarize_timing([row for row in rows if row['strategy'] == 'evidence'])
        if b and a:
            lines.append(f"| {group['title']} | {b['mean_seconds']:.2f} | {a['mean_seconds']:.2f} | {b['requests']} / {a['requests']} |")
    equal = data['equal_target_timing']
    lines += ['', '### 检出相同真实问题时的全流程耗时', '', equal['note']]
    if equal['before'] and equal['after']:
        b, a = equal['before'], equal['after']
        lines += [f"有效同目标配对 {equal['pairs']} 对；涉及组：" + '、'.join(dict.fromkeys(p['group'] for p in equal['targets'])) + '。',
            f"平均耗时 {b['mean_seconds']:.2f} → {a['mean_seconds']:.2f} 秒；变化 {(a['mean_seconds']/b['mean_seconds']-1)*100:+.1f}%；请求总数 {b['requests']} → {a['requests']}。",
            '这是按最终真阳性交集条件选出的探索性子集，不能代替全体预先固定用例结果，亦不证明同目标首次检出的延迟。']
    else:
        lines.append('没有非空且最终正确检出集合相同的有效配对，不计算同目标加速。')
    lines += ['', '### 本次修复与耗时优化', '',
        '- 错误撤回：拦截“仍有效、无需撤回”等与撤回操作矛盾的提交；程序验证支持的边界风险不能被模型撤回。',
        '- 安全说明误列问题：新增问题性质和访问 ID 绑定，仅对源码哈希匹配且终止为 SAFE/VIOLATION 的同一访问做属性级校正。SAFE 只处理该处边界意见，同一行其他缺陷保持审查。',
        '- 错误触发条件：程序已判 VIOLATION 的同一边界属性，采用程序符号见证生成规范结论；模型原条件保留在程序校正记录中，不能冒充模型独立正确。',
        '- 重复输入：模型只接收必要结论、前提、推导和缺项；完整事实账本、哈希与补查轨迹继续保存在报告，不在 audit_plan 中重复发送。',
        '- 重复轮次：程序已经完成的循环边界任务不再让模型重新提交；函数其他性质仍须模型审查。完整且无冲突时结束，仅补查缺项、未决记录或未验证模型问题。',
        '- 模型思考：仍存在未决循环边界任务时保留原思考模式；边界任务由程序完成后，其他任务使用既有非思考提交，减少重复边界推理。',
        '- 新增分段计时：记录扫描、证据补查、审查、每轮请求大小及服务返回的加载/输入处理/生成耗时；不用推测代替实测。', '',
        '### 质量门槛', '',
        f"本批最终漏检不劣化：{data['quality_gate']['no_additional_misses']}；最终错误问题项不劣化：{data['quality_gate']['no_additional_false_claims']}；没有新增错误主张：{data['quality_gate']['no_new_false_claims']}。",
        '此门槛仅适用于本回归集及评分范围，不能保证任意项目零误报。']
    validation = data.get('post_run_validation')
    if validation:
        lines += ['', '### 最终代码的保守回退回归', '', validation['note'],
            f"逐轮协议回放一致 {validation['runs']}/{len(data['runs'])}；新增真实模型请求 {validation['model_requests']}。",
            '该回放只检验流程回归，不作为新的准确性实验或独立样本。真实运行时源码摘要保存在 code_hashes，最终源码摘要及改动文件保存在 post_run_validation 中。']
    reference = data.get('historical_reference')
    if reference:
        lines += ['', '### 上一批未优化组合方案（历史参考）', '',
            '上一批结果保留用于说明退步来源。因运行时间与分析器代码不同，以下耗时仅作历史描述；本批同时重跑的 checklist/evidence 配对才是当前主要对照。', '',
            '| 指标 | 历史 evidence | 本批优化 evidence |', '|---|---:|---:|']
        old, new = reference['totals']['evidence'], data['totals']['evidence']
        for label, b, a in [('正确检出', old['tp'], new['tp']), ('漏检', old['fn'], new['fn']),
                ('错误问题项', old['fp'], new['fp']), ('模型请求总数', old['timing']['requests'], new['timing']['requests']),
                ('输入 Token', old['timing']['prompt_tokens'], new['timing']['prompt_tokens'])]:
            lines.append(f'| {label} | {b} | {a} |')
        lines.append(f"| 平均耗时秒（历史描述） | {old['timing']['mean_seconds']:.2f} | {new['timing']['mean_seconds']:.2f} |")
        lines.append('历史结果目录：`' + reference['path'] + '`；报告及裁决通过原摘要校验。')
    return lines


def write_report(data, output):
    before, after = data['totals']['checklist'],data['totals']['evidence']
    fp_trend = '增加' if after['fp'] > before['fp'] else '减少' if after['fp'] < before['fp'] else '持平'
    lines=['# 多组前后对照结果与代码示例','',
        f"固定 5 组、10 个代码案例，每组重复 {data['repeats']} 次，共 {len(data['runs'])} 次分析运行。真实模型 {data['model']['name']}，上下文 {data['model_context']}，温度 0。",
        '同一当前代码版本的 checklist / evidence 开关对照，保留共同缺陷修复；不是两个历史版本。每对同源码、模型和轮次预算，AB/BA 交替，不按结果筛选重复。',
        '标签事先保存在 manifest.json，没有发送给模型。'+data['contract'],
        data['scoring'],
        '严格正确性口径：触发条件明确错误的模型意见不计正确检出，即使报警位置碰巧是真实缺陷；它计入错误主张。安全说明误列入正式 findings 也计误报。程序的正确见证仍独立计分。修复建议不作为检测 TP 的充分条件，也未经自动修复验证。', '',
        f"本批有效配对中，系统正确检出由 **{before['tp']}** 变为 **{after['tp']}**，最终误报问题项由 **{before['fp']}** 至 **{after['fp']}**（{fp_trend}）。不能只凭个别误报被撤回就宣称总体误报改善。", '',
        '| 指标 | 加入前 | 加入后 |', '|---|---:|---:|',
        f"| 最终正确检出 / 真实缺陷机会 | {before['tp']} / {before['positive_opportunities']} | {after['tp']} / {after['positive_opportunities']} |",
        f"| 全流程平均耗时（秒/次） | {before['timing']['mean_seconds']:.2f} | {after['timing']['mean_seconds']:.2f} |",
        f"| 最终漏检 | {before['fn']} | {after['fn']} |",
        f"| 无漏检且无误报的完整运行 | {before['fully_correct_runs']} / {before['runs']} | {after['fully_correct_runs']} / {after['runs']} |",
        f"| 最终误报问题项 | {before['fp']} | {after['fp']} |",
        f"| 其中：错误触发条件 | {before.get('wrong_trigger_condition',0)} | {after.get('wrong_trigger_condition',0)} |",
        f"| 其中：安全说明误列问题 | {before.get('non_issue_listed',0)} | {after.get('non_issue_listed',0)} |",
        f"| 其中：安全访问被断言为越界 | {before.get('false_bounds_alarm',0)} | {after.get('false_bounds_alarm',0)} |",
        f"| 安全案例被误报 / 安全检查机会 | {before['safe_case_false_alerts']} / {before['safe_opportunities']} | {after['safe_case_false_alerts']} / {after['safe_opportunities']} |", '',
        f"逐对变化：新增正确检出 **{data['transitions']['new_detections']}** 次，反向漏报 **{data['transitions']['lost_detections']}** 次；原误报消失 **{data['transitions']['removed_false_claims']}** 项次，同时新增误报 **{data['transitions']['added_false_claims']}** 项次。不能只列减少的一侧。", '',
        '![前后效果图](comparison.png)', '', '![逐案例结果](case-matrix.png)', '',
        '## 最终系统结果的判定口径', '',
        '最终系统结果合并程序验证与模型最终保留的结论，同一真实缺陷只计一次；正确与否由预先登记的标签和逐条人工裁决判断。未经程序改写的模型检出仅用于解释来源；程序校正条目归入程序来源，不能用该计数升降判断模型能力。这是实验的综合计分；当前自动校正只覆盖已支持的列表边界属性，其他或未知意见仍需复核。',
        '“无漏检且无误报的完整运行”要求该次运行检出所有已标注真实缺陷，且没有任何计分范围内的错误问题项；分母是每个方案的有效完整运行次数。它反映本测试集上的整次运行正确性，不能解释成一般项目准确率。',
        '正确检出按缺陷计数，错误问题项按错误主张计数，两者不能直接相加当作总问题数计算精确率。重复两次增加的是检出机会，不是独立缺陷数量。', '',
        f"## 各组最终系统结果（{data['repeats']} 次重复累计）", '',
        '| 用例组 | 加入前：正确 / 漏检 / 错误项 | 加入后：正确 / 漏检 / 错误项 | 无漏检且无误报的运行：前 → 后 |',
        '|---|---:|---:|---:|']
    groups = {group['id']: group for group in data['manifest']['groups']}
    valid_pairs = {(pair['group'], pair['repeat']) for pair in data['pairs'] if pair['valid']}
    for group in data['manifest']['groups']:
        selected = [row for row in data['runs'] if row['group'] == group['id'] and (row['group'], row['repeat']) in valid_pairs]
        b = summarize_results([row for row in selected if row['strategy'] == 'checklist'], groups)
        a = summarize_results([row for row in selected if row['strategy'] == 'evidence'], groups)
        lines.append(f"| {group['title']} | {b['tp']} / {b['fn']} / {b['fp']} | {a['tp']} / {a['fn']} / {a['fp']} | {b['fully_correct_runs']}/{b['runs']} → {a['fully_correct_runs']}/{a['runs']} |")
    lines += timing_report(data)
    lines += ['', '## 每组变化与代码', '', 'TP 按真实缺陷去重；FP 按错误主张去重，包括错误触发条件。重复次数不等于独立案例数。']
    for group in data['manifest']['groups']:
        lines += ['',f"### {group['title']}（{group['id']}）",'',
            '```python',(ROOT/group['source']).read_text().rstrip(),'```','',
            *[f"- `{key}`：{'真实缺陷' if label['bug'] else '安全对照'}。{label['reason']}" for key,label in group['labels'].items()], '',
            '| 重复 | 加入前 TP / FP | 加入后 TP / FP | 新增检出 | 消失的错误主张 | 新增错误主张 |', '|---|---|---|---|---|---|']
        for pair in [p for p in data['pairs'] if p['group']==group['id']]:
            rows={r['strategy']:r for r in data['runs'] if r['group']==group['id'] and r['repeat']==pair['repeat']}
            b,a=rows['checklist']['quality'],rows['evidence']['quality']
            if not pair['valid']:
                lines.append(f"| {pair['repeat']} | 未完成/无效 | 未完成/无效 | 不计分 | 不计分 | 不计分 |")
                continue
            lines.append(f"| {pair['repeat']} | {b['tp']} / {b['fp']} | {a['tp']} / {a['fp']} | {pair['changes']['newly_detected']} | {pair['removed_false_claims']} | {pair['added_false_claims']} |")
        for row in [r for r in data['runs'] if r['group']==group['id']]:
            lines.append(f"- [{row['id']} 原始报告]({Path(row['report']).with_suffix('.html')})；完成={row['complete']}，上下文有效={row['input_context_valid']}。")
    reconciliations = [(row, item) for row in data['runs'] for item in row['program_reconciliations']]
    if reconciliations:
        lines += ['', '## 程序属性校正的实际记录', '',
            '下面记录模型原意见和程序处理。SAFE 仅处理对应访问的边界意见，VIOLATION 采用符号见证规范化条件；规范化数量不等于误报数量，正确与否仍逐条人工裁决。模型原文保存在各次报告的 audit.program_reconciliations 和 trace。', '',
            '| 运行 | 原问题位置及标题 | 模型原触发条件 | 程序处理 |', '|---|---|---|---|']
        for row, item in reconciliations:
            original = item['original']
            treatment = '该访问边界在前提下安全，移出问题清单' if item['verdict'] == 'SAFE' else '该访问有越界见证，条件由程序规范化'
            condition = original.get('condition', '').replace('|', ' / ').replace('\n', ' ')
            lines.append(f"| {row['id']} | {original['file']}:{original['line']} · {original['title']} | {condition} | {treatment} |")
    lines += ['', '## 误报是否由新方案纠正', '',
        '上表的“错误主张消失”仅是最终输出差异。下面单列真实的程序冲突反馈与撤回记录；没有记录就不称为程序主动纠错。即使有纠错记录，若旧流程也撤回相同误报，就不能称为新增方案额外消除误报。', '']
    found=False
    for row in data['runs']:
        for event in row['program_corrections']:
            found=True
            lines.append(f"- {row['id']}：第 {event['round']} 轮对 {event['file']}:{event['line']} / {event['finding_id']} 反馈程序安全证据，最终撤回「{event['title']}」。撤回理由：{event['withdraw_reason']}")
    if not found:
        lines.append('本批有直接属性校正记录，见上表；没有采用“反证反馈后让模型撤回”的旧机制，不能把两者混为一谈。' if reconciliations else '本批次没有“程序安全反证→模型撤回”的完整记录；不能据输出差异声称该机制消除了误报。')
    replay_path = output/'mechanism-replay.json'
    if replay_path.exists():
        replay = json.loads(replay_path.read_text())
        lines += ['', '### 同一真实旧流程提交的定点对照', '', replay['scope'],
            f"参考运行：`{replay['reference_run']}`。F1 为真实缺陷，F2 为安全说明误列问题清单；回放原提交，没有人为新增误报。",
            '两组接收相同源码和相同首轮错误提交，仅实验组加入程序证据、知识库条件结论和冲突反馈。', '',
            '| 重复 | 组 | F2 是否撤回 | F1 是否保留 | 是否有 F2 程序冲突反馈 | 状态 |', '|---|---|---|---|---|---|']
        for row in replay['runs']:
            o = row['outcome']
            lines.append(f"| {row['repeat']} | {row['strategy']} | {o.get('false_positive_withdrawn')} | {o.get('true_positive_retained')} | {o.get('conflict_feedback_observed')} | {o['status']} |")
        meanings = {'incremental_correction_observed': '在同一起点下，旧流程保留误报，新流程收到程序反证后撤回，真实问题仍保留；支持本条输出误报的增量纠错归因。',
            'both_corrected_no_incremental_benefit': '两组都能撤回，不能把此项纠错归功于新增方案。',
            'both_corrected_baseline_lost_true_positive': '两组都撤回误报；区别是旧流程误撤真实问题，新流程保留。不能称为额外消除误报。'}
        for pair in replay['pairs']:
            lines.append(f"- 第 {pair['repeat']} 对共同起点检查：{pair['same_start_except_intervention']}；" + meanings.get(pair['outcome'], pair['outcome']))
        for row in replay['runs']:
            for reason in row['outcome'].get('withdrawal_reasons', []):
                lines.append(f"- {row['strategy']} 实际撤回理由：{reason}")
        lines += ['','这是事后选择的一个机制样本，不并入前面 20 次完整分析的计数，也不代表自然误报率或各组件独立贡献。完整请求与响应在 `mechanism-replay.json`。']
    if data['interpretation_notes']:
        lines += ['', '## 对结果的解释', '', *['- '+note for note in data['interpretation_notes']]]
    lines += ['', '## 限制与复现', '',
        '这是小型、人工设计的 Python 列表边界回归集，覆盖首期验证器支持的结构；没有 SBFL，未执行目标源码。程序 SAFE/VIOLATION 是带前提的规则结论。不能推断一般项目、多语言或三个单独组件的收益。',
        '同模型温度 0 的重复结果可能完全相同，不能把重复当作独立统计样本。准确性与耗时分开；同质量效率仅比较共同最终真阳性集合，不把漏检更多当作提速。纯风格和非目标意见保留在裁决中，不当作正确越界检出。',
        '原始模型输出、扫描状态和调用轨迹在各运行目录；逐条裁决在 adjudication.json，汇总数字在 summary.json，源码标签在 test/cases/evidence_bounds/matrix/manifest.json。', '',
        '```bash', '.venv/bin/python test/run_comparison_matrix.py --repeats 2 --max-rounds 4 --output test/results-comparison-matrix-new',
        '# 完成逐条语义裁决后，生成图表和文档：',
        '.venv/bin/python test/plot_comparison_matrix.py --output test/results-comparison-matrix-new', '```','']
    (output/'summary.md').write_text('\n'.join(lines),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'results-comparison-matrix')
    parser.add_argument('--require-no-regression', action='store_true', help='生成报告后，如出现新增误报或漏检/误报总数劣化则返回失败')
    parser.add_argument('--reference', type=Path, help='已有完整旧实验目录；历史对照不混入本批计分')
    parser.add_argument('--font',help='中文字体文件路径；支持 macOS/Windows/Linux 常见字体自动定位')
    args=parser.parse_args()
    output=args.output.resolve()
    data=load_scored(output)
    if args.reference:
        reference = load_scored(args.reference.resolve())
        data['historical_reference'] = {'path': str(args.reference.resolve()), 'totals': reference['totals'],
            'code_hashes': reference['code_hashes'], 'adjudication_sha256': reference['adjudication_sha256']}
    (output/'summary.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    plot(data,output,args.font)
    write_report(data,output)
    print(output/'summary.md')
    if args.require_no_regression and not all(data['quality_gate'].values()):
        raise SystemExit('质量门槛未通过：结果已保留，不能宣称优化验收成功')


if __name__=='__main__':
    main()
