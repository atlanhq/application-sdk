import { createResolver } from '@nuxt/kit'

const { resolve } = createResolver(import.meta.url)

// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  devtools: { enabled: true },

  modules: [
    '@nuxt/content',
    '@nuxt/eslint',
    '@nuxt/fonts',
    '@nuxt/scripts',
    '@nuxt/test-utils',
    '@vueuse/nuxt'
  ],



  app: {
    pageTransition: {
      name: 'page',
      mode: 'out-in'
    }
  },

  alias: {
    'styled-system': resolve('./styled-system')
  },

  components: {
    global: true,
    dirs: ['~/components'],
  },


  css: [
    '~/assets/css/global.css',
    '@atlanhq/atlantis/dist/styles.css',
  ],

  postcss: {
    plugins: {
      tailwindcss: {},
      autoprefixer: {},
      '@pandacss/dev/postcss': {},
    }
  }
})