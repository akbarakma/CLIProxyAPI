import { describe, expect, test } from 'bun:test';
import { parseKeepaliveSnapshot } from '../src/services/api/keepalive';

const account = {
  id: 'claude-main',
  label: 'Claude Main',
  interval_seconds: 3600,
  available: true,
  enabled: false,
  timer_active: false,
  running: true,
  started_at: '2026-11-03T18:10:00Z',
  next_run_at: null,
  checked_at: '2026-11-03T18:00:00Z',
  last_result: 'error',
};
const snapshot = { updated_at: '2026-11-03T18:10:00Z', accounts: [account] };

describe('keepalive status', () => {
  test('a paused schedule can have a ping still running', () => {
    const row = parseKeepaliveSnapshot(snapshot).accounts[0];
    expect(row.enabled).toBe(false);
    expect(row.running).toBe(true);
    expect(row.nextRunAt).toBeNull();
    expect(row.lastResult).toBe('error');
  });

  test('unknown outcomes stay unknown', () => {
    const value = { ...snapshot, accounts: [{ ...account, last_result: 'new-status' }] };
    expect(parseKeepaliveSnapshot(value).accounts[0].lastResult).toBe('unknown');
  });

  test('invalid timestamps, booleans, intervals and duplicate accounts are rejected', () => {
    for (const change of [
      { enabled: 'false' },
      { next_run_at: 'tomorrow' },
      { interval_seconds: '3600' },
    ]) {
      expect(() =>
        parseKeepaliveSnapshot({ ...snapshot, accounts: [{ ...account, ...change }] })
      ).toThrow();
    }
    expect(() => parseKeepaliveSnapshot({ ...snapshot, accounts: [account, account] })).toThrow();
  });
});
