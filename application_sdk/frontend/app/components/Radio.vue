<script setup lang="ts">
    import { Segmented } from '@atlanhq/atlantis'
    import type { RadioWidget } from '~/types/workflows'

    const { property } = defineProps<{ property: RadioWidget }>()

    const modelValue = defineModel<string>({ default: '' })

    const radioOptions = computed(() => {
        if (property.enum && property.enumNames) {
            return property.enum.map((value, index) => ({
                value,
                label: property.enumNames?.[index] || value,
            }))
        }
        return []
    })
</script>

<template>
    <div>
        <Segmented v-model="modelValue" :options="radioOptions" />
    </div>
</template>
