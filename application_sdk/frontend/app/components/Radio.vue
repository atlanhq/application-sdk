<script setup lang="ts">
    import { Segmented } from '@atlanhq/atlantis'
    import type { RadioWidget } from '~/types/workflows'

    const { property, field } = defineProps<{
        property: RadioWidget
        field: unknown
    }>()

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
        <Segmented
            :modelValue="field.state.value"
            :options="radioOptions"
            @update:modelValue="field.handleChange"
        />
    </div>
</template>
