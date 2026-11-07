import { useTranslation } from 'react-i18next';
import { getTypeLabel } from '@/features/authFiles/constants';
import { collectRoutingProviders } from '../providers';
import styles from './ProviderFilter.module.scss';

interface ProviderFilterProps {
  providers: ReturnType<typeof collectRoutingProviders>;
  selected: string | null;
  disabled: boolean;
  onSelect: (provider: string | null) => void;
}

export function ProviderFilter({ providers, selected, disabled, onSelect }: ProviderFilterProps) {
  const { t } = useTranslation();
  return (
    <div className={styles.filters} role="group" aria-label={t('routing.provider_filter')}>
      {providers.map((provider) => (
        <button
          key={provider.id}
          type="button"
          className={styles.filter}
          aria-pressed={selected === provider.id}
          disabled={disabled}
          onClick={() => onSelect(provider.id)}
        >
          <span>{provider.name || getTypeLabel(t, provider.family)}</span>
          <span className={styles.count}>{provider.count}</span>
        </button>
      ))}
      <button
        type="button"
        className={`${styles.filter} ${styles.all}`}
        aria-pressed={selected === null}
        disabled={disabled}
        onClick={() => onSelect(null)}
      >
        <span>{t('routing.all_providers')}</span>
        <span className={styles.count}>
          {providers.reduce((total, provider) => total + provider.count, 0)}
        </span>
      </button>
    </div>
  );
}
