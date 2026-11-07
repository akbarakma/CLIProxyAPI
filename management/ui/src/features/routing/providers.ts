import type {
  RoutingBoard,
  RoutingDraft,
  RoutingDropTarget,
  RoutingMember,
  RoutingTier,
} from '@/types/routing';
import { moveMember, tierIndexOf } from './logic';

export const routingProviderId = (member: RoutingMember): string =>
  member.kind === 'compat' ? member.key : `family:${member.family}`;

export function collectRoutingProviders(members: readonly RoutingMember[]) {
  const providers = new Map<string, { id: string; family: string; name: string; count: number }>();
  for (const member of members) {
    const id = routingProviderId(member);
    const existing = providers.get(id);
    if (existing) existing.count += 1;
    else {
      providers.set(id, {
        id,
        family: member.family,
        name: member.kind === 'compat' ? member.name : '',
        count: 1,
      });
    }
  }
  return [...providers.values()].sort(
    (left, right) => left.family.localeCompare(right.family) || left.name.localeCompare(right.name)
  );
}

export function routingProviderView(
  board: RoutingBoard,
  draft: RoutingDraft,
  provider: string | null
) {
  if (provider === null) return { board, draft };
  const members = board.members.filter((member) => routingProviderId(member) === provider);
  const keys = new Set(members.map((member) => member.key));
  const tiers = draft.tiers
    .map((tier) => ({
      ...tier,
      memberKeys: tier.memberKeys.filter((key) => keys.has(key)),
    }))
    .filter((tier) => tier.memberKeys.length > 0);
  return { board: { ...board, members }, draft: { ...draft, tiers } };
}

export function moveProviderMember(
  tiers: readonly RoutingTier[],
  visible: readonly RoutingTier[],
  key: string,
  target: RoutingDropTarget
): RoutingTier[] {
  if (target.kind === 'tier') return moveMember(tiers, key, target);
  const before = visible[target.index];
  const after = visible[visible.length - 1];
  const index = before
    ? tiers.findIndex((tier) => tier.id === before.id)
    : after
      ? tiers.findIndex((tier) => tier.id === after.id) + 1
      : tiers.length;
  return moveMember(tiers, key, { kind: 'gap', index });
}

export function stepProviderMember(
  tiers: readonly RoutingTier[],
  visible: readonly RoutingTier[],
  key: string,
  direction: 'up' | 'down'
): RoutingTier[] {
  const from = tierIndexOf(visible, key);
  if (from < 0) return [...tiers];
  const next = visible[from + (direction === 'up' ? -1 : 1)];
  return moveProviderMember(
    tiers,
    visible,
    key,
    next
      ? { kind: 'tier', tierId: next.id }
      : { kind: 'gap', index: direction === 'up' ? 0 : visible.length }
  );
}
