import { defineContentConfig, defineCollection } from '@nuxt/content'

export default defineContentConfig({
    collections: {
        workflows: defineCollection({
            type: 'page',
            source: 'workflows/*.json',
        }),
        credentials: defineCollection({
            type: 'page',
            source: 'credentials/*.json',
        }),
    },
})
