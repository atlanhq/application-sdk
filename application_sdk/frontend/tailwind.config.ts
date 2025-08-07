import type { Config } from 'tailwindcss'

export default {
    content: [
        './app/components/**/*.{js,vue,ts}',
        './app/layouts/**/*.vue',
        './app/pages/**/*.vue',
    ],
    theme: {
        extend: {},
    },
    plugins: [],
    safelist: [
        {
            pattern: /col-span-.+/,
        },
        {
            pattern: /col-start-.+/,
        },
        {
            pattern: /col-end-.+/,
        },
        {
            pattern: /row-span-.+/,
        },
        {
            pattern: /grid-cols-.+/,
        },
        'shadow-rc',
        'col-auto',
        'row-auto',
    ],
} satisfies Config
