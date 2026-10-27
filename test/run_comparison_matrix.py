"""Real paired multi-case runs; labels stay outside the analyzer input."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'cases/evidence_bounds/matrix/manifest.json'


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_for(group, strategy, destination, args):
    command = ['analyze', str(ROOT / group['source']), '--analysis-strategy', strategy,
        '--review-scope', 'files', '--model', args.model, '--ollama-url', args.ollama_url,
        '--max-rounds', str(args.max_rounds), '--max-calls', '12', '--context-chars', '30000',
        '--model-context', str(args.model_context), '--model-timeout', str(args.model_timeout),
        '--output', str(destination)]
    if strategy == 'evidence':
        command += ['--evidence-policy', 'cost', '--max-evidence-actions', '1000', '--evidence-budget', '2000']
    return command


def context_valid(row, identity, context):
    return (any(m.get('digest') == identity['digest'] and m.get('context_length') == context
                for m in row['runtime_model'].get('models', []))
            and bool(row['measurement']['responses'])
            and all(0 < r['prompt_tokens'] < context for r in row['measurement']['responses']))


def run(args):
    from evidence_review import cli
    from evidence_review.agent import Ollama
    from run_before_after import api, model_identity, artifacts
    from run_suite import LiveLog
    from run_evidence_reliability import run as reliability_preflight
    preflight = reliability_preflight(['cost'])
    metrics = preflight['runs'][0]['metrics']
    if any(metrics[k] for k in ('program_fn', 'safe_as_violation', 'unsafe_as_safe', 'boundary_decisions')) or metrics['expectation_matches'] != preflight['cases']['accesses']:
        raise ValueError('可靠性前置检查未通过，先修复漏检/误报再启动正式对照')
    output = args.output.resolve()
    if (output / 'runs.json').exists():
        raise ValueError('输出目录已有运行，拒绝覆盖真实记录')
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text())
    identity = model_identity(args.ollama_url, args.model)
    code = {str(p.relative_to(ROOT.parent)): digest(p)
            for p in sorted((ROOT.parent/'src/evidence_review').rglob('*.py'))}
    data = {'kind': 'multi_group_real_end_to_end', 'manifest': manifest, 'manifest_sha256': digest(MANIFEST),
        'model': identity, 'model_context': args.model_context, 'max_rounds': args.max_rounds,
        'repeats': args.repeats, 'code_hashes': code, 'runner_sha256': digest(Path(__file__)),
        'contract': manifest['contract'], 'temperature': 0, 'preflight': preflight, 'runs': []}
    assessments = {}
    save(output/'runs.json', data)
    for group in manifest['groups']:
        for repeat in range(1, args.repeats + 1):
            order = ('checklist', 'evidence') if repeat % 2 else ('evidence', 'checklist')
            for strategy in order:
                if digest(ROOT/group['source']) != group['sha256'] or digest(MANIFEST) != data['manifest_sha256']:
                    raise ValueError('用例或标签在实验中发生变化')
                if any(digest(ROOT.parent/p) != sha for p, sha in code.items()):
                    raise ValueError('分析器代码在实验中发生变化')
                if model_identity(args.ollama_url, args.model) != identity:
                    raise ValueError('模型在实验中发生变化')
                run_id = f"{group['id']}/{repeat:02d}-{strategy}"
                destination = output/run_id
                destination.mkdir(parents=True)
                warmup = api(args.ollama_url, 'chat', {'model': args.model, 'stream': False, 'think': False,
                    'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                    'options': {'temperature': 0, 'num_predict': 1, 'num_ctx': args.model_context}}, args.model_timeout)
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
                            seconds = time.monotonic() - tick
                            measurement['model_seconds'] += seconds
                            measurement['responses'].append({'round': measurement['attempts'], 'seconds': seconds,
                                'wall_seconds_at_response': time.monotonic()-started,
                                'prompt_tokens': self.usage['prompt_tokens']-prior['prompt_tokens'],
                                'completion_tokens': self.usage['completion_tokens']-prior['completion_tokens'],
                                'runtime': dict(self.request_metrics[-1]) if self.usage['requests'] > prior['requests'] else {}})
                command = command_for(group, strategy, destination, args)
                original = cli.Ollama
                with (destination/'run.log').open('w', encoding='utf-8') as log:
                    with redirect_stdout(LiveLog(sys.stdout, log)), redirect_stderr(LiveLog(sys.stderr, log)):
                        print(f"\n[{len(data['runs'])+1}/{len(manifest['groups'])*args.repeats*2}] {run_id}", flush=True)
                        cli.Ollama = MeteredOllama
                        started = time.monotonic()
                        try:
                            exit_code = cli.main(command)
                        finally:
                            measurement['wall_seconds'] = time.monotonic()-started
                            cli.Ollama = original
                report_path = destination/'report.json'
                report = json.loads(report_path.read_text())
                alerts = artifacts(report)
                row = {'id': run_id, 'group': group['id'], 'repeat': repeat, 'strategy': strategy,
                    'command': command, 'exit_code': exit_code,
                    'complete': exit_code == 0 and bool(report['reviews'])
                        and all(r['status'] == 'completed' for r in report['reviews']),
                    'report': str(report_path.relative_to(output)), 'report_sha256': digest(report_path),
                    'source_hashes': report['source_hashes'], 'alerts': alerts,
                    'measurement': measurement, 'usage': report['model_usage'], 'stage_timings': report.get('stage_timings', {}),
                    'runtime_model': api(args.ollama_url, 'ps'),
                    'warmup': {k: warmup.get(k) for k in ('load_duration', 'total_duration', 'eval_count')}}
                row['input_context_valid'] = context_valid(row, identity, args.model_context)
                data['runs'].append(row)
                # Re-read annotations so manual review during a long run is preserved.
                if (output/'adjudication.json').exists():
                    assessments = json.loads((output/'adjudication.json').read_text())
                assessments[run_id] = {'report_sha256': row['report_sha256'],
                    'final': {a['id']: {'status': 'pending', 'reason': ''} for a in alerts}, 'rounds': {}}
                save(output/'adjudication.json', assessments)
                save(output/'runs.json', data)
                print(f"完成 {run_id}：completed={row['complete']} context_valid={row['input_context_valid']}", flush=True)
    return 0 if all(r['complete'] and r['input_context_valid'] for r in data['runs']) else 2



def replay(args):
    """Replay a baseline's actual first submission to isolate later correction."""
    from evidence_review.agent import Ollama, review
    from evidence_review.evidence import analyze_evidence, candidate_analysis
    from evidence_review.project import Project
    from run_before_after import (api, model_identity, FrozenFirstClient, correction_outcome,
                                  correction_pair, correction_request_without_intervention)
    output = args.output.resolve()
    destination = output/'mechanism-replay.json'
    if destination.exists():
        raise ValueError('已有同起点纠错记录，拒绝覆盖')
    data = json.loads((output/'runs.json').read_text())
    annotations = json.loads((output/'adjudication.json').read_text())
    reference = args.replay_reference.resolve()
    row = next(r for r in data['runs'] if (output/r['report']).resolve() == reference)
    if row['strategy'] != 'checklist' or not row['complete'] or not row['input_context_valid']:
        raise ValueError('只能回放已完成的有效旧流程误报')
    if digest(reference) != row['report_sha256'] or annotations[row['id']]['report_sha256'] != row['report_sha256']:
        raise ValueError('参考报告发生变化')
    group = next(g for g in data['manifest']['groups'] if g['id'] == row['group'])
    if not any(d['status']=='fp' and 'case_b' in d.get('labels', []) for d in annotations[row['id']]['final'].values()):
        raise ValueError('基线没有已裁决的 case_b 最终误报，不能作为消除误报的归因样本')
    report = json.loads(reference.read_text())
    project = Project(ROOT/group['source'])
    if project.hashes() != report['source_hashes']:
        raise ValueError('回放源码与原报告不同')
    frozen = report['reviews'][0]['trace'][0]['arguments']
    if [f['line'] for f in frozen['findings']] != [group['labels'][k]['line'] for k in ('case_a','case_b')]:
        raise ValueError('首轮不满足 F1 真实问题、F2 误报的已核对结构，需另行裁决')
    analysis = analyze_evidence(project,policy='cost',max_actions=1000,max_cost=2000)
    candidate = report['candidates'][0]
    analysis = candidate_analysis(analysis,candidate,project)
    identity = model_identity(args.ollama_url,args.model)
    if identity != data['model']:
        raise ValueError('回放模型与完整分析不同')
    result = {'kind':'frozen_baseline_false_positive_replay','reference_run':row['id'],
        'reference_sha256':row['report_sha256'],'source_hashes':project.hashes(),
        'model':identity,'model_context':args.model_context,'frozen_submission':frozen,
        'max_live_rounds':args.max_rounds,'repeats':args.repeats,
        'program_analysis':analysis,'runs':[],'pairs':[],
        'scope':'事后选出的真实旧流程最终误报；冻结其首轮完整提交。两组仅后续真实复核，回放不是本次生成，不能用于统计自然误报率。'}
    for repeat in range(1,args.repeats+1):
        pair={}
        for strategy in (('checklist','evidence') if repeat%2 else ('evidence','checklist')):
            print(f'同起点纠错 {repeat}/{args.repeats} {strategy}',flush=True)
            class MeasuredClient(Ollama):
                def chat(self,*values,**kwargs):
                    prior = self.usage['prompt_tokens']
                    message = super().chat(*values,**kwargs)
                    self.prompt_counts.append(self.usage['prompt_tokens']-prior)
                    return message
            delegate=MeasuredClient(args.model,args.ollama_url,args.model_timeout,args.model_context)
            delegate.prompt_counts=[]
            client=FrozenFirstClient(delegate,frozen)
            reviewed=review(project,candidate,client,budget=30000,max_rounds=args.max_rounds+1,max_calls=12,
                program_analysis=analysis if strategy=='evidence' else None,
                progress=lambda value: print(value,flush=True))
            record={'repeat':repeat,'strategy':strategy,'result':reviewed,'live_requests':client.requests,
                'usage':delegate.usage,'runtime_model':api(args.ollama_url,'ps'),
                'measurement':{'responses':[{'prompt_tokens':n} for n in delegate.prompt_counts]}}
            record['input_context_valid']=context_valid(record,identity,args.model_context)
            record['outcome']=(correction_outcome(reviewed) if record['input_context_valid'] else {'status':'invalid_context'})
            result['runs'].append(record);pair[strategy]=record
            save(destination,result)
        b,a=pair['checklist'],pair['evidence']
        same=bool(b['live_requests'] and a['live_requests']) and (
            correction_request_without_intervention(b['live_requests'][0]) ==
            correction_request_without_intervention(a['live_requests'][0]))
        result['pairs'].append({'repeat':repeat,'same_start_except_intervention':same,
            'outcome':correction_pair(b['outcome'],a['outcome'],same)})
        save(destination,result)
    return 0 if all(r['outcome']['status']=='scored' for r in result['runs']) else 2


