import { apiClient } from './client';

export type KeepaliveResult =
  'skipped' | 'ok' | 'error' | 'unverified' | 'pending_alignment' | 'waiting_next_slot' | 'unknown';

export interface KeepaliveAccount {
  id: string;
  label: string;
  intervalSeconds: 1800 | 3600;
  available: boolean;
  enabled: boolean;
  timerActive: boolean;
  running: boolean;
  startedAt: string | null;
  nextRunAt: string | null;
  checkedAt: string | null;
  lastResult: KeepaliveResult;
}

export interface KeepaliveSnapshot {
  updatedAt: string;
  accounts: KeepaliveAccount[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function text(value: unknown): string {
  if (typeof value !== 'string' || !value) throw new Error('Invalid keepalive response');
  return value;
}

function boolean(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new Error('Invalid keepalive response');
  return value;
}

function date(value: unknown): string {
  const result = text(value);
  if (!Number.isFinite(Date.parse(result))) throw new Error('Invalid keepalive timestamp');
  return result;
}

function nullableDate(value: unknown): string | null {
  return value === null ? null : date(value);
}

function result(value: unknown): KeepaliveResult {
  switch (value) {
    case 'skipped':
    case 'ok':
    case 'error':
    case 'unverified':
    case 'pending_alignment':
    case 'waiting_next_slot':
      return value;
    default:
      return 'unknown';
  }
}

export function parseKeepaliveSnapshot(value: unknown): KeepaliveSnapshot {
  if (!isRecord(value) || !Array.isArray(value.accounts)) {
    throw new Error('Invalid keepalive response');
  }
  const accounts = value.accounts.map((row: unknown): KeepaliveAccount => {
    if (!isRecord(row)) {
      throw new Error('Invalid keepalive account');
    }
    const interval = row.interval_seconds;
    if (interval !== 1800 && interval !== 3600) throw new Error('Invalid keepalive interval');
    return {
      id: text(row.id),
      label: text(row.label),
      intervalSeconds: interval,
      available: boolean(row.available),
      enabled: boolean(row.enabled),
      timerActive: boolean(row.timer_active),
      running: boolean(row.running),
      startedAt: nullableDate(row.started_at),
      nextRunAt: nullableDate(row.next_run_at),
      checkedAt: nullableDate(row.checked_at),
      lastResult: result(row.last_result),
    };
  });
  if (new Set(accounts.map((account) => account.id)).size !== accounts.length) {
    throw new Error('Duplicate keepalive account');
  }
  return { updatedAt: date(value.updated_at), accounts };
}

export const keepaliveApi = {
  async read(signal?: AbortSignal): Promise<KeepaliveSnapshot> {
    const client = apiClient.captureConnection();
    return parseKeepaliveSnapshot(
      await client.get<unknown>('/keepalive', { signal, timeout: 12_000 })
    );
  },
  async setEnabled(id: string, enabled: boolean): Promise<KeepaliveSnapshot> {
    const client = apiClient.captureConnection();
    return parseKeepaliveSnapshot(
      await client.put<unknown>(
        `/keepalive/${encodeURIComponent(id)}`,
        { enabled },
        { timeout: 25_000 }
      )
    );
  },
};
