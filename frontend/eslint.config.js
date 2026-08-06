import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // THE `_` PREFIX IS NOW DECLARED, and it was already the convention. `replayClient.ts`
      // mirrors `client.ts`'s signatures exactly -- same parameters, same order -- because
      // vite.config.ts swaps one module for the other by alias, and a signature that drifted
      // would break in the replay build alone. Parameters it has no use for are prefixed `_`.
      // The config never knew that, so `npm run lint` was RED on two deliberate lines and had
      // been for as long as they existed, because nothing ran it. Declaring the pattern is not
      // a weakening: an UN-prefixed unused variable still fails, which is the case that matters.
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
      // `readStateBlurb` is allowed BY NAME rather than the rule being downgraded, and the
      // reason is not ergonomics. Moving it out of ReadStateBadge.tsx means exporting `STATE`
      // -- the badge's private lookup table -- so two files can read it: a refactor of a
      // shipped component, for a rule about dev-server fast refresh. And any change under
      // `src/` makes the committed `docs/replay/` bundle no longer the build of the current
      // source, so it would have to be rebuilt and re-verified as well. One named exception,
      // in one reviewable place, buys `--max-warnings=0` without either.
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true, allowExportNames: ['readStateBlurb'] },
      ],
    },
  }
);
