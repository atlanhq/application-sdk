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
} satisfies Config