def validate_recorded(output):
    """Protocol regression replay only; never substitutes for real model runs."""
    from copy import deepcopy
    from evidence_review.agent import review
    from evidence_review.project import Project
    output = output.resolve()
    data = json.loads((output/'runs.json').read_text())
    expected = len(data['manifest']['groups']) * data['repeats'] * 2
    if len(data['runs']) != expected or not all(row['complete'] and row['input_context_valid'] for row in data['runs']):
        raise ValueError('只回放已全部完成的有效实验，不将未完成矩阵视为验证成功')
    checks = []
    for row in data['runs']:
        path = output/row['report']
        if digest(path) != row['report_sha256']:
            raise ValueError('原始报告摘要变化')
        report = json.loads(path.read_text())
        group = next(group for group in data['manifest']['groups'] if group['id'] == row['group'])
        project = Project(ROOT/group['source'])
        if project.hashes() != row['source_hashes']:
            raise ValueError('回放源码与真实运行不同')
        for saved in report['reviews']:
            events = saved['trace']
            if [event['round'] for event in events] != list(range(1, row['measurement']['attempts']+1)):
                raise ValueError('此校验器仅支持每轮一次工具提交的已记录实验')
            class RecordedClient:
                calls = 0
                def chat(self, messages, tools):
                    if self.calls >= len(events):
                        raise AssertionError('当前流程要求额外模型请求，与原运行不等价')
                    event = events[self.calls]
                    self.calls += 1
                    message = {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {
                        'name': event['tool'], 'arguments': deepcopy(event['arguments'])}}]}
                    if event.get('transport'):
                        message['_transport'] = deepcopy(event['transport'])
                    return message
            client = RecordedClient()
            candidate = next(item for item in report['candidates'] if item['id'] == saved['candidate_id'])
            command = row['command']
            current = review(project, candidate, client, mode=report['context_mode'],
                budget=int(command[command.index('--context-chars')+1]), max_rounds=data['max_rounds'],
                max_calls=int(command[command.index('--max-calls')+1]), program_analysis=saved.get('program_analysis'))
            for key in ('status', 'review', 'audit', 'trace', 'evidence', 'context_chars', 'source_coverage'):
                if current.get(key) != saved.get(key):
                    raise AssertionError(f"{row['id']} 当前回放字段 {key} 与原实测不等价")
            if client.calls != len(events):
                raise AssertionError('当前流程提前结束，与原实测不等价')
        checks.append({'id': row['id'], 'report_sha256': row['report_sha256'], 'identical': True})
    final_hashes = {str(path.relative_to(ROOT.parent)): digest(path)
                    for path in sorted((ROOT.parent/'src/evidence_review').rglob('*.py'))}
    data['post_run_validation'] = {'kind': 'recorded_protocol_regression_not_new_inference',
        'runs': len(checks), 'model_requests': 0, 'checks': checks, 'final_code_hashes': final_hashes,
        'changed_code_files': [key for key, value in data['code_hashes'].items() if final_hashes.get(key) != value],
        'note': '完整真实实验期间代码冻结。随后补充无证据/未知证据的保守回退；'
                '对本批真实提交逐轮回放，结果、反馈、证据与轮次完全一致。回放不计为新模型实验或独立样本。'}
    save(output/'runs.json', data)
    print(f'逐轮协议回归回放一致：{len(checks)}/{expected}；新增模型请求 0')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='qwen3:8b')
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11435')
    parser.add_argument('--model-context', type=int, default=40960)
    parser.add_argument('--model-timeout', type=int, default=300)
    parser.add_argument('--max-rounds', type=int, default=4)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--output', type=Path, default=ROOT/'results-comparison-matrix')
    parser.add_argument('--validate-recorded', action='store_true', help='用已有真实提交校验当前流程协议等价性，不调用模型')
    parser.add_argument('--replay-reference',type=Path,help='选用矩阵中旧流程真实最终误报，冻结首轮进行同起点纠错对照')
    args = parser.parse_args()
    if min(args.model_context, args.model_timeout, args.max_rounds, args.repeats) < 1:
        parser.error('预算必须为正数')
    return validate_recorded(args.output) if args.validate_recorded else replay(args) if args.replay_reference else run(args)


if __name__ == '__main__':
    raise SystemExit(main())
