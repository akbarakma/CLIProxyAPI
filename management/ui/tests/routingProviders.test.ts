import { describe, expect, test } from 'bun:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import '../src/i18n/index';
import { buildRoutingBoard } from '../src/services/api/routing';
import {
  buildRoutingPlan,
  collectModelOptions,
  draftFromBoard,
  resolveRoute,
} from '../src/features/routing/logic';
import {
  collectRoutingProviders,
  moveProviderMember,
  routingProviderView,
  stepProviderMember,
} from '../src/features/routing/providers';
import { ProviderFilter } from '../src/features/routing/components/ProviderFilter';

const boardFor = () =>
  buildRoutingBoard(
    {
      routing: { strategy: 'fill-first' },
      'api-keys': {
        claude: [{ keys: [{ 'api-key': 'test-key', priority: 100 }] }],
        'openai-compatibility': [
          { name: 'Juxta', prefix: 'juxta', models: [{ name: 'glm' }] },
          { name: 'Other gateway', models: [{ name: 'shared' }] },
        ],
      },
    },
    [
      { name: 'claude-main.json', type: 'claude', priority: 100 },
      { name: 'codex-alt.json', type: 'codex', priority: 75 },
      { name: 'claude-alt.json', type: 'claude', priority: 50 },
      { name: 'plugin.json', type: 'custom-provider', priority: 0 },
    ]
  );

describe('routing provider filters', () => {
  test('groups API keys with OAuth credentials and keeps custom gateways separate', () => {
    const providers = collectRoutingProviders(boardFor().members);
    expect(providers.find((provider) => provider.id === 'family:claude')?.count).toBe(3);
    expect(providers.find((provider) => provider.id === 'family:custom-provider')?.count).toBe(1);
    expect(
      providers
        .filter((provider) => provider.family === 'openai-compatibility')
        .map((provider) => provider.name)
    ).toEqual(['Juxta', 'Other gateway']);
  });

  test('filtering leaves the full draft and save plan unchanged', () => {
    const board = boardFor();
    const draft = draftFromBoard(board);
    const before = structuredClone(draft);
    const view = routingProviderView(board, draft, 'family:claude');
    expect(view.board.members).toHaveLength(3);
    expect(view.draft.tiers.map((tier) => tier.id)).toEqual(['p100', 'p50']);
    expect(draft).toEqual(before);
    expect(buildRoutingPlan(board, draft).changes).toEqual([]);
    expect(routingProviderView(board, draft, null)).toEqual({ board, draft });
  });

  test('a provider preview selects its own first eligible tier', () => {
    const board = boardFor();
    const draft = draftFromBoard(board);
    const view = routingProviderView(board, draft, 'family:codex');
    const preview = resolveRoute(
      view.board,
      view.draft,
      { channels: {}, credentials: {} },
      null,
      0
    );
    expect(preview.tiers).toHaveLength(1);
    expect(preview.tiers[0].state).toBe('active');
    expect(preview.tiers[0].members.map((member) => member.key)).toEqual(['cred:codex-alt.json']);
  });

  test('model suggestions follow the selected provider and All keeps shared aliases', () => {
    const board = boardFor();
    const draft = draftFromBoard(board);
    const catalog = {
      channels: {},
      credentials: { 'claude-main.json': ['shared'] },
    };
    const view = routingProviderView(board, draft, 'compat:0');
    expect(collectModelOptions(view.board, catalog)).toEqual(['glm', 'juxta/glm']);
    expect(collectModelOptions(board, catalog)).toContain('shared');
    expect(
      resolveRoute(board, draft, catalog, 'shared', 0)
        .tiers.flatMap((tier) => tier.members)
        .filter((member) => member.state === 'serving')
        .map((member) => member.key)
    ).toEqual(expect.arrayContaining(['cred:claude-main.json', 'compat:1']));
  });

  test('a filtered gap maps to the full board without losing hidden credentials', () => {
    const board = boardFor();
    const draft = draftFromBoard(board);
    const view = routingProviderView(board, draft, 'family:claude');
    const tiers = moveProviderMember(draft.tiers, view.draft.tiers, 'cred:claude-main.json', {
      kind: 'gap',
      index: 1,
    });
    const plan = buildRoutingPlan(board, { ...draft, tiers });
    expect(plan.priorities['cred:claude-main.json']).toBeGreaterThan(50);
    expect(plan.priorities['cred:claude-main.json']).toBeLessThan(75);
    expect(plan.priorities['cred:codex-alt.json']).toBeUndefined();
    expect(tiers.flatMap((tier) => tier.memberKeys).sort()).toEqual(
      board.members.map((member) => member.key).sort()
    );
  });

  test('menu and keyboard moves skip hidden provider tiers', () => {
    const board = boardFor();
    const draft = draftFromBoard(board);
    const view = routingProviderView(board, draft, 'family:claude');
    const tiers = stepProviderMember(draft.tiers, view.draft.tiers, 'cred:claude-alt.json', 'up');
    expect(tiers.find((tier) => tier.memberKeys.includes('cred:claude-alt.json'))?.id).toBe('p100');
    expect(
      buildRoutingPlan(board, { ...draft, tiers }).priorities['cred:codex-alt.json']
    ).toBeUndefined();
  });

  test('switching views retains edits for the existing save review', () => {
    const board = boardFor();
    const draft = draftFromBoard(board);
    const view = routingProviderView(board, draft, 'family:claude');
    const changed = {
      ...draft,
      tiers: stepProviderMember(draft.tiers, view.draft.tiers, 'cred:claude-alt.json', 'up'),
    };
    routingProviderView(board, changed, 'family:codex');
    const restored = routingProviderView(board, changed, 'family:claude');
    expect(restored.draft.tiers[0].memberKeys).toContain('cred:claude-alt.json');
    expect(buildRoutingPlan(board, changed).priorities['cred:claude-alt.json']).toBe(100);
    expect(buildRoutingPlan(board, draftFromBoard(board)).changes).toEqual([]);
  });

  test('provider controls expose selection and counts without credential details', () => {
    const html = renderToStaticMarkup(
      createElement(ProviderFilter, {
        providers: collectRoutingProviders(boardFor().members),
        selected: 'family:claude',
        disabled: false,
        onSelect: () => {},
      })
    );
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('Juxta');
    expect(html).not.toContain('test-key');
    expect(html).not.toContain('claude-main.json');
  });
});
