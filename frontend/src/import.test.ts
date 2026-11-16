import { describe, expect, it } from 'vitest';
import research from '../../tests/fixtures/example/research.sample.json';
import manifest from '../../tests/fixtures/example/manifest.sample.json';
import evidence from '../../tests/fixtures/example/evidence.sample.json';
import { buildCommand, parseSnapshot } from './import';
import { demoData, makeReport, safeUrl, topicEvidence } from './data';

const snapshot = [
  { name: 'analysis/research.json', text: JSON.stringify(research) },
  { name: 'manifest.json', text: JSON.stringify(manifest) },
  { name: 'normalized/evidence.jsonl', text: `${JSON.stringify(evidence)}\n` },
];

describe('research snapshot import', () => {
  it('reads the repository’s actual schema 1.3 fixture and preserves unverified delivery', () => {
    const result = parseSnapshot(snapshot);
    expect(result.repo).toBe('example/demo');
    expect(result.topics[0].title).toBe('缓存生命周期管理');
    expect(result.evidence[0].text).toBe(evidence.text);
    expect(result.capabilities[0].delivery_state).toBe('not_found');
    expect(result.warnings.some((w) => w.includes('2 条被引用证据未载入'))).toBe(true);
    expect(topicEvidence(result, result.topics[0])).toHaveLength(1);
  });
  it('allows a research-only file without inventing evidence or dates', () => {
    const result = parseSnapshot(snapshot.slice(0, 1));
    expect(result.evidence).toEqual([]);
    expect(result.start).toBe('');
    expect(result.repo).toBe('本地研究快照');
    expect(result.warnings.some((w) => w.includes('未导入证据账本'))).toBe(true);
  });
  it('rejects malformed, mismatched and mixed snapshots', () => {
    expect(() => parseSnapshot([])).toThrow('缺少 research.json');
    expect(() => parseSnapshot([{ name: 'research.json', text: '{broken' }])).toThrow(
      '有效的 JSON',
    );
    expect(() => parseSnapshot([{ name: 'research.json', text: '{}' }])).toThrow('格式不兼容');
    expect(() => parseSnapshot([...snapshot, snapshot[0]])).toThrow('多个研究快照');
    expect(() =>
      parseSnapshot([
        snapshot[0],
        { name: 'manifest.json', text: JSON.stringify({ ...manifest, run_id: 'other' }) },
      ]),
    ).toThrow('run_id 不一致');
  });
  it('rejects malformed or duplicate evidence instead of silently dropping it', () => {
    expect(() => parseSnapshot([snapshot[0], { name: 'evidence.jsonl', text: '{}\n' }])).toThrow(
      '证据文件格式不正确',
    );
    expect(() =>
      parseSnapshot([
        snapshot[0],
        {
          name: 'evidence.jsonl',
          text: `${JSON.stringify(evidence)}\n${JSON.stringify(evidence)}`,
        },
      ]),
    ).toThrow('重复 ID');
  });
  it('keeps imported Markdown unchanged and labels generated reading drafts', () => {
    const result = parseSnapshot([...snapshot, { name: 'report.md', text: '# Original report\n' }]);
    expect(makeReport(result)).toBe('# Original report\n');
    expect(makeReport(parseSnapshot(snapshot))).toContain('未执行 CLI validate/render');
    expect(makeReport(demoData)).toContain('虚构数据');
  });
});

describe('command generation and external links', () => {
  it('generates existing CLI flags and a deliberate Agent handoff', () => {
    const command = buildCommand(
      'https://github.com/vllm-project/vllm/',
      'deep',
      180,
      'KV cache, 调度',
    );
    expect(command).toContain("--repo 'vllm-project/vllm' --mode deep --days 180");
    expect(command).toContain("--focus 'KV cache'");
    expect(command).toContain('编写 analysis/research.json');
    expect(command).toContain('validate --run "$RUN"');
  });
  it('rejects invalid repository and window, and quotes shell metacharacters', () => {
    expect(() => buildCommand('x/y; ls', 'quick', 30, '')).toThrow('请输入');
    expect(() => buildCommand('x/y', 'standard', 0, '')).toThrow('研究窗口');
    expect(() => buildCommand('x/y', 'invalid', 30, '')).toThrow('研究窗口');
    expect(buildCommand('x/y', 'quick', 30, "it's $(example) `example`")).toContain(
      "--focus 'it'\\''s $(example) `example`'",
    );
  });
  it('only renders web links', () => {
    expect(safeUrl('javascript:alert(1)')).toBeUndefined();
    expect(safeUrl('file:///etc/passwd')).toBeUndefined();
    expect(safeUrl('https://github.com/example/demo')).toBe('https://github.com/example/demo');
  });
});
