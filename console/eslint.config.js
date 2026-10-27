import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', 'src/gen', 'src/components/ui']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
  {
    // The intake pages ship to the public intake site, which has no edge behind it: they import
    // nothing that talks to edge or carries its API's shape.
    files: ['src/intake/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          paths: [
            { name: '@/api', message: "The intake pages use @/intake/api; edge's clients stay out of the public bundle." },
            { name: '@/session', message: 'The intake pages use @/intake/session.' },
            { name: '@/router', message: 'The intake pages use @/intake/router.' },
            { name: '@/components/AppSidebar', message: 'It calls edge.' },
            { name: '@/components/Replay', message: 'It calls edge.' },
            { name: '@/lib/useRunEvents', message: 'It calls edge.' },
          ],
          patterns: [
            { group: ['@/pages/*'], message: "The console's pages call edge; the intake pages are in src/intake/pages." },
            { group: ['@/gen/swarmeval/api/*'], message: "edge's API is not the intake site's; use @/gen/swarmeval/intake/*." },
          ],
        },
      ],
    },
  },
])
