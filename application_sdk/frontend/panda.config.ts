import { defineConfig } from '@pandacss/dev'
import { createPreset } from '@atlanhq/atlantis-preset'

export default defineConfig({
    preflight: true,

    // Whether to use css reset
    presets: [createPreset()],

    // Where to look for your css declarations
    include: [
        './app/components/**/*.{js,jsx,ts,tsx,vue}',
        './app/pages/**/*.{js,jsx,ts,tsx,vue}',
    ],

    // Files to exclude
    exclude: [],

    // Useful for theme customization
    theme: {
        extend: {},
    },

    // The output directory for your css system
    outdir: 'styled-system',

    layers: {
        reset: 'pw-reset',
        base: 'pw-base',
        tokens: 'pw-tokens',
        recipes: 'pw-recipes',
        utilities: 'pw-utilities',
    },
})
