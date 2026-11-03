import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ToggleSwitch } from '@/components/ui/ToggleSwitch';
import { apiClient } from '@/services/api/client';
import { keepaliveApi, type KeepaliveSnapshot } from '@/services/api/keepalive';
import { useAuthStore } from '@/stores';
import styles from './KeepalivePanel.module.scss';

type Reading =
  | { kind: 'ready'; snapshot: KeepaliveSnapshot; receivedAt: number; revision: number }
  | { kind: 'unavailable'; updatedAt: string | null; revision: number };

export function KeepalivePanel() {
  const { t, i18n } = useTranslation();
  const apiBase = useAuthStore((state) => state.apiBase);
  const key = useAuthStore((state) => state.managementKey);
  const connectionStatus = useAuthStore((state) => state.connectionStatus);
  const [reading, setReading] = useState<Reading | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [changeFailed, setChangeFailed] = useState(false);
  const [clock, setClock] = useState(Date.now);
  const request = useRef(0);
  const changing = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const revision = apiClient.getConnectionRevision();
  const current = reading?.revision === revision ? reading : null;

  const refresh = useCallback(async () => {
    if (changing.current) return;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    const sequence = ++request.current;
    const owner = apiClient.getConnectionRevision();
    try {
      const snapshot = await keepaliveApi.read(abort.signal);
      if (sequence !== request.current || owner !== apiClient.getConnectionRevision()) return;
      setReading({ kind: 'ready', snapshot, receivedAt: Date.now(), revision: owner });
      setChangeFailed(false);
    } catch {
      if (
        abort.signal.aborted ||
        sequence !== request.current ||
        owner !== apiClient.getConnectionRevision()
      )
        return;
      setReading((previous) => ({
        kind: 'unavailable',
        updatedAt:
          previous?.revision === owner
            ? previous.kind === 'ready'
              ? previous.snapshot.updatedAt
              : previous.updatedAt
            : null,
        revision: owner,
      }));
    }
  }, []);

  useEffect(() => {
    if (connectionStatus !== 'connected') return;
    void refresh();
    const poll = window.setInterval(() => void refresh(), 15_000);
    const tick = window.setInterval(() => setClock(Date.now()), 1000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(tick);
      controller.current?.abort();
      request.current += 1;
    };
  }, [apiBase, key, connectionStatus, refresh]);

  const handleChange = useCallback(async (id: string, enabled: boolean) => {
    if (changing.current) return;
    const owner = apiClient.getConnectionRevision();
    changing.current = true;
    controller.current?.abort();
    request.current += 1;
    setPending(id);
    setChangeFailed(false);
    try {
      const snapshot = await keepaliveApi.setEnabled(id, enabled);
      if (owner !== apiClient.getConnectionRevision()) return;
      setReading({ kind: 'ready', snapshot, receivedAt: Date.now(), revision: owner });
    } catch {
      if (owner !== apiClient.getConnectionRevision()) return;
      setChangeFailed(true);
      setReading((previous) => ({
        kind: 'unavailable',
        updatedAt: previous?.kind === 'ready' ? previous.snapshot.updatedAt : null,
        revision: owner,
      }));
    } finally {
      changing.current = false;
      setPending(null);
    }
  }, []);

  const time = (value: string) =>
    new Date(value).toLocaleTimeString(i18n.language, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      timeZoneName: 'short',
    });
  const attemptTime = (value: string) =>
    new Date(value).toLocaleString(i18n.language, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      timeZoneName: 'short',
    });
  const next = (value: string) => {
    if (current?.kind !== 'ready') return t('keepalive.unavailable');
    const now = Date.parse(current.snapshot.updatedAt) + Math.max(0, clock - current.receivedAt);
    const seconds = Math.ceil((Date.parse(value) - now) / 1000);
    if (seconds <= 0) return t('keepalive.due');
    return seconds >= 60
      ? t('keepalive.in_minutes', { count: Math.ceil(seconds / 60) })
      : t('keepalive.in_seconds', { count: seconds });
  };
  const updatedAt = current?.kind === 'ready' ? current.snapshot.updatedAt : current?.updatedAt;

  return (
    <section className={styles.section} aria-labelledby="keepalive-title">
      <header className={styles.header}>
        <div>
          <h2 id="keepalive-title">{t('keepalive.title')}</h2>
          <p>{t('keepalive.description')}</p>
        </div>
        <div className={styles.refresh}>
          {updatedAt && <span>{t('keepalive.updated', { time: time(updatedAt) })}</span>}
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={pending !== null || connectionStatus !== 'connected'}
            aria-label={t('keepalive.refresh')}
          >
            {t('common.refresh')}
          </button>
        </div>
      </header>
      {!current && connectionStatus === 'connected' && (
        <p role="status">{t('keepalive.loading')}</p>
      )}
      {(current?.kind === 'unavailable' || connectionStatus !== 'connected') && (
        <p role="alert" className={styles.error}>
          {t(changeFailed ? 'keepalive.change_failed' : 'keepalive.unavailable')}
        </p>
      )}
      {current?.kind === 'ready' && connectionStatus === 'connected' && (
        <div className={styles.table} role="table" aria-label={t('keepalive.title')}>
          <div className={styles.columns} role="row">
            {['account', 'schedule', 'last_attempt', 'next_run', 'enabled'].map((label) => (
              <span key={label} role="columnheader">
                {t(`keepalive.${label}`)}
              </span>
            ))}
          </div>
          {current.snapshot.accounts.map((account) => (
            <div key={account.id} className={styles.row} role="row" data-keepalive={account.id}>
              <div role="cell" className={styles.account}>
                {account.label}
              </div>
              <div role="cell">
                <span>
                  {t(
                    account.intervalSeconds === 3600 ? 'keepalive.hourly' : 'keepalive.halfhourly'
                  )}
                </span>
                <small className={!account.available ? styles.error : undefined}>
                  {t(
                    !account.available
                      ? 'keepalive.unavailable'
                      : account.enabled && account.timerActive
                        ? 'keepalive.enabled'
                        : !account.enabled && !account.timerActive
                          ? 'keepalive.paused'
                          : 'keepalive.attention'
                  )}
                </small>
              </div>
              <div role="cell">
                <span
                  className={
                    account.lastResult === 'error'
                      ? styles.error
                      : account.lastResult === 'ok'
                        ? styles.success
                        : undefined
                  }
                >
                  {account.running
                    ? t('keepalive.running')
                    : t(`keepalive.result.${account.lastResult}`)}
                </span>
                <small>
                  {account.running && account.startedAt
                    ? attemptTime(account.startedAt)
                    : account.checkedAt
                      ? attemptTime(account.checkedAt)
                      : t('keepalive.never')}
                </small>
              </div>
              <div role="cell">
                <span>
                  {!account.available
                    ? t('keepalive.unavailable')
                    : !account.timerActive
                      ? t('keepalive.paused')
                      : account.nextRunAt
                        ? next(account.nextRunAt)
                        : t('keepalive.unavailable')}
                </span>
                {account.timerActive && account.nextRunAt && (
                  <small>{time(account.nextRunAt)}</small>
                )}
              </div>
              <div role="cell" className={styles.control}>
                <ToggleSwitch
                  checked={account.enabled && account.timerActive}
                  onChange={(enabled) => void handleChange(account.id, enabled)}
                  disabled={!account.available || pending !== null}
                  ariaLabel={t('keepalive.switch', { account: account.label })}
                />
                {pending === account.id && <small role="status">{t('keepalive.saving')}</small>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
